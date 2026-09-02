"""Django middleware for omero-web that works around a server-side bug in
OMERO's native thumbnail generator.

Background: OMERO's thumbnail service (ThumbnailBean) uses a fast, decimated
read path when scaling an image down for a preview. That path returns solid
black output for any Image whose Pixels are backed by a Bio-Formats
`.companion.ome` file spanning multiple physical TIFFs -- exactly the
structure habomero's HCS import pipeline uses (scripts/hcs.py,
scripts/import_scan.py) to build multi-channel Plate/Well images via
zero-duplication `ln_s` imports. Full-resolution rendering (used when a well
or field is opened directly) does not use that fast path and is unaffected.

This middleware intercepts two OMERO.web endpoints that both end up calling
OMERO's broken generator: the single-image thumbnail request
(`/webgateway/render_thumbnail/<iid>/...`, used by the classic plate grid)
and the JSONP batch endpoint (`/webgateway/get_thumbnails/...?id=...&id=...`,
used by the general data-browsing/"userdata" pages via jQuery). Once a
normal OMERO.web login session is confirmed *and* that session's own OMERO
permissions grant it access to every requested Image (checked via a
short-lived connection joined to the requester's own session, the same
mechanism OMERO.web's own view decorators use -- no plaintext password
needed), it regenerates each preview itself via the full-resolution render
path (proven correct) and a standard resize -- the same symlinked,
non-duplicated source data, just without OMERO's broken shortcut. Anything
that doesn't match, isn't logged in, isn't authorized, or fails for any
reason falls straight through to OMERO.web's normal view, so this can only
fix behavior, never break it further or serve an image to someone who
shouldn't see it.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import logging
import os
import random
import re
import threading
from typing import TYPE_CHECKING

from django.http import HttpResponse

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from django.http import HttpRequest
    from omero.gateway import BlitzGateway

logger = logging.getLogger(__name__)

# Mirrors omeroweb.webgateway.urls' own render_thumbnail pattern -- the
# plate grid JS (ome.plateview.js) appends a `size` path segment whenever
# the thumbnail-size slider has a value, which is the common case, not the
# exception. Matching only the bare "<iid>/" form (no size) missed almost
# every real request from the plate grid.
_THUMB_RE = re.compile(
    r"^/webgateway/render_thumbnail/(?P<iid>\d+)"
    r"/(?:(?P<w>\d+)/)?(?:(?P<h>\d+)/)?$"
)

# Mirrors omeroweb.webgateway.urls' get_thumbnails_json pattern. This is the
# endpoint the general "userdata"/browse pages actually use (jQuery.getJSON
# with a JSONP callback), and it hits the exact same broken native
# generator, just batched -- a separate code path from render_thumbnail
# that this middleware missed on the first pass.
_BATCH_RE = re.compile(r"^/webgateway/get_thumbnails/(?:(?P<w>\d+)/)?$")

_JS_CALLBACK_RE = re.compile(r"^[a-zA-Z_$][0-9a-zA-Z_$]*$")

_DEFAULT_SIZE = 96
# Matches OMERO.web's own default THUMBNAILS_BATCH setting -- requests
# larger than this aren't something the real UI ever sends.
_MAX_BATCH_IDS = 50
# Generous upper bound on requested thumbnail dimensions. This is a preview
# endpoint; nothing legitimate asks for more than this, and without a cap a
# crafted request could force a full-resolution render (an expensive,
# uncached full-plane read) for an arbitrarily large size.
_MAX_DIMENSION = 2048

_CACHE_DIR = os.environ.get(
    "THUMBFIX_CACHE_DIR", "/opt/omero/web/OMERO.web/var/thumbfix_cache"
)
# Soft cap on the number of cached preview files. Checked probabilistically
# on writes (not every write) to keep the common-case cost negligible.
_CACHE_MAX_FILES = int(os.environ.get("THUMBFIX_CACHE_MAX_FILES", "150000"))
_CACHE_PRUNE_CHECK_PROBABILITY = 1 / 500

_OMERO_HOST = os.environ.get("THUMBFIX_OMERO_HOST", "omero-server")
_OMERO_PORT = int(os.environ.get("THUMBFIX_OMERO_PORT", "4064"))
_OMERO_USER = os.environ.get("THUMBFIX_OMERO_USER")
_OMERO_PASSWORD = os.environ.get("THUMBFIX_OMERO_PASSWORD")
_OMERO_GROUP = os.environ.get("THUMBFIX_OMERO_GROUP")
_OMERO_SECURE = os.environ.get("THUMBFIX_OMERO_SECURE", "true").lower() not in (
    "0",
    "false",
    "no",
)


class ThumbnailFixMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response
        self._lock = threading.Lock()
        self._conn: BlitzGateway | None = None
        os.makedirs(_CACHE_DIR, exist_ok=True)

    def __call__(self, request: HttpRequest) -> HttpResponse:
        # No configured credentials, non-GET, or no active OMERO.web login
        # (in which case the normal view should handle redirecting to the
        # login page) -- pass straight through in all of these cases.
        if (
            not _OMERO_USER
            or not _OMERO_PASSWORD
            or request.method != "GET"
            or not request.session.get("connector")
        ):
            return self.get_response(request)

        single = _THUMB_RE.match(request.path)
        if single:
            return self._handle_single(request, single)

        batch = _BATCH_RE.match(request.path)
        if batch:
            return self._handle_batch(request, batch)

        return self.get_response(request)

    def _handle_single(
        self, request: HttpRequest, single: re.Match[str]
    ) -> HttpResponse:
        iid = single.group("iid")
        w = single.group("w")
        h = single.group("h")
        target_w = int(w) if w else _DEFAULT_SIZE
        target_h = int(h) if h else target_w
        if not self._dimensions_in_range(target_w, target_h):
            return self.get_response(request)

        if not self._authorized_for(request, [int(iid)]):
            # Not logged in with access to this specific Image (or the
            # authorization check itself failed) -- let the normal,
            # permission-checked OMERO.web view handle it.
            return self.get_response(request)

        data = self._cached_or_generate(iid, target_w, target_h)
        if data is None:
            return self.get_response(request)
        return HttpResponse(data, content_type="image/jpeg")

    def _handle_batch(self, request: HttpRequest, batch: re.Match[str]) -> HttpResponse:
        try:
            w_arg = batch.group("w")
            size = int(w_arg) if w_arg else _DEFAULT_SIZE
            if not self._dimensions_in_range(size, size):
                return self.get_response(request)

            ids = []
            for raw in request.GET.getlist("id"):
                try:
                    ids.append(int(raw))
                except (TypeError, ValueError):
                    continue
            ids = list(dict.fromkeys(ids))  # dedupe, preserve order

            if not ids or len(ids) > _MAX_BATCH_IDS:
                return self.get_response(request)

            if not self._authorized_for(request, ids):
                return self.get_response(request)

            # If any individual thumbnail can't be generated, fall through
            # to the normal view for the *whole* batch rather than mixing
            # our correct previews with null placeholders for the rest --
            # a simpler, more predictable failure mode. Anything already
            # generated for other ids in this pass stays cached for next
            # time, so nothing is wasted.
            result = {}
            for iid in ids:
                data = self._cached_or_generate(str(iid), size, size)
                if data is None:
                    return self.get_response(request)
                result[iid] = "data:image/jpeg;base64," + base64.b64encode(data).decode(
                    "ascii"
                )

            body = json.dumps(result)
            callback = request.GET.get("callback")
            if callback and _JS_CALLBACK_RE.match(callback):
                body = f"{callback}({body})"
                content_type = "application/javascript"
            else:
                content_type = "application/json"
            return HttpResponse(body, content_type=content_type)
        except Exception:
            logger.exception("thumbfix: failed handling get_thumbnails batch")
            return self.get_response(request)

    @staticmethod
    def _dimensions_in_range(width: int, height: int) -> bool:
        return 0 < width <= _MAX_DIMENSION and 0 < height <= _MAX_DIMENSION

    def _authorized_for(self, request: HttpRequest, ids: Iterable[int]) -> bool:
        """Check, via a connection joined to the *requester's own* OMERO
        session (never our service account), that every id is an Image
        they can actually see under OMERO's own group permissions.

        This is a real authorization check, not just "is someone logged
        in" -- without it, any authenticated user could request any Image
        ID and get back pixel data regardless of OMERO group membership,
        since the generation step below always uses a full-access service
        account.
        """

        from omeroweb.connector import Connector

        connector = Connector.from_session(request)
        if connector is None:
            return False

        user_conn = None
        try:
            user_conn = connector.join_connection("thumbfix-auth")
            if user_conn is None:
                return False
            # Search across every group this user belongs to, not just
            # their currently-active one -- the joined connection's default
            # group context won't necessarily be the group the requested
            # Image lives in, and getObject finds nothing across a group
            # mismatch even for an otherwise-authorized owner.
            user_conn.SERVICE_OPTS.setOmeroGroup("-1")
            return all(user_conn.getObject("Image", iid) is not None for iid in ids)
        except Exception:
            logger.exception("thumbfix: authorization check failed")
            return False
        finally:
            if user_conn is not None:
                # hard=False (closeSession, not killSession): this is a
                # *joined* connection sharing the user's real OMERO.web
                # session. The default hard=True calls killSession(),
                # which would terminate that session outright and log the
                # requesting user out on every single thumbnail request.
                with contextlib.suppress(Exception):
                    user_conn.close(hard=False)

    def _cached_or_generate(
        self, iid: str, target_w: int, target_h: int
    ) -> bytes | None:
        cache_path = os.path.join(_CACHE_DIR, f"{iid}_{target_w}x{target_h}.jpg")
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "rb") as f:
                    return f.read()
            except OSError:
                pass

        try:
            data = self._generate(iid, target_w, target_h)
        except Exception:
            logger.exception("thumbfix: failed to generate thumbnail for Image:%s", iid)
            return None

        if data is None:
            return None

        try:
            tmp_path = cache_path + f".tmp{os.getpid()}"
            with open(tmp_path, "wb") as f:
                f.write(data)
            os.replace(tmp_path, cache_path)
            if random.random() < _CACHE_PRUNE_CHECK_PROBABILITY:
                self._prune_cache_if_needed()
        except OSError:
            logger.exception("thumbfix: failed to write cache for Image:%s", iid)

        return data

    @staticmethod
    def _prune_cache_if_needed() -> None:
        try:
            entries = [e for e in os.scandir(_CACHE_DIR) if e.is_file()]
            if len(entries) <= _CACHE_MAX_FILES:
                return
            entries.sort(key=lambda e: e.stat().st_mtime)
            excess = len(entries) - _CACHE_MAX_FILES
            for entry in entries[:excess]:
                with contextlib.suppress(OSError):
                    os.remove(entry.path)
            logger.info("thumbfix: pruned %d cached previews", excess)
        except OSError:
            logger.exception("thumbfix: cache pruning failed")

    def _generate(self, iid: str, target_w: int, target_h: int) -> bytes | None:
        from PIL import Image as PILImage

        conn = self._get_connection()
        if conn is None:
            return None

        img = conn.getObject("Image", int(iid))
        if img is None:
            return None

        size_x, size_y = img.getSizeX(), img.getSizeY()
        native_size = max(size_x, size_y)
        # direct=True forces the full-resolution render path (proven
        # correct for companion-file HCS images), skipping the buggy
        # decimated-read shortcut entirely.
        native_jpeg = img.getThumbnail(size=(native_size,), direct=True)
        if not native_jpeg:
            return None

        im = PILImage.open(io.BytesIO(native_jpeg))
        im.thumbnail((target_w, target_h), PILImage.LANCZOS)
        buf = io.BytesIO()
        im.convert("RGB").save(buf, format="JPEG", quality=87)
        return buf.getvalue()

    def _get_connection(self) -> BlitzGateway | None:
        from omero.gateway import BlitzGateway

        with self._lock:
            if self._conn is not None:
                try:
                    if self._conn.keepAlive():
                        return self._conn
                except Exception:
                    pass
                with contextlib.suppress(Exception):
                    self._conn.close()
                self._conn = None

            conn = BlitzGateway(
                _OMERO_USER,
                _OMERO_PASSWORD,
                host=_OMERO_HOST,
                port=_OMERO_PORT,
                group=_OMERO_GROUP or None,
                secure=_OMERO_SECURE,
            )
            if not conn.connect():
                logger.error("thumbfix: failed to connect to OMERO server")
                return None
            self._conn = conn
            return self._conn
