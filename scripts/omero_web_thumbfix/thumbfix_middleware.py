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
normal OMERO.web login session is confirmed, it regenerates each preview
itself via the full-resolution render path (proven correct) and a standard
resize -- the same symlinked, non-duplicated source data, just without
OMERO's broken shortcut. Anything that doesn't match, isn't logged in, or
fails for any reason falls straight through to OMERO.web's normal view, so
this can only fix behavior, never break it further.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import logging
import os
import re
import threading
from typing import TYPE_CHECKING

from django.http import HttpResponse

if TYPE_CHECKING:
    from collections.abc import Callable

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

_CACHE_DIR = os.environ.get(
    "THUMBFIX_CACHE_DIR", "/opt/omero/web/OMERO.web/var/thumbfix_cache"
)
_OMERO_HOST = os.environ.get("THUMBFIX_OMERO_HOST", "omero-server")
_OMERO_PORT = int(os.environ.get("THUMBFIX_OMERO_PORT", "4064"))
_OMERO_USER = os.environ.get("THUMBFIX_OMERO_USER")
_OMERO_PASSWORD = os.environ.get("THUMBFIX_OMERO_PASSWORD")
_OMERO_GROUP = os.environ.get("THUMBFIX_OMERO_GROUP")


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
            iid = single.group("iid")
            w = single.group("w")
            h = single.group("h")
            target_w = int(w) if w else 96
            target_h = int(h) if h else (int(w) if w else 96)
            data = self._cached_or_generate(iid, target_w, target_h)
            if data is None:
                return self.get_response(request)
            return HttpResponse(data, content_type="image/jpeg")

        batch = _BATCH_RE.match(request.path)
        if batch:
            return self._handle_batch(request, batch)

        return self.get_response(request)

    def _handle_batch(self, request: HttpRequest, batch: re.Match[str]) -> HttpResponse:
        try:
            w_arg = batch.group("w")
            size = int(w_arg) if w_arg else 96
            ids = []
            for raw in request.GET.getlist("id"):
                try:
                    ids.append(int(raw))
                except (TypeError, ValueError):
                    continue
            ids = list(dict.fromkeys(ids))  # dedupe, preserve order

            result = {}
            for iid in ids:
                data = self._cached_or_generate(str(iid), size, size)
                result[iid] = (
                    "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
                    if data is not None
                    else None
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
        except OSError:
            logger.exception("thumbfix: failed to write cache for Image:%s", iid)

        return data

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
            )
            if not conn.connect():
                logger.error("thumbfix: failed to connect to OMERO server")
                return None
            self._conn = conn
            return self._conn
