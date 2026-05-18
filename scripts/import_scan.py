"""Import configured multi-root scan directories into OMERO."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
USERS_CONFIG_PATH = PROJECT_ROOT / "config/omero/users.yml"
SCAN_CONFIG_PATH = PROJECT_ROOT / "config/omero/scan_dirs.yml"
SCAN_ROOTS_STATE_PATH = PROJECT_ROOT / "data/state/scan_roots.yml"
IMPORT_STATE_PATH = PROJECT_ROOT / "data/state/imported_files.txt"
DATASET_STATE_PATH = PROJECT_ROOT / "data/state/path_datasets.yml"
PROJECT_STATE_PATH = PROJECT_ROOT / "data/state/root_projects.yml"
FAILURE_STATE_PATH = PROJECT_ROOT / "data/state/import_failures.yml"
SUPPORTED_EXTENSIONS = {
    ".tif",
    ".tiff",
    ".ome.tif",
    ".ome.tiff",
    ".png",
    ".jpg",
    ".jpeg",
}
RETRY_ATTEMPTS = 8
RETRY_INTERVAL_SECONDS = 3
PER_FILE_RETRY_ATTEMPTS = 4
PER_FILE_RETRY_BACKOFF_SECONDS = 5
SLEEP_BETWEEN_IMPORTS_SECONDS = 2
MAX_FAILURES_PER_RUN = 50
MAX_FILES_PER_RUN = 200
DB_STABLE_CHECKS_REQUIRED = 5
DB_STABLE_CHECK_INTERVAL_SECONDS = 3
LIST_RETRY_ATTEMPTS = 5
LIST_RETRY_BACKOFF_SECONDS = 3
SCAN_PROGRESS_EVERY_PATHS = 100_000
IMPORT_PROGRESS_EVERY_FILES = 50
IMPORT_WORKERS = 1


class ImportConfigError(ValueError):
    """Raised for invalid import configuration."""


def positive_int(payload: dict[str, object], key: str, default: int) -> int:
    """Read a positive integer from config with fallback."""

    value = payload.get(key)
    if isinstance(value, int) and value > 0:
        return value
    return default


def nonnegative_int(payload: dict[str, object], key: str, default: int) -> int:
    """Read a non-negative integer from config with fallback."""

    value = payload.get(key)
    if isinstance(value, int) and value >= 0:
        return value
    return default


def run_in_omero(command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "exec", "-T", "omero-server", "bash", "-lc", command],
        check=False,
        capture_output=True,
        text=True,
    )


def wait_for_server(max_attempts: int = 40, interval_seconds: int = 3) -> None:
    cmd = 'export PATH="/opt/omero/server/venv3/bin:$PATH"; omero version >/dev/null'
    for _ in range(max_attempts):
        if run_in_omero(cmd).returncode == 0:
            return
        time.sleep(interval_seconds)
    raise RuntimeError("OMERO server did not become ready in time")


def load_user_credentials() -> tuple[dict[str, str], str]:
    payload = yaml.safe_load(USERS_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    users = payload.get("users", [])
    if not isinstance(users, list) or not users:
        raise ImportConfigError("users.yml must contain at least one user")

    credentials: dict[str, str] = {}
    first_username = ""
    for index, item in enumerate(users):
        if not isinstance(item, dict):
            raise ImportConfigError("Each user entry must be a mapping")
        username = str(item.get("username", "")).strip()
        password = str(item.get("password", "")).strip()
        if not username or not password:
            raise ImportConfigError("Each user needs username and password")
        credentials[username] = password
        if index == 0:
            first_username = username
    return credentials, first_username


def load_import_config(  # noqa: C901, PLR0912, PLR0915
) -> tuple[str, str, str, int, int, int, int, int, int, int, int]:
    shared_group = ""
    root_prefix = "scan-root"
    import_mode = "copy"
    max_files_per_run = MAX_FILES_PER_RUN
    db_stable_checks = DB_STABLE_CHECKS_REQUIRED
    db_stable_interval = DB_STABLE_CHECK_INTERVAL_SECONDS
    max_failures_per_run = MAX_FAILURES_PER_RUN
    sleep_between_files = SLEEP_BETWEEN_IMPORTS_SECONDS
    scan_progress_every_paths = SCAN_PROGRESS_EVERY_PATHS
    import_progress_every_files = IMPORT_PROGRESS_EVERY_FILES
    import_workers = IMPORT_WORKERS
    if SCAN_CONFIG_PATH.exists():
        payload = yaml.safe_load(SCAN_CONFIG_PATH.read_text(encoding="utf-8")) or {}
        group = payload.get("shared_group")
        if isinstance(group, str) and group.strip():
            shared_group = group.strip()
        prefix = payload.get("omero_folder_root")
        if isinstance(prefix, str) and prefix.strip():
            root_prefix = prefix.strip()
        mode = payload.get("import_mode")
        if isinstance(mode, str) and mode.strip():
            normalized = mode.strip().lower()
            if normalized in {"copy", "inplace"}:
                import_mode = normalized
            else:
                raise ImportConfigError(
                    "import_mode must be either 'copy' or 'inplace'"
                )
        max_files_per_run = positive_int(
            payload, "max_files_per_run", max_files_per_run
        )
        db_stable_checks = positive_int(
            payload, "db_stable_checks_required", db_stable_checks
        )
        db_stable_interval = positive_int(
            payload,
            "db_stable_check_interval_seconds",
            db_stable_interval,
        )
        max_failures_per_run = positive_int(
            payload, "max_failures_per_run", max_failures_per_run
        )
        sleep_between_files = nonnegative_int(
            payload,
            "sleep_between_imports_seconds",
            sleep_between_files,
        )
        scan_progress_every_paths = positive_int(
            payload, "scan_progress_every_paths", scan_progress_every_paths
        )
        import_progress_every_files = positive_int(
            payload,
            "import_progress_every_files",
            import_progress_every_files,
        )
        import_workers = positive_int(payload, "import_workers", import_workers)
    env_cap = os.environ.get("IMPORT_MAX_FILES_PER_RUN", "").strip()
    if env_cap:
        try:
            parsed = int(env_cap)
        except ValueError as exc:
            raise ImportConfigError(
                "IMPORT_MAX_FILES_PER_RUN must be a positive integer"
            ) from exc
        if parsed <= 0:
            raise ImportConfigError(
                "IMPORT_MAX_FILES_PER_RUN must be a positive integer"
            )
        max_files_per_run = parsed
    env_sleep = os.environ.get("IMPORT_SLEEP_BETWEEN_IMPORTS_SECONDS", "").strip()
    if env_sleep:
        try:
            parsed_sleep = int(env_sleep)
        except ValueError as exc:
            raise ImportConfigError(
                "IMPORT_SLEEP_BETWEEN_IMPORTS_SECONDS must be a non-negative integer"
            ) from exc
        if parsed_sleep < 0:
            raise ImportConfigError(
                "IMPORT_SLEEP_BETWEEN_IMPORTS_SECONDS must be a non-negative integer"
            )
        sleep_between_files = parsed_sleep
    env_scan_log = os.environ.get("IMPORT_SCAN_PROGRESS_EVERY_PATHS", "").strip()
    if env_scan_log:
        try:
            scan_progress_every_paths = int(env_scan_log)
        except ValueError as exc:
            raise ImportConfigError(
                "IMPORT_SCAN_PROGRESS_EVERY_PATHS must be a positive integer"
            ) from exc
        if scan_progress_every_paths <= 0:
            raise ImportConfigError(
                "IMPORT_SCAN_PROGRESS_EVERY_PATHS must be a positive integer"
            )
    env_import_log = os.environ.get("IMPORT_PROGRESS_EVERY_FILES", "").strip()
    if env_import_log:
        try:
            import_progress_every_files = int(env_import_log)
        except ValueError as exc:
            raise ImportConfigError(
                "IMPORT_PROGRESS_EVERY_FILES must be a positive integer"
            ) from exc
        if import_progress_every_files <= 0:
            raise ImportConfigError(
                "IMPORT_PROGRESS_EVERY_FILES must be a positive integer"
            )
    env_workers = os.environ.get("IMPORT_WORKERS", "").strip()
    if env_workers:
        try:
            import_workers = int(env_workers)
        except ValueError as exc:
            raise ImportConfigError(
                "IMPORT_WORKERS must be a positive integer"
            ) from exc
        if import_workers <= 0:
            raise ImportConfigError("IMPORT_WORKERS must be a positive integer")
    return (
        shared_group,
        root_prefix,
        import_mode,
        max_files_per_run,
        db_stable_checks,
        db_stable_interval,
        max_failures_per_run,
        sleep_between_files,
        scan_progress_every_paths,
        import_progress_every_files,
        import_workers,
    )


def is_db_healthy() -> bool:
    """Check postgres health from compose metadata."""

    check = subprocess.run(
        [
            "docker",
            "inspect",
            "--format",
            "{{.State.Health.Status}}",
            "omero-db",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return check.returncode == 0 and check.stdout.strip() == "healthy"


def wait_for_db_stable(required_checks: int, interval_seconds: int) -> None:
    """Require multiple consecutive healthy checks before importing."""

    consecutive = 0
    attempts = max(required_checks * 20, 40)
    print(
        "[db-gate] waiting for stable DB: "
        f"need {required_checks} consecutive healthy checks"
    )
    for attempt in range(1, attempts + 1):
        if is_db_healthy():
            consecutive += 1
            print(f"[db-gate] healthy check {consecutive}/{required_checks}")
            if consecutive >= required_checks:
                print("[db-gate] stable DB confirmed")
                return
        else:
            if consecutive > 0:
                print("[db-gate] health streak reset")
            consecutive = 0
            if attempt % 5 == 0:
                print(f"[db-gate] waiting... attempt={attempt}/{attempts}")
        time.sleep(interval_seconds)
    raise RuntimeError("Database did not stay healthy long enough for safe import")


def load_scan_roots() -> dict[str, dict[str, str]]:
    if not SCAN_ROOTS_STATE_PATH.exists():
        raise FileNotFoundError("scan roots state missing; run poe scan-dirs first")
    payload = yaml.safe_load(SCAN_ROOTS_STATE_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        return {}
    result: dict[str, dict[str, str]] = {}
    for key, value in payload.items():
        if isinstance(key, str) and isinstance(value, dict):
            source = value.get("source")
            container_root = value.get("container_root")
            if isinstance(source, str) and isinstance(container_root, str):
                result[key] = {"source": source, "container_root": container_root}
    return result


def load_string_set(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def save_string_set(path: Path, values: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(sorted(values)) + "\n", encoding="utf-8")


def load_int_map(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in payload.items():
        if isinstance(k, str) and isinstance(v, int):
            out[k] = v
    return out


def save_int_map(path: Path, values: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(values, sort_keys=True), encoding="utf-8")


def save_failure_map(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(values, sort_keys=True), encoding="utf-8")


def run_as_user(
    username: str, password: str, command: str, group: str = ""
) -> subprocess.CompletedProcess[str]:
    login_user = shlex.quote(username)
    login_password = shlex.quote(password)
    group_arg = f" -g {shlex.quote(group)}" if group else ""
    full = (
        "set -euo pipefail; "
        'export PATH="/opt/omero/server/venv3/bin:$PATH"; '
        f"omero login {login_user}@localhost:4064{group_arg} "
        f"-w {login_password} >/dev/null; "
        f"{command}"
    )
    return run_in_omero(full)


def run_as_user_with_retry(
    username: str,
    password: str,
    command: str,
    group: str = "",
) -> subprocess.CompletedProcess[str]:
    """Retry transient OMERO connection failures during startup."""

    last: subprocess.CompletedProcess[str] | None = None
    for _ in range(RETRY_ATTEMPTS):
        result = run_as_user(username, password, command, group)
        last = result
        if result.returncode == 0:
            return result
        stderr_lower = result.stderr.lower()
        if not is_transient_import_error(stderr_lower):
            return result
        time.sleep(RETRY_INTERVAL_SECONDS)
    assert last is not None
    return last


def is_transient_import_error(stderr: str) -> bool:
    """Return True for transient backend/db errors worth retrying."""

    transient_markers = (
        "databasebusyexception",
        "transactionsystemexception",
        "connection has been closed",
        "the database system is in recovery mode",
        "broken pipe",
        "timed out",
        "connectionrefused",
        "isn't running",
    )
    lowered = stderr.lower()
    return any(marker in lowered for marker in transient_markers)


def extract_object_id(output: str, object_name: str) -> int:
    match = re.search(rf"{object_name}:(\d+)", output)
    if not match:
        raise RuntimeError(f"Could not parse {object_name} id from output: {output}")
    return int(match.group(1))


def list_root_files(
    source_root: str,
    container_root: str,
    max_files: int | None = None,
    scan_progress_every_paths: int = SCAN_PROGRESS_EVERY_PATHS,
) -> list[str]:
    """List files from host source path, mapped to container-root paths."""

    source_path = Path(source_root)
    if not source_path.exists() or not source_path.is_dir():
        raise RuntimeError(f"Source root is not available on host: {source_root}")

    print(f"[scan] source_root={source_root}")
    print(f"[scan] container_root={container_root}")
    print(f"[scan] indexing files under {source_root}")

    # Streaming `find` is substantially faster than Python-level rglob on huge trees.
    process = subprocess.Popen(
        ["find", source_root, "-type", "f"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None

    files: list[str] = []
    scanned = 0
    src_prefix = source_root.rstrip("/") + "/"
    try:
        for line in process.stdout:
            scanned += 1
            if scanned % scan_progress_every_paths == 0:
                print(f"[scan] visited={scanned} matched={len(files)}")
            abs_path = line.strip()
            if not abs_path:
                continue
            lowered = abs_path.lower()
            if not any(lowered.endswith(ext) for ext in SUPPORTED_EXTENSIONS):
                continue
            rel = abs_path.removeprefix(src_prefix)
            files.append(f"{container_root.rstrip('/')}/{rel}")
            if max_files is not None and len(files) >= max_files:
                print(
                    f"[scan] reached max candidate cap={max_files}, stopping scan early"
                )
                process.terminate()
                break
    finally:
        _stdout, stderr = process.communicate(timeout=15)
        if process.returncode not in (0, -15) and stderr.strip():
            raise RuntimeError(f"Host file scan failed: {stderr.strip()}")

    print(f"[scan] completed visited={scanned} matched={len(files)}")
    return files


def rel_path_from_root(abs_path: str, container_root: str) -> str:
    prefix = f"{container_root.rstrip('/')}/"
    return abs_path.removeprefix(prefix)


def dataset_key_for_rel_path(relative_path: str) -> str:
    parent = str(Path(relative_path).parent)
    return "root" if parent == "." else parent


def build_project_name(root_prefix: str, source_path: str) -> str:
    root_name = Path(source_path).name.strip()
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", root_name).strip("_")
    return f"{root_prefix} :: {safe or 'scan'}"


def build_dataset_name(rel_dir: str) -> str:
    return rel_dir.replace("/", " :: ")


def get_or_create_project(  # noqa: PLR0913
    owner: str,
    password: str,
    group: str,
    key: str,
    root_prefix: str,
    source: str,
    project_state: dict[str, int],
) -> int:
    if key in project_state:
        print(f"[project] reuse Project:{project_state[key]} for {source}")
        return project_state[key]
    print(f"[project] creating project for root source: {source}")
    name = shlex.quote(build_project_name(root_prefix, source))
    created = run_as_user_with_retry(
        owner, password, f"omero obj new Project name={name}", group
    )
    if created.returncode != 0:
        raise RuntimeError(
            f"Failed to create project for {source}: {created.stderr.strip()}"
        )
    project_id = extract_object_id(created.stdout, "Project")
    project_state[key] = project_id
    return project_id


def get_or_create_dataset(  # noqa: PLR0913
    owner: str,
    password: str,
    group: str,
    root_key: str,
    rel_dir: str,
    project_id: int,
    dataset_state: dict[str, int],
) -> tuple[int, bool]:
    map_key = f"{root_key}|{rel_dir}"
    if map_key in dataset_state:
        print(f"[dataset] reuse Dataset:{dataset_state[map_key]} for {rel_dir}")
        return dataset_state[map_key], False

    print(f"[dataset] creating dataset for {rel_dir}")
    name = shlex.quote(build_dataset_name(rel_dir))
    created = run_as_user_with_retry(
        owner, password, f"omero obj new Dataset name={name}", group
    )
    if created.returncode != 0:
        raise RuntimeError(
            f"Failed to create dataset {rel_dir}: {created.stderr.strip()}"
        )
    dataset_id = extract_object_id(created.stdout, "Dataset")

    link = run_as_user_with_retry(
        owner,
        password,
        "omero obj new ProjectDatasetLink "
        f"parent=Project:{project_id} child=Dataset:{dataset_id}",
        group,
    )
    if link.returncode != 0:
        # Idempotency guard: tolerate link failures when link already exists.
        existing = run_as_user_with_retry(
            owner,
            password,
            "omero hql "
            '"select count(l) from ProjectDatasetLink l '
            f'where l.parent.id = {project_id} and l.child.id = {dataset_id}"',
            group,
        )
        if (
            existing.returncode == 0 and "(1 row)" in existing.stdout
        ) or "already" in link.stderr.lower():
            pass
        else:
            raise RuntimeError(f"Failed to link dataset: {link.stderr.strip()}")

    dataset_state[map_key] = dataset_id
    return dataset_id, True


def delete_dataset(  # noqa: PLR0913
    owner: str,
    password: str,
    group: str,
    root_key: str,
    rel_dir: str,
    dataset_id: int,
    dataset_state: dict[str, int],
) -> None:
    """Best-effort cleanup for datasets created by failed imports."""

    result = run_as_user_with_retry(
        owner,
        password,
        f"omero delete Dataset:{dataset_id} -w --no-wait",
        group,
    )
    if result.returncode == 0:
        dataset_state.pop(f"{root_key}|{rel_dir}", None)


def load_dataset_image_names(
    owner: str,
    owner_password: str,
    shared_group: str,
    dataset_id: int,
) -> set[str]:
    """Load existing image names for a dataset from OMERO."""

    cmd = (
        "omero hql "
        f'"select i.name from Image i join i.datasetLinks l '
        f'where l.parent.id = {dataset_id}"'
    )
    result = run_as_user_with_retry(owner, owner_password, cmd, shared_group)
    if result.returncode != 0:
        return set()

    names: set[str] = set()
    for line in result.stdout.splitlines():
        text = line.strip()
        if not text or text.startswith("Using session") or text.startswith("("):
            continue
        # HQL output is typically like: [my_image.tif]
        cleaned = text.strip("[] ").strip()
        if cleaned:
            names.add(cleaned)
    return names


def build_import_command(import_mode: str, dataset_id: int, abs_path: str) -> str:
    """Build OMERO import command for configured transfer mode."""

    transfer_args = "--transfer=ln_s " if import_mode == "inplace" else ""
    debug_level = os.environ.get("IMPORT_OMERO_DEBUG", "").strip()
    debug_args = f"--debug {shlex.quote(debug_level)} " if debug_level else ""
    return (
        f"omero import {transfer_args}{debug_args}-d {dataset_id} "
        f"{shlex.quote(abs_path)}"
    )


def import_one_file(  # noqa: PLR0913
    owner: str,
    owner_password: str,
    shared_group: str,
    import_mode: str,
    root_key: str,
    project_id: int,
    container_root: str,
    abs_path: str,
    dataset_state: dict[str, int],
) -> tuple[bool, str, str]:
    """Import a file with retries and best-effort cleanup on failure."""

    tracking_key = f"{root_key}:{abs_path}"
    rel_path = rel_path_from_root(abs_path, container_root)
    rel_dir = dataset_key_for_rel_path(rel_path)
    dataset_id, dataset_created = get_or_create_dataset(
        owner,
        owner_password,
        shared_group,
        root_key,
        rel_dir,
        project_id,
        dataset_state,
    )

    command = build_import_command(import_mode, dataset_id, abs_path)
    print(f"[import-start] {rel_path} -> Dataset:{dataset_id}")
    result: subprocess.CompletedProcess[str] | None = None
    for attempt in range(1, PER_FILE_RETRY_ATTEMPTS + 1):
        result = run_as_user_with_retry(owner, owner_password, command, shared_group)
        if result.returncode == 0:
            print(f"[import-done] [{root_key}] {rel_path} -> Dataset:{dataset_id}")
            return True, tracking_key, ""
        if not is_transient_import_error(result.stderr):
            break
        print(
            f"[retry {attempt}/{PER_FILE_RETRY_ATTEMPTS}] transient import "
            f"failure for {rel_path}"
        )
        time.sleep(PER_FILE_RETRY_BACKOFF_SECONDS * attempt)

    assert result is not None
    if dataset_created:
        delete_dataset(
            owner,
            owner_password,
            shared_group,
            root_key,
            rel_dir,
            dataset_id,
            dataset_state,
        )
    return False, tracking_key, result.stderr.strip()


def import_to_dataset(  # noqa: PLR0913
    owner: str,
    owner_password: str,
    shared_group: str,
    import_mode: str,
    root_key: str,
    container_root: str,
    dataset_id: int,
    abs_path: str,
) -> tuple[bool, str, str]:
    """Import one file when dataset is already known."""

    rel_path = rel_path_from_root(abs_path, container_root)
    command = build_import_command(import_mode, dataset_id, abs_path)
    result: subprocess.CompletedProcess[str] | None = None
    for attempt in range(1, PER_FILE_RETRY_ATTEMPTS + 1):
        result = run_as_user_with_retry(owner, owner_password, command, shared_group)
        if result.returncode == 0:
            print(f"[import-done] [{root_key}] {rel_path} -> Dataset:{dataset_id}")
            return True, f"{root_key}:{abs_path}", ""
        if not is_transient_import_error(result.stderr):
            break
        print(
            f"[retry {attempt}/{PER_FILE_RETRY_ATTEMPTS}] transient import "
            f"failure for {rel_path}"
        )
        time.sleep(PER_FILE_RETRY_BACKOFF_SECONDS * attempt)
    assert result is not None
    return False, f"{root_key}:{abs_path}", result.stderr.strip()


def summarize_import_error(error: str) -> str:
    """Extract a useful one-line error summary for console logs."""

    lines = [line.strip() for line in error.splitlines() if line.strip()]
    if not lines:
        return "unknown error"
    priority_tokens = (
        "caused by",
        "exception",
        "error:",
        "permission denied",
        "no such file",
        "unsupported",
        "cannot",
    )
    for line in lines:
        lowered = line.lower()
        if "report bugs at https://www.openmicroscopy.org/forums" in lowered:
            continue
        if any(token in lowered for token in priority_tokens):
            return line
    for line in lines:
        if "report bugs at https://www.openmicroscopy.org/forums" not in line.lower():
            return line
    return lines[0]


def import_root_files(  # noqa: PLR0913, C901, PLR0912, PLR0915
    owner: str,
    owner_password: str,
    shared_group: str,
    import_mode: str,
    root_key: str,
    source_root: str,
    container_root: str,
    project_id: int,
    imported: set[str],
    dataset_state: dict[str, int],
    budget_remaining: int,
    max_failures_per_run: int,
    sleep_between_imports_seconds: int,
    scan_progress_every_paths: int,
    import_progress_every_files: int,
    import_workers: int,
) -> tuple[int, dict[str, str], bool, dict[str, int]]:
    """Import files for a single root. Returns count, failures, hit_cap."""

    imported_count = 0
    skipped_existing_count = 0
    skipped_tracked_count = 0
    failures: dict[str, str] = {}
    dataset_image_cache: dict[int, set[str]] = {}
    root_files: list[str] | None = None
    for attempt in range(1, LIST_RETRY_ATTEMPTS + 1):
        try:
            root_files = list_root_files(
                source_root,
                container_root,
                max_files=budget_remaining if budget_remaining > 0 else None,
                scan_progress_every_paths=scan_progress_every_paths,
            )
            break
        except RuntimeError as exc:
            print(
                f"[retry {attempt}/{LIST_RETRY_ATTEMPTS}] transient list failure "
                f"for {source_root}: {exc}"
            )
            time.sleep(LIST_RETRY_BACKOFF_SECONDS * attempt)
    if root_files is None:
        print(f"[root-list-failed] {source_root}")
        return (
            0,
            {},
            False,
            {
                "candidates": 0,
                "queued": 0,
                "skipped_tracked": 0,
                "skipped_existing": 0,
            },
        )
    print(
        f"[root] {source_root}: candidate files={len(root_files)} "
        f"budget={budget_remaining}"
    )

    # Build worklist serially so dataset/project bookkeeping stays consistent.
    work: list[tuple[str, int]] = []
    for abs_path in root_files:
        if imported_count >= budget_remaining:
            print(f"Reached max_files_per_run budget for this run ({budget_remaining})")
            return (
                imported_count,
                failures,
                True,
                {
                    "candidates": len(root_files),
                    "queued": len(work),
                    "skipped_tracked": skipped_tracked_count,
                    "skipped_existing": skipped_existing_count,
                },
            )
        tracking_key = f"{root_key}:{abs_path}"
        if imported_count % 10 == 0:
            print(f"[import-candidate] {abs_path}")
        if tracking_key in imported:
            skipped_tracked_count += 1
            continue
        rel_path = rel_path_from_root(abs_path, container_root)
        rel_dir = dataset_key_for_rel_path(rel_path)
        dataset_id, _ = get_or_create_dataset(
            owner,
            owner_password,
            shared_group,
            root_key,
            rel_dir,
            project_id,
            dataset_state,
        )
        if dataset_id not in dataset_image_cache:
            dataset_image_cache[dataset_id] = load_dataset_image_names(
                owner,
                owner_password,
                shared_group,
                dataset_id,
            )
        file_name = Path(abs_path).name
        if file_name in dataset_image_cache[dataset_id]:
            imported.add(tracking_key)
            skipped_existing_count += 1
            if skipped_existing_count % 25 == 0:
                print(
                    f"[dedupe] skipped_existing={skipped_existing_count} "
                    f"dataset={dataset_id}"
                )
            continue
        work.append((abs_path, dataset_id))

    if import_workers <= 1:
        for abs_path, dataset_id in work:
            ok, tracked, error = import_to_dataset(
                owner,
                owner_password,
                shared_group,
                import_mode,
                root_key,
                container_root,
                dataset_id,
                abs_path,
            )
            if not ok:
                failures[tracked] = error
                rel_path = rel_path_from_root(abs_path, container_root)
                print(f"[failed] {rel_path}: {summarize_import_error(error)}")
                if len(failures) >= max_failures_per_run:
                    print(
                        f"Reached failure cap ({max_failures_per_run}), "
                        "stopping import run"
                    )
                    return (
                        imported_count,
                        failures,
                        True,
                        {
                            "candidates": len(root_files),
                            "queued": len(work),
                            "skipped_tracked": skipped_tracked_count,
                            "skipped_existing": skipped_existing_count,
                        },
                    )
                continue

            imported.add(tracked)
            imported_count += 1
            dataset_image_cache.setdefault(dataset_id, set()).add(Path(abs_path).name)
            if imported_count % import_progress_every_files == 0:
                print(f"[root-progress] imported={imported_count} for {source_root}")
            time.sleep(sleep_between_imports_seconds)
            if sleep_between_imports_seconds > 0:
                print(f"[pace] slept {sleep_between_imports_seconds}s")
        return (
            imported_count,
            failures,
            False,
            {
                "candidates": len(root_files),
                "queued": len(work),
                "skipped_tracked": skipped_tracked_count,
                "skipped_existing": skipped_existing_count,
            },
        )

    print(f"[parallel] enabled with import_workers={import_workers}")
    with ThreadPoolExecutor(max_workers=import_workers) as pool:
        futures = {
            pool.submit(
                import_to_dataset,
                owner,
                owner_password,
                shared_group,
                import_mode,
                root_key,
                container_root,
                dataset_id,
                abs_path,
            ): (abs_path, dataset_id)
            for abs_path, dataset_id in work
        }
        for future in as_completed(futures):
            abs_path, dataset_id = futures[future]
            ok, tracked, error = future.result()
            if not ok:
                failures[tracked] = error
                rel_path = rel_path_from_root(abs_path, container_root)
                print(f"[failed] {rel_path}: {summarize_import_error(error)}")
                if len(failures) >= max_failures_per_run:
                    print(
                        f"Reached failure cap ({max_failures_per_run}), "
                        "stopping import run"
                    )
                    return (
                        imported_count,
                        failures,
                        True,
                        {
                            "candidates": len(root_files),
                            "queued": len(work),
                            "skipped_tracked": skipped_tracked_count,
                            "skipped_existing": skipped_existing_count,
                        },
                    )
                continue
            imported.add(tracked)
            imported_count += 1
            dataset_image_cache.setdefault(dataset_id, set()).add(Path(abs_path).name)
            if imported_count % import_progress_every_files == 0:
                print(f"[root-progress] imported={imported_count} for {source_root}")
    if skipped_existing_count > 0:
        print(f"[dedupe] skipped {skipped_existing_count} existing image(s)")
    return (
        imported_count,
        failures,
        False,
        {
            "candidates": len(root_files),
            "queued": len(work),
            "skipped_tracked": skipped_tracked_count,
            "skipped_existing": skipped_existing_count,
        },
    )


def import_files() -> None:
    credentials, owner = load_user_credentials()
    owner_password = credentials[owner]
    (
        shared_group,
        root_prefix,
        import_mode,
        max_files_per_run,
        db_stable_checks,
        db_stable_interval,
        max_failures_per_run,
        sleep_between_imports_seconds,
        scan_progress_every_paths,
        import_progress_every_files,
        import_workers,
    ) = load_import_config()
    wait_for_db_stable(db_stable_checks, db_stable_interval)

    roots = load_scan_roots()
    if not roots:
        print("No scan roots configured")
        return
    print(f"[import] loaded roots={len(roots)} mode={import_mode}")

    imported = load_string_set(IMPORT_STATE_PATH)
    imported_before = len(imported)
    dataset_state = load_int_map(DATASET_STATE_PATH)
    project_state = load_int_map(PROJECT_STATE_PATH)

    imported_count = 0
    failures: dict[str, str] = {}
    total_candidates = 0
    total_queued = 0
    total_skipped_tracked = 0
    total_skipped_existing = 0
    for root_key, root_data in sorted(roots.items()):
        source = root_data["source"]
        container_root = root_data["container_root"]
        print(f"[root-begin] {root_key} source={source}")
        print(f"[root-begin] {root_key} container_root={container_root}")
        try:
            project_id = get_or_create_project(
                owner,
                owner_password,
                shared_group,
                root_key,
                root_prefix,
                source,
                project_state,
            )
        except RuntimeError as exc:
            print(f"[root-failed] {source}: {exc}")
            continue

        root_imported, root_failures, hit_cap, root_stats = import_root_files(
            owner,
            owner_password,
            shared_group,
            import_mode,
            root_key,
            source,
            container_root,
            project_id,
            imported,
            dataset_state,
            max_files_per_run - imported_count,
            max_failures_per_run,
            sleep_between_imports_seconds,
            scan_progress_every_paths,
            import_progress_every_files,
            import_workers,
        )
        imported_count += root_imported
        failures.update(root_failures)
        total_candidates += root_stats["candidates"]
        total_queued += root_stats["queued"]
        total_skipped_tracked += root_stats["skipped_tracked"]
        total_skipped_existing += root_stats["skipped_existing"]
        print(
            f"[root-end] {root_key} imported_this_root={root_imported} "
            f"failures_this_root={len(root_failures)} "
            f"candidates={root_stats['candidates']} "
            f"queued={root_stats['queued']} "
            f"skipped_tracked={root_stats['skipped_tracked']} "
            f"skipped_existing={root_stats['skipped_existing']}"
        )
        if hit_cap:
            break

    save_string_set(IMPORT_STATE_PATH, imported)
    save_int_map(DATASET_STATE_PATH, dataset_state)
    save_int_map(PROJECT_STATE_PATH, project_state)
    save_failure_map(FAILURE_STATE_PATH, failures)
    imported_after = len(imported)
    print(
        "[run-summary] "
        f"candidates={total_candidates} queued={total_queued} "
        f"imported_new={imported_count} skipped_tracked={total_skipped_tracked} "
        f"skipped_existing={total_skipped_existing} failures={len(failures)} "
        f"tracked_before={imported_before} tracked_after={imported_after}"
    )
    if imported_count == 0:
        print("No new files to import")
    else:
        print(f"Imported {imported_count} new file(s) total")
    if failures:
        print(
            f"Encountered {len(failures)} file import failure(s); "
            f"see {FAILURE_STATE_PATH}"
        )


def main() -> None:
    wait_for_server()
    import_files()


if __name__ == "__main__":
    main()
