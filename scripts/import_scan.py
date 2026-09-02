"""Import configured multi-root scan directories into OMERO."""

from __future__ import annotations

import base64
import os
import re
import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import yaml

try:
    from scripts import hcs
except ImportError:  # running as `python scripts/import_scan.py`, not as a package
    import hcs

PROJECT_ROOT = Path(__file__).resolve().parent.parent
USERS_CONFIG_PATH = PROJECT_ROOT / "config/omero/users.yml"
SCAN_CONFIG_PATH = PROJECT_ROOT / "config/omero/scan_dirs.yml"
ENV_PATH = PROJECT_ROOT / ".env"
SCAN_ROOTS_STATE_PATH = PROJECT_ROOT / "data/state/scan_roots.yml"
IMPORT_STATE_PATH = PROJECT_ROOT / "data/state/imported_files.txt"
DATASET_STATE_PATH = PROJECT_ROOT / "data/state/path_datasets.yml"
PROJECT_STATE_PATH = PROJECT_ROOT / "data/state/root_projects.yml"
FAILURE_STATE_PATH = PROJECT_ROOT / "data/state/import_failures.yml"
HCS_SHADOW_ROOT = PROJECT_ROOT / "data/omero/hcs_shadow"  # bind-mounted as /OMERO in
# the omero-server container (see compose.yml) — this must live there, not under
# data/state, because `omero import` needs to read the shadow symlink and companion
# files from inside the container.
HCS_FIELD_STATE_PATH = PROJECT_ROOT / "data/state/hcs_imported_fields.txt"
HCS_PLATE_STATE_PATH = PROJECT_ROOT / "data/state/hcs_plates.yml"
HCS_WELL_STATE_PATH = PROJECT_ROOT / "data/state/hcs_wells.yml"
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
PROJECT_RECORD_COLUMN_COUNT = 5
DATASET_RECORD_COLUMN_COUNT = 3
GROUP_RECORD_COLUMN_COUNT = 3
DUPLICATE_RECORD_MIN_COUNT = 2


class ImportConfigError(ValueError):
    """Raised for invalid import configuration."""


@dataclass(frozen=True)
class ProjectRecord:
    """Minimal OMERO Project placement details used for duplicate cleanup."""

    project_id: int
    name: str
    group: str
    owner: str


@dataclass(frozen=True)
class DatasetRecord:
    """Minimal OMERO Dataset details used for duplicate cleanup."""

    dataset_id: int
    name: str


@dataclass(frozen=True)
class GroupRecord:
    """Minimal OMERO group details used for placement reconciliation."""

    group_id: int
    name: str


def read_env_var(name: str) -> str:
    """Read a required variable from process env or local .env file."""

    if value := os.getenv(name):
        return value

    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, raw = line.split("=", maxsplit=1)
            if key.strip() == name:
                return raw.strip()

    raise ImportConfigError(f"Missing required environment variable: {name}")


def user_password(item: dict[str, object]) -> str:
    """Read an OMERO user password from a literal or configured env var."""

    password = item.get("password")
    if isinstance(password, str) and password.strip():
        return password.strip()

    password_env = item.get("password_env")
    if isinstance(password_env, str) and password_env.strip():
        return read_env_var(password_env.strip())

    raise ImportConfigError("Each user needs password or password_env")


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


def run_as_root(command: str) -> subprocess.CompletedProcess[str]:
    """Run an OMERO CLI command as root inside the server container."""

    full = (
        "set -euo pipefail; "
        'export PATH="/opt/omero/server/venv3/bin:$PATH"; '
        'omero -C -s localhost -p 4064 -u root -w "$ROOTPASS" -g system '
        f"{command}"
    )
    return run_in_omero(full)


def run_as_root_with_retry(command: str) -> subprocess.CompletedProcess[str]:
    """Retry transient OMERO root-session failures during cleanup."""

    last: subprocess.CompletedProcess[str] | None = None
    for _ in range(RETRY_ATTEMPTS):
        result = run_as_root(command)
        last = result
        if result.returncode == 0:
            return result
        combined_output = f"{result.stderr}\n{result.stdout}"
        if not is_transient_import_error(combined_output):
            return result
        time.sleep(RETRY_INTERVAL_SECONDS)
    assert last is not None
    return last


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
        if not username:
            raise ImportConfigError("Each user needs username")
        credentials[username] = user_password(item)
        if index == 0:
            first_username = username
    return credentials, first_username


def load_import_config(  # noqa: C901, PLR0912, PLR0915
) -> tuple[str, str, str, int, int, int, int, int, int, int, int, bool]:
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
    delete_omero_missing_files = False
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
        max_files_per_run = nonnegative_int(
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
        delete_missing_value = payload.get(
            "delete_omero_missing_files",
            payload.get("delete_missing_files"),
        )
        if isinstance(delete_missing_value, bool):
            delete_omero_missing_files = delete_missing_value
    env_cap = os.environ.get("IMPORT_MAX_FILES_PER_RUN", "").strip()
    if env_cap:
        try:
            parsed = int(env_cap)
        except ValueError as exc:
            raise ImportConfigError(
                "IMPORT_MAX_FILES_PER_RUN must be a non-negative integer"
            ) from exc
        if parsed < 0:
            raise ImportConfigError(
                "IMPORT_MAX_FILES_PER_RUN must be a non-negative integer"
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
        delete_omero_missing_files,
    )


def load_default_import_user() -> str:
    """Load optional default OMERO user for all imports."""

    if not SCAN_CONFIG_PATH.exists():
        return ""
    payload = yaml.safe_load(SCAN_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    import_user = payload.get("import_user")
    if isinstance(import_user, str) and import_user.strip():
        return import_user.strip()
    return ""


def load_reimport_legacy_import_state() -> bool:
    """Load whether old unscoped imported-file state should trigger reimport."""

    if not SCAN_CONFIG_PATH.exists():
        return False
    payload = yaml.safe_load(SCAN_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    value = payload.get("reimport_legacy_import_state")
    return value is True


def load_cleanup_obsolete_duplicate_projects() -> bool:
    """Load whether old same-named Project duplicates should be removed."""

    if not SCAN_CONFIG_PATH.exists():
        return True
    payload = yaml.safe_load(SCAN_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    value = payload.get("cleanup_obsolete_duplicate_projects")
    if isinstance(value, bool):
        return value
    return True


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
            group = value.get("group")
            import_user = value.get("import_user")
            hcs_channels = value.get("hcs_channels")
            hcs_enabled = value.get("hcs_enabled")
            if isinstance(source, str) and isinstance(container_root, str):
                result[key] = {"source": source, "container_root": container_root}
                if isinstance(group, str) and group.strip():
                    result[key]["group"] = group.strip()
                if isinstance(import_user, str) and import_user.strip():
                    result[key]["import_user"] = import_user.strip()
                if isinstance(hcs_channels, str) and hcs_channels.strip():
                    result[key]["hcs_channels"] = hcs_channels.strip()
                if isinstance(hcs_enabled, str) and hcs_enabled.strip():
                    result[key]["hcs_enabled"] = hcs_enabled.strip()
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


def run_python_as_user(
    username: str, password: str, group: str, script: str
) -> subprocess.CompletedProcess[str]:
    """Run a Python script inside the OMERO server container's client venv.

    Used for the small set of operations (Well/WellSample creation) that need the
    OMERO Python API rather than the `omero` CLI, because the CLI's generic `obj new`
    command can't populate a required collection field (`Well.wellSamples`) at
    creation time. The script is base64-encoded to avoid shell-quoting a multi-line
    Python source string; connection details are passed via environment variables.
    """

    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    full = (
        "set -euo pipefail; "
        'export PATH="/opt/omero/server/venv3/bin:$PATH"; '
        f"export HCS_OMERO_USER={shlex.quote(username)}; "
        f"export HCS_OMERO_PASSWORD={shlex.quote(password)}; "
        f"export HCS_OMERO_GROUP={shlex.quote(group)}; "
        f"echo {shlex.quote(encoded)} | base64 -d | python3 -"
    )
    return run_in_omero(full)


def run_python_as_user_with_retry(
    username: str, password: str, group: str, script: str
) -> subprocess.CompletedProcess[str]:
    """Retry transient OMERO connection failures for embedded Python scripts."""

    last: subprocess.CompletedProcess[str] | None = None
    for _ in range(RETRY_ATTEMPTS):
        result = run_python_as_user(username, password, group, script)
        last = result
        if result.returncode == 0:
            return result
        if not is_transient_import_error(f"{result.stderr}\n{result.stdout}".lower()):
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
        "connecttimeoutexception",
        "server not fully initialized",
        "obtained null object prox",
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


def parse_project_records(output: str) -> list[ProjectRecord]:
    """Parse OMERO CLI HQL table rows for Project placement records."""

    records: list[ProjectRecord] = []
    for line in output.splitlines():
        if "|" not in line:
            continue
        cols = [col.strip() for col in line.split("|")]
        if len(cols) < PROJECT_RECORD_COLUMN_COUNT or not cols[0].isdigit():
            continue
        records.append(
            ProjectRecord(
                project_id=int(cols[1]),
                name=cols[2],
                group=cols[3],
                owner=cols[4],
            )
        )
    return records


def list_projects_by_name(project_name: str) -> list[ProjectRecord]:
    """List all OMERO Projects matching a generated scan-root Project name."""

    query = (
        "select p.id, p.name, details.group.id, details.owner.omeName from Project p"
    )
    result = run_as_root_with_retry(f"hql {shlex.quote(query)}")
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        print(f"[duplicate-project-cleanup-warn] list failed: {detail}")
        return []
    return [
        record
        for record in parse_project_records(result.stdout)
        if record.name == project_name
    ]


def delete_project(project_id: int) -> bool:
    """Delete an obsolete duplicate OMERO Project."""

    result = run_as_root_with_retry(f"delete Project:{project_id} --wait -1")
    if result.returncode == 0:
        return True
    detail = result.stderr.strip() or result.stdout.strip()
    print(f"[duplicate-project-cleanup-failed] Project:{project_id}: {detail}")
    return False


def parse_dataset_records(output: str) -> list[DatasetRecord]:
    """Parse OMERO CLI HQL table rows for Dataset records."""

    records: list[DatasetRecord] = []
    for line in output.splitlines():
        if "|" not in line:
            continue
        cols = [col.strip() for col in line.split("|")]
        if len(cols) < DATASET_RECORD_COLUMN_COUNT or not cols[0].isdigit():
            continue
        records.append(DatasetRecord(dataset_id=int(cols[1]), name=cols[2]))
    return records


def list_project_datasets(project_id: int) -> list[DatasetRecord]:
    """List all Datasets linked under a Project."""

    query = (
        "select d.id, d.name from Dataset d "
        f"join d.projectLinks l where l.parent.id = {project_id}"
    )
    result = run_as_root_with_retry(f"hql {shlex.quote(query)}")
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        print(f"[duplicate-dataset-cleanup-warn] list failed: {detail}")
        return []
    return parse_dataset_records(result.stdout)


def delete_dataset_as_root(dataset_id: int) -> bool:
    """Delete an obsolete duplicate OMERO Dataset."""

    result = run_as_root_with_retry(f"delete Dataset:{dataset_id} --wait -1")
    if result.returncode == 0:
        return True
    detail = result.stderr.strip() or result.stdout.strip()
    print(f"[duplicate-dataset-cleanup-failed] Dataset:{dataset_id}: {detail}")
    return False


def parse_group_records(output: str) -> list[GroupRecord]:
    """Parse OMERO CLI HQL table rows for group records."""

    records: list[GroupRecord] = []
    for line in output.splitlines():
        if "|" not in line:
            continue
        cols = [col.strip() for col in line.split("|")]
        if len(cols) < GROUP_RECORD_COLUMN_COUNT or not cols[0].isdigit():
            continue
        records.append(GroupRecord(group_id=int(cols[1]), name=cols[2]))
    return records


def list_groups() -> list[GroupRecord]:
    """List OMERO groups."""

    query = "select g.id, g.name from ExperimenterGroup g"
    result = run_as_root_with_retry(f"hql {shlex.quote(query)}")
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        print(f"[project-reconcile-warn] group list failed: {detail}")
        return []
    return parse_group_records(result.stdout)


def group_ids_by_name(group_name: str) -> set[str]:
    """Return OMERO group IDs matching a configured group name."""

    return {
        str(record.group_id) for record in list_groups() if record.name == group_name
    }


def project_dataset_count(project_id: int) -> int:
    """Count Datasets linked under a Project."""

    return len(list_project_datasets(project_id))


def choose_configured_project_record(
    records: list[ProjectRecord],
    owner: str,
    group_ids: set[str],
    fallback_project_id: int,
) -> ProjectRecord | None:
    """Choose the Project matching configured owner/group, or a fallback ID."""

    configured_records = [
        record
        for record in records
        if record.owner == owner and (not group_ids or record.group in group_ids)
    ]
    if configured_records:
        return max(
            configured_records,
            key=lambda record: (
                project_dataset_count(record.project_id),
                record.project_id,
            ),
        )
    for record in records:
        if record.project_id == fallback_project_id:
            return record
    return None


def reconcile_project_id(  # noqa: PLR0913
    owner: str,
    group: str,
    root_key: str,
    root_prefix: str,
    source: str,
    project_id: int,
    project_state: dict[str, int],
) -> int:
    """Prefer the configured owner/group Project over stale local state."""

    project_name = build_project_name(root_prefix, source)
    records = list_projects_by_name(project_name)
    if not records:
        return project_id
    group_ids = group_ids_by_name(group) if group else set()
    configured_record = choose_configured_project_record(
        records,
        owner,
        group_ids,
        project_id,
    )
    if configured_record is None or configured_record.project_id == project_id:
        return project_id
    print(
        "[project-reconcile] "
        f"{root_key}: state Project:{project_id} -> "
        f"configured Project:{configured_record.project_id} "
        f"owner={configured_record.owner} group={configured_record.group}"
    )
    project_state[state_scope_key(root_key, owner, group)] = (
        configured_record.project_id
    )
    return configured_record.project_id


def cleanup_obsolete_duplicate_datasets(
    project_id: int,
    keep_dataset_ids: set[int],
) -> int:
    """Delete duplicate Dataset names under one Project."""

    datasets_by_name: dict[str, list[DatasetRecord]] = {}
    for record in list_project_datasets(project_id):
        datasets_by_name.setdefault(record.name, []).append(record)

    deleted = 0
    for dataset_name, records in sorted(datasets_by_name.items()):
        if len(records) < DUPLICATE_RECORD_MIN_COUNT:
            continue
        current_records = [
            record for record in records if record.dataset_id in keep_dataset_ids
        ]
        keep_id = (
            current_records[0].dataset_id
            if current_records
            else max(record.dataset_id for record in records)
        )
        duplicates = [record for record in records if record.dataset_id != keep_id]
        print(
            "[duplicate-dataset-cleanup] "
            f"project=Project:{project_id} name={dataset_name} "
            f"keep=Dataset:{keep_id} duplicates={len(duplicates)}"
        )
        for record in duplicates:
            print(
                "[duplicate-dataset-cleanup-delete] "
                f"Dataset:{record.dataset_id} name={record.name}"
            )
            if delete_dataset_as_root(record.dataset_id):
                deleted += 1
    return deleted


def cleanup_obsolete_duplicate_projects(
    owner: str,
    group: str,
    root_prefix: str,
    source: str,
    keep_project_id: int,
) -> int:
    """Delete same-named scan-root Projects except the configured one."""

    project_name = build_project_name(root_prefix, source)
    records = list_projects_by_name(project_name)
    duplicates = [record for record in records if record.project_id != keep_project_id]
    if not duplicates:
        return 0

    deleted = 0
    print(
        "[duplicate-project-cleanup] "
        f"name={project_name} keep=Project:{keep_project_id} "
        f"configured_owner={owner} configured_group={group} "
        f"duplicates={len(duplicates)}"
    )
    for record in duplicates:
        print(
            "[duplicate-project-cleanup-delete] "
            f"Project:{record.project_id} owner={record.owner} group={record.group}"
        )
        if delete_project(record.project_id):
            deleted += 1
    return deleted


def build_dataset_name(rel_dir: str) -> str:
    return rel_dir.replace("/", " :: ")


def state_scope_key(root_key: str, owner: str, group: str) -> str:
    """Scope state by root, owner, and group so config changes do not collide."""

    return f"{root_key}|owner={owner}|group={group}"


def current_dataset_ids_for_root(
    dataset_state: dict[str, int],
    root_key: str,
    owner: str,
    group: str,
) -> set[int]:
    """Return Dataset IDs tracked for the current root owner/group placement."""

    prefix = f"{state_scope_key(root_key, owner, group)}|"
    return {
        dataset_id
        for key, dataset_id in dataset_state.items()
        if key.startswith(prefix)
    }


def imported_file_key(root_key: str, owner: str, group: str, abs_path: str) -> str:
    """Build the scoped imported-file state key for current ownership config."""

    return f"{state_scope_key(root_key, owner, group)}:{abs_path}"


def legacy_imported_file_key(root_key: str, abs_path: str) -> str:
    """Build the pre-owner/group imported-file state key."""

    return f"{root_key}:{abs_path}"


def get_or_create_project(  # noqa: PLR0913
    owner: str,
    password: str,
    group: str,
    key: str,
    root_prefix: str,
    source: str,
    project_state: dict[str, int],
) -> int:
    scoped_key = state_scope_key(key, owner, group)
    legacy_project_id = project_state.pop(key, None)
    if scoped_key in project_state:
        print(f"[project] reuse Project:{project_state[scoped_key]} for {source}")
        return project_state[scoped_key]
    if legacy_project_id is not None:
        print(
            "[project] ignoring legacy unscoped Project:"
            f"{legacy_project_id} for {source}; owner={owner} group={group}"
        )
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
    project_state[scoped_key] = project_id
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
    map_key = f"{state_scope_key(root_key, owner, group)}|{rel_dir}"
    dataset_state.pop(f"{root_key}|{rel_dir}", None)
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


def get_or_create_plate(  # noqa: PLR0913
    owner: str,
    password: str,
    group: str,
    root_key: str,
    rel_dir: str,
    plate_state: dict[str, int],
) -> int:
    """Get or create the OMERO Plate for one HCS plate folder (keyed like Dataset)."""

    map_key = f"{state_scope_key(root_key, owner, group)}|{rel_dir}"
    if map_key in plate_state:
        return plate_state[map_key]

    name = build_dataset_name(rel_dir)
    # The cache may be stale (e.g. after an interrupted run) -- check OMERO
    # directly before assuming this plate doesn't exist yet. Plate has no
    # unique database constraint on name (unlike Well's row/column), so a
    # stale-cache duplicate here fails silently instead of raising, which is
    # exactly how the Plate:7/Plate:12 duplicate happened. The scope key is
    # also stored in Plate.description so two different roots that happen to
    # share the same rel_dir (and thus the same display name) can't reuse
    # each other's Plate.
    existing_plate_id = find_plate_id_by_name(owner, password, group, name, map_key)
    if existing_plate_id is not None:
        plate_state[map_key] = existing_plate_id
        return existing_plate_id

    created = run_as_user_with_retry(
        owner,
        password,
        f"omero obj new Plate name={shlex.quote(name)} "
        f"description={shlex.quote(map_key)}",
        group,
    )
    if created.returncode != 0:
        raise RuntimeError(
            f"Failed to create plate for {rel_dir}: {created.stderr.strip()}"
        )
    plate_id = extract_object_id(created.stdout, "Plate")
    plate_state[map_key] = plate_id
    return plate_id


def find_plate_id_by_name(
    owner: str, password: str, group: str, name: str, map_key: str
) -> int | None:
    """Find an existing Plate by exact name (oldest match), or None if not found.

    Queries all Plates and filters in Python rather than embedding the name in
    the HQL WHERE clause: OMERO's HQL parser misparses names containing "::"
    (the separator this pipeline uses for folder-path-derived names) as a
    malformed filter-parameter reference. `list_projects_by_name` works around
    the same issue the same way.

    A name match alone isn't enough: two different scan roots whose rel_dir
    happens to collide would produce the same display name and could
    otherwise reuse each other's Plate. `get_or_create_plate` stores its
    `map_key` (root + owner + group + rel_dir) in Plate.description at
    creation time, so name matches are also checked against it -- unless the
    candidate predates this and has no description at all, in which case it
    falls back to the old name-only match for backward compatibility.
    """

    query = "select p.id, p.name, p.description from Plate p order by p.id"
    result = run_as_user_with_retry(
        owner, password, f"omero hql {shlex.quote(query)}", group
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if "|" not in line:
            continue
        cols = [col.strip() for col in line.split("|")]
        if len(cols) < 4 or not cols[0].isdigit() or not cols[1].isdigit():  # noqa: PLR2004
            continue
        if cols[2] != name:
            continue
        description = cols[3]
        if (
            description
            and description not in ("None", "null")
            and description != map_key
        ):
            continue
        return int(cols[1])
    return None


_AUTO_CONTRAST_SCRIPT = """
import os
import numpy as np
from omero.gateway import BlitzGateway

conn = BlitzGateway(
    os.environ["HCS_OMERO_USER"],
    os.environ["HCS_OMERO_PASSWORD"],
    host="localhost",
    port=4064,
    group=os.environ["HCS_OMERO_GROUP"],
)
conn.connect()
try:
    image = conn.getObject("Image", __IMAGE_ID__)
    pixels = image.getPrimaryPixels()
    re = conn.createRenderingEngine()
    re.lookupPixels(pixels.getId())
    if not re.lookupRenderingDef(pixels.getId()):
        re.resetDefaultSettings(True)
        re.lookupRenderingDef(pixels.getId())
    re.load()
    for c in range(image.getSizeC()):
        plane = pixels.getPlane(0, c, 0)
        lo = float(np.percentile(plane, 1.0))
        hi = float(np.percentile(plane, 99.5))
        if hi <= lo:
            hi = lo + 1
        re.setChannelWindow(c, lo, hi)
    re.saveCurrentSettings()
    print("CONTRAST_OK")
    # Settle the Thumbnail metadata row as the owning user (habomero) now,
    # while we have write permission on it. OMERO.web's default thumbnail
    # request path (direct=False) updates this row's bookkeeping on first
    # access; a non-owner group member (read-annotate permission) hitting
    # that path first gets a SecurityViolation trying to update someone
    # else's object, which OMERO.web silently turns into a blank thumbnail.
    image.getThumbnail(size=(96,), direct=False)
    print("THUMBNAIL_OK")
finally:
    conn.close()
"""


_WELL_SAMPLE_SCRIPT = """
import os
import omero.sys
import omero.rtypes
from omero.gateway import BlitzGateway
from omero.model import PlateI, WellI, WellSampleI
from omero.rtypes import rint

conn = BlitzGateway(
    os.environ["HCS_OMERO_USER"],
    os.environ["HCS_OMERO_PASSWORD"],
    host="localhost",
    port=4064,
    group=os.environ["HCS_OMERO_GROUP"],
)
conn.connect()
try:
    query = conn.getQueryService()
    update = conn.getUpdateService()
    image = conn.getObject("Image", __IMAGE_ID__)._obj

    def find_well_by_id(well_id):
        hql = (
            "select w from Well w "
            "left join fetch w.wellSamples ws left join fetch ws.image "
            "where w.id = :id"
        )
        params = omero.sys.ParametersI()
        params.addId(well_id)
        return query.findByQuery(hql, params, conn.SERVICE_OPTS)

    def find_well_by_position(plate_id, row, column):
        hql = (
            "select w from Well w "
            "left join fetch w.wellSamples ws left join fetch ws.image "
            "where w.plate.id = :plate_id and w.row = :row and w.column = :column"
        )
        params = omero.sys.ParametersI()
        params.add("plate_id", omero.rtypes.rlong(plate_id))
        params.add("row", omero.rtypes.rint(row))
        params.add("column", omero.rtypes.rint(column))
        return query.findByQuery(hql, params, conn.SERVICE_OPTS)

    well_id = __WELL_ID__
    well = find_well_by_id(well_id) if well_id else None
    if well is None:
        # The cache may be stale (e.g. after an interrupted run) -- check the DB
        # directly before assuming this well doesn't exist yet, to avoid a
        # unique-constraint violation on (plate, row, column).
        well = find_well_by_position(__PLATE_ID__, __ROW__, __COLUMN__)
    if well is None:
        well = WellI()
        well.plate = PlateI(__PLATE_ID__, False)
        well.row = rint(__ROW__)
        well.column = rint(__COLUMN__)

    sample = WellSampleI()
    sample.image = image
    sample.well = well
    well.addWellSample(sample)

    saved = update.saveAndReturnObject(well, conn.SERVICE_OPTS)
    samples = saved.copyWellSamples()
    print("WELL_ID=" + str(saved.id.val))
    print("WELL_SAMPLE_ID=" + str(samples[-1].id.val))
finally:
    conn.close()
"""


def get_or_create_well_and_add_sample(  # noqa: PLR0913
    owner: str,
    password: str,
    group: str,
    plate_id: int,
    well: str,
    image_id: int,
    well_state: dict[str, int],
) -> tuple[int, int]:
    """Get or create the Well for a plate position and append a new WellSample.

    Uses the OMERO Python API (via `run_python_as_user`) rather than `omero obj new`
    because `Well.wellSamples` is a required, non-nullable collection at creation
    time — the CLI's generic object-creation command can't populate it, so the first
    WellSample must be created together with the Well in one save. Later fields for
    the same well append to the already-loaded `wellSamples` collection and re-save.
    """

    map_key = f"{plate_id}|{well}"
    well_id = well_state.get(map_key, 0)
    script = (
        _WELL_SAMPLE_SCRIPT.replace("__IMAGE_ID__", str(image_id))
        .replace("__WELL_ID__", str(well_id))
        .replace("__PLATE_ID__", str(plate_id))
        .replace("__ROW__", str(hcs.well_row_index(well)))
        .replace("__COLUMN__", str(hcs.well_column_index(well)))
    )
    result = run_python_as_user_with_retry(owner, password, group, script)
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to create/update Well for {well} in Plate:{plate_id}: "
            f"{result.stderr.strip()}"
        )
    well_match = re.search(r"WELL_ID=(\d+)", result.stdout)
    sample_match = re.search(r"WELL_SAMPLE_ID=(\d+)", result.stdout)
    if not well_match or not sample_match:
        raise RuntimeError(f"Could not parse Well/WellSample id from: {result.stdout}")
    well_id = int(well_match.group(1))
    well_state[map_key] = well_id
    return well_id, int(sample_match.group(1))


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
        f"omero delete Dataset:{dataset_id} --wait -1",
        group,
    )
    if result.returncode == 0:
        dataset_state.pop(f"{state_scope_key(root_key, owner, group)}|{rel_dir}", None)
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


def hql_string(value: str) -> str:
    """Quote a string literal for the simple HQL queries used by this script."""

    return "'" + value.replace("'", "''") + "'"


def load_dataset_image_ids_by_name(
    owner: str,
    owner_password: str,
    shared_group: str,
    dataset_id: int,
    image_name: str,
) -> list[int]:
    """Load image IDs in a dataset matching an imported source filename."""

    cmd = (
        "omero hql "
        f'"select i.id from Image i join i.datasetLinks l '
        f'where l.parent.id = {dataset_id} and i.name = {hql_string(image_name)}"'
    )
    result = run_as_user_with_retry(owner, owner_password, cmd, shared_group)
    if result.returncode != 0:
        return []

    # `omero hql` renders results as a `|`-delimited table (` # | Col1 \n---+---\n
    # 0 | 12345 \n(1 row)`), not bracket notation — parse it the same way
    # parse_project_records/parse_dataset_records do.
    ids: list[int] = []
    for line in result.stdout.splitlines():
        if "|" not in line:
            continue
        cols = [col.strip() for col in line.split("|")]
        if len(cols) < 2 or not cols[0].isdigit() or not cols[1].isdigit():  # noqa: PLR2004
            continue
        ids.append(int(cols[1]))
    return ids


def delete_image(
    owner: str,
    owner_password: str,
    shared_group: str,
    image_id: int,
) -> bool:
    """Delete a single OMERO image and wait for deletion completion."""

    result = run_as_user_with_retry(
        owner,
        owner_password,
        f"omero delete Image:{image_id} --wait -1",
        shared_group,
    )
    return result.returncode == 0


def delete_missing_imports(  # noqa: PLR0913
    owner: str,
    owner_password: str,
    shared_group: str,
    root_key: str,
    container_root: str,
    root_files: set[str],
    imported: set[str],
    dataset_state: dict[str, int],
) -> int:
    """Remove OMERO images whose source files disappeared from a scanned root."""

    deleted = 0
    root_prefix = f"{state_scope_key(root_key, owner, shared_group)}:"
    stale_tracking_keys = sorted(
        key
        for key in imported
        if (
            key.startswith(root_prefix)
            and key.removeprefix(root_prefix) not in root_files
        )
    )
    if not stale_tracking_keys:
        return 0

    print(f"[cleanup] stale tracked files={len(stale_tracking_keys)} for {root_key}")
    affected_datasets: dict[int, str] = {}
    for tracking_key in stale_tracking_keys:
        abs_path = tracking_key.removeprefix(root_prefix)
        rel_path = rel_path_from_root(abs_path, container_root)
        rel_dir = dataset_key_for_rel_path(rel_path)
        dataset_id = dataset_state.get(
            f"{state_scope_key(root_key, owner, shared_group)}|{rel_dir}"
        )
        if dataset_id is None:
            dataset_id = dataset_state.get(f"{root_key}|{rel_dir}")
        if dataset_id is None:
            imported.remove(tracking_key)
            deleted += 1
            continue

        image_ids = load_dataset_image_ids_by_name(
            owner,
            owner_password,
            shared_group,
            dataset_id,
            Path(abs_path).name,
        )
        if not image_ids:
            imported.remove(tracking_key)
            deleted += 1
            print(f"[cleanup-missing] {rel_path}: no OMERO image found")
            continue

        deleted_all = True
        for image_id in image_ids:
            if delete_image(owner, owner_password, shared_group, image_id):
                print(f"[cleanup-deleted] {rel_path} -> Image:{image_id}")
            else:
                print(f"[cleanup-failed] {rel_path} -> Image:{image_id}")
                deleted_all = False
        if deleted_all:
            imported.remove(tracking_key)
            deleted += 1
            affected_datasets[dataset_id] = rel_dir
    if deleted:
        save_string_set(IMPORT_STATE_PATH, imported)

    delete_now_empty_datasets(
        owner, owner_password, shared_group, root_key, affected_datasets, dataset_state
    )
    return deleted


def delete_now_empty_datasets(  # noqa: PLR0913
    owner: str,
    owner_password: str,
    shared_group: str,
    root_key: str,
    affected_datasets: dict[int, str],
    dataset_state: dict[str, int],
) -> None:
    """Delete Datasets left with zero images after missing-file cleanup."""

    for dataset_id, rel_dir in sorted(affected_datasets.items()):
        remaining = load_dataset_image_names(
            owner, owner_password, shared_group, dataset_id
        )
        if remaining:
            continue
        if delete_dataset_as_root(dataset_id):
            print(f"[cleanup-empty-dataset] Dataset:{dataset_id} ({rel_dir})")
            dataset_state.pop(
                f"{state_scope_key(root_key, owner, shared_group)}|{rel_dir}", None
            )
            dataset_state.pop(f"{root_key}|{rel_dir}", None)
        else:
            print(f"[cleanup-empty-dataset-failed] Dataset:{dataset_id} ({rel_dir})")


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

    tracking_key = imported_file_key(root_key, owner, shared_group, abs_path)
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
    tracking_key = imported_file_key(root_key, owner, shared_group, abs_path)
    command = build_import_command(import_mode, dataset_id, abs_path)
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
    return False, tracking_key, result.stderr.strip()


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


@dataclass(frozen=True)
class HcsFieldGroup:
    """One (plate folder, well, field) group of channel files ready for HCS import."""

    rel_dir: str
    well: str
    field: str
    plate_id: str  # numeric plate ID parsed from the source filenames
    channel_files: dict[int, str]  # channel index -> container-absolute path


def strip_supported_extension(filename: str) -> str:
    """Remove a recognized image extension from a filename, if present."""

    lowered = filename.lower()
    for ext in SUPPORTED_EXTENSIONS:
        if lowered.endswith(ext):
            return filename[: -len(ext)]
    return filename


def partition_hcs_candidates(
    root_files: list[str], container_root: str, hcs_channels: int
) -> tuple[list[HcsFieldGroup], list[str]]:
    """Split scanned files into ready HCS field groups and everything else.

    Files matching the Thermo CX7 naming pattern are grouped by (plate folder, well,
    field). A group is returned once it has `hcs_channels` files; an incomplete group
    is silently omitted from both return values (not flat-imported, not marked
    processed) so it's naturally reconsidered on a later scan once complete. Files
    that don't match the HCS pattern at all continue through the existing flat
    per-file import path unchanged.
    """

    groups: dict[tuple[str, str, str], dict[int, str]] = {}
    plate_ids: dict[tuple[str, str, str], str] = {}
    flat_files: list[str] = []
    for abs_path in root_files:
        rel_path = rel_path_from_root(abs_path, container_root)
        stem = strip_supported_extension(Path(rel_path).name)
        info = hcs.parse_filename(stem)
        if info is None:
            flat_files.append(abs_path)
            continue
        rel_dir = dataset_key_for_rel_path(rel_path)
        key = (rel_dir, info.well, info.field)
        groups.setdefault(key, {})[info.channel] = abs_path
        plate_ids[key] = info.plate_id

    ready: list[HcsFieldGroup] = []
    for (rel_dir, well, field), channel_files in groups.items():
        if len(channel_files) >= hcs_channels:
            ready.append(
                HcsFieldGroup(
                    rel_dir,
                    well,
                    field,
                    plate_ids[(rel_dir, well, field)],
                    channel_files,
                )
            )
    return ready, flat_files


def _shadow_container_path(host_path: Path) -> str:
    """Translate a host-side path under HCS_SHADOW_ROOT to its container path."""

    return f"/OMERO/hcs_shadow/{host_path.relative_to(HCS_SHADOW_ROOT).as_posix()}"


def ensure_hcs_shadow_root_writable() -> None:
    """Bootstrap HCS_SHADOW_ROOT with host-writable ownership, once.

    `data/omero` is bind-mounted into the omero-server container and owned by its
    internal service user, so the host-side process can't create directories under
    it directly. This creates just the `hcs_shadow` subtree (not the rest of
    `data/omero`, which is live OMERO-managed storage) with ownership fixed to the
    current host user/group, via a throwaway container run — mirroring the same
    pattern `scan_dirs.materialize_scan_roots` already uses for `data/state`.
    """

    if HCS_SHADOW_ROOT.exists():
        return
    uid = os.getuid()
    gid = os.getgid()
    fix = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            "0:0",
            "-v",
            f"{HCS_SHADOW_ROOT.parent}:/data",
            "postgres:16",
            "sh",
            "-lc",
            f"mkdir -p /data/{HCS_SHADOW_ROOT.name} && "
            f"chown -R {uid}:{gid} /data/{HCS_SHADOW_ROOT.name}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if fix.returncode != 0:
        detail = fix.stderr.strip() or fix.stdout.strip() or "auto-fix failed"
        raise PermissionError(
            f"Cannot create {HCS_SHADOW_ROOT} (owned by the OMERO container). "
            f"Auto-fix failed: {detail}. "
            f"Fix manually with: sudo mkdir -p {HCS_SHADOW_ROOT} && "
            f"sudo chown -R {uid}:{gid} {HCS_SHADOW_ROOT}"
        ) from None


def read_container_file_bytes(container_path: str) -> bytes:
    """Read a file's raw bytes from inside the OMERO server container.

    Used instead of reading source files directly from the host filesystem: this
    CIFS-backed mount has been observed to deny direct host-process reads of
    individual files (even though `find`-based scanning and container-side reads of
    the same files work fine) — an environment quirk, not something worth working
    around by touching host-side file permissions.
    """

    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "omero-server", "cat", container_path],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to read {container_path} from omero-server container: "
            f"{result.stderr.decode(errors='replace').strip()}"
        )
    return result.stdout


def import_hcs_field(  # noqa: PLR0913
    owner: str,
    password: str,
    group: str,
    root_key: str,
    container_root: str,
    group_info: HcsFieldGroup,
) -> int:
    """Build a companion file for one field's channels and import it as one Image.

    Returns the new multi-channel Image id. Uses `--transfer=ln_s` throughout, same
    as the rest of this pipeline — no pixel data is copied.
    """

    real_plate_dir = f"{container_root}/{group_info.rel_dir}"
    ensure_hcs_shadow_root_writable()
    plate_dir = hcs.ensure_shadow_symlink(
        HCS_SHADOW_ROOT, root_key, group_info.rel_dir, real_plate_dir
    )

    ordered_channels = sorted(group_info.channel_files)
    reference_container_path = group_info.channel_files[ordered_channels[0]]
    reference_bytes = read_container_file_bytes(reference_container_path)
    width, height, bits_per_sample, sample_format = hcs.parse_tiff_geometry(
        reference_bytes, label=reference_container_path
    )
    pixel_type = hcs.ome_pixel_type(bits_per_sample, sample_format)

    relative_paths = [
        f"source/{Path(group_info.channel_files[c]).name}" for c in ordered_channels
    ]
    image_name = f"{group_info.plate_id}_{group_info.well}_{group_info.field}"
    xml_text = hcs.generate_companion_xml(
        image_name, width, height, pixel_type, relative_paths
    )
    # Must live alongside `source` (not in a subdirectory): companion FileName
    # references are resolved relative to the companion file's own directory, and
    # `..` parent-traversal in that path breaks checksum verification (see
    # hcs.generate_companion_xml docstring).
    companion_path = plate_dir / f"{group_info.well}_{group_info.field}.companion.ome"
    companion_path.write_text(xml_text, encoding="utf-8")

    companion_container_path = shlex.quote(_shadow_container_path(companion_path))
    command = f"omero import --transfer=ln_s {companion_container_path}"
    result = run_as_user_with_retry(owner, password, command, group)
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to import HCS field {image_name}: {result.stderr.strip()}"
        )
    image_id = extract_object_id(result.stdout, "Image")

    # Bio-Formats' OME-TIFF companion reader names the Image after the companion
    # file itself, ignoring the <Image Name="..."> attribute — rename it after
    # import so it reads as "B02_f00" rather than "B02_f00.companion.ome".
    rename = run_as_user_with_retry(
        owner, password, f"omero obj update Image:{image_id} name={image_name}", group
    )
    if rename.returncode != 0:
        print(f"[hcs-rename-failed] Image:{image_id}: {rename.stderr.strip()}")

    contrast_script = _AUTO_CONTRAST_SCRIPT.replace("__IMAGE_ID__", str(image_id))
    contrast = run_python_as_user_with_retry(owner, password, group, contrast_script)
    if contrast.returncode != 0:
        print(f"[hcs-contrast-failed] Image:{image_id}: {contrast.stderr.strip()}")

    return image_id


def hcs_field_state_key(  # noqa: PLR0913
    root_key: str, owner: str, group: str, rel_dir: str, well: str, field: str
) -> str:
    """Build the tracking key for one imported HCS field (well+field within a plate)."""

    return f"{state_scope_key(root_key, owner, group)}|{rel_dir}:{well}:{field}"


def cleanup_old_flat_field_images(  # noqa: PLR0913
    owner: str,
    password: str,
    group: str,
    root_key: str,
    rel_dir: str,
    channel_files: dict[int, str],
    imported: set[str],
    dataset_state: dict[str, int],
) -> None:
    """Delete pre-existing flat single-channel Images now superseded by an HCS Image.

    Looks up the flat Dataset for this exact plate folder (read-only — does not
    create one) and removes any old per-channel Images plus their tracking entries,
    so already-imported plates converge to the same Plate/Well structure as new
    data. If the flat Dataset ends up empty, `delete_now_empty_datasets` catches it.
    """

    dataset_id = dataset_state.get(
        f"{state_scope_key(root_key, owner, group)}|{rel_dir}"
    )
    if dataset_id is None:
        return

    affected_datasets: dict[int, str] = {}
    for abs_path in channel_files.values():
        filename = Path(abs_path).name
        image_ids = load_dataset_image_ids_by_name(
            owner, password, group, dataset_id, filename
        )
        for image_id in image_ids:
            if delete_image(owner, password, group, image_id):
                print(f"[hcs-old-flat-cleanup] {filename} -> Image:{image_id}")
                affected_datasets[dataset_id] = rel_dir
            else:
                print(f"[hcs-old-flat-cleanup-failed] {filename} -> Image:{image_id}")
        tracking_key = imported_file_key(root_key, owner, group, abs_path)
        imported.discard(tracking_key)

    delete_now_empty_datasets(
        owner, password, group, root_key, affected_datasets, dataset_state
    )


def process_hcs_candidates(  # noqa: PLR0913
    owner: str,
    owner_password: str,
    shared_group: str,
    root_key: str,
    container_root: str,
    hcs_channels: int,
    root_files: list[str],
    imported: set[str],
    dataset_state: dict[str, int],
    hcs_imported: set[str],
    hcs_plate_state: dict[str, int],
    hcs_well_state: dict[str, int],
) -> list[str]:
    """Import HCS-pattern fields as Plate/Well structure; return the remaining files.

    Ready (well, field) groups are imported as one multi-channel Image each and
    linked into Plate/Well/WellSample structure; any pre-existing flat
    single-channel Images for the same files are deleted so already-imported plates
    converge to the same structure as new data. Files that don't match the Thermo
    CX7 naming pattern, and incomplete HCS groups (waiting on more channels), pass
    through unchanged for the existing flat per-file import path.
    """

    if hcs_channels <= 0:
        return root_files

    ready_groups, flat_files = partition_hcs_candidates(
        root_files, container_root, hcs_channels
    )
    for group_info in ready_groups:
        field_key = hcs_field_state_key(
            root_key,
            owner,
            shared_group,
            group_info.rel_dir,
            group_info.well,
            group_info.field,
        )
        if field_key in hcs_imported:
            continue
        plate_id = get_or_create_plate(
            owner,
            owner_password,
            shared_group,
            root_key,
            group_info.rel_dir,
            hcs_plate_state,
        )
        image_id = import_hcs_field(
            owner,
            owner_password,
            shared_group,
            root_key,
            container_root,
            group_info,
        )
        get_or_create_well_and_add_sample(
            owner,
            owner_password,
            shared_group,
            plate_id,
            group_info.well,
            image_id,
            hcs_well_state,
        )
        hcs_imported.add(field_key)
        # Checkpoint after every field (not just per-root, like dataset/project
        # state): an interrupted run must not lose track of a Plate/Well already
        # created in OMERO, or the next run will hit a unique-constraint violation
        # trying to recreate it.
        save_string_set(HCS_FIELD_STATE_PATH, hcs_imported)
        save_int_map(HCS_PLATE_STATE_PATH, hcs_plate_state)
        save_int_map(HCS_WELL_STATE_PATH, hcs_well_state)
        print(
            f"[hcs-import] {group_info.rel_dir} {group_info.well}{group_info.field} "
            f"-> Plate:{plate_id} Image:{image_id}"
        )
        cleanup_old_flat_field_images(
            owner,
            owner_password,
            shared_group,
            root_key,
            group_info.rel_dir,
            group_info.channel_files,
            imported,
            dataset_state,
        )
    return flat_files


def import_root_files(  # noqa: PLR0913, C901, PLR0912, PLR0915
    owner: str,
    owner_password: str,
    shared_group: str,
    import_mode: str,
    root_key: str,
    source_root: str,
    container_root: str,
    hcs_channels: int,
    hcs_imported: set[str],
    hcs_plate_state: dict[str, int],
    hcs_well_state: dict[str, int],
    project_id: int,
    imported: set[str],
    dataset_state: dict[str, int],
    budget_remaining: int,
    max_failures_per_run: int,
    sleep_between_imports_seconds: int,
    scan_progress_every_paths: int,
    import_progress_every_files: int,
    import_workers: int,
    delete_omero_missing_files: bool,
    reimport_legacy_import_state: bool,
) -> tuple[int, dict[str, str], bool, dict[str, int]]:
    """Import files for a single root. Returns count, failures, hit_cap."""

    imported_count = 0
    budget_capped = budget_remaining > 0
    skipped_existing_count = 0
    skipped_tracked_count = 0
    deleted_missing_count = 0
    failures: dict[str, str] = {}
    dataset_image_cache: dict[int, set[str]] = {}
    root_files: list[str] | None = None
    for attempt in range(1, LIST_RETRY_ATTEMPTS + 1):
        try:
            root_files = list_root_files(
                source_root,
                container_root,
                max_files=budget_remaining if budget_capped else None,
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
                "deleted_missing": 0,
            },
        )
    print(
        f"[root] {source_root}: candidate files={len(root_files)} "
        f"budget={budget_remaining if budget_capped else 'uncapped'}"
    )
    root_files = process_hcs_candidates(
        owner,
        owner_password,
        shared_group,
        root_key,
        container_root,
        hcs_channels,
        root_files,
        imported,
        dataset_state,
        hcs_imported,
        hcs_plate_state,
        hcs_well_state,
    )
    root_file_set = set(root_files)
    if delete_omero_missing_files:
        deleted_missing_count = delete_missing_imports(
            owner,
            owner_password,
            shared_group,
            root_key,
            container_root,
            root_file_set,
            imported,
            dataset_state,
        )

    # Build worklist serially so dataset/project bookkeeping stays consistent.
    work: list[tuple[str, int]] = []
    for abs_path in root_files:
        if budget_capped and imported_count >= budget_remaining:
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
                    "deleted_missing": deleted_missing_count,
                },
            )
        tracking_key = imported_file_key(root_key, owner, shared_group, abs_path)
        legacy_tracking_key = legacy_imported_file_key(root_key, abs_path)
        if imported_count % 10 == 0:
            print(f"[import-candidate] {abs_path}")
        if tracking_key in imported:
            skipped_tracked_count += 1
            continue
        if legacy_tracking_key in imported:
            if reimport_legacy_import_state:
                print(
                    "[state-reimport] legacy import state exists; rechecking under "
                    f"owner={owner} group={shared_group}: {abs_path}"
                )
            else:
                print(
                    "[state-migrate] legacy import state migrated without reimport "
                    f"for owner={owner} group={shared_group}: {abs_path}"
                )
                imported.add(tracking_key)
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
                            "deleted_missing": deleted_missing_count,
                        },
                    )
                continue

            imported.add(tracked)
            # Persist immediately so interrupted runs don't replay this file.
            save_string_set(IMPORT_STATE_PATH, imported)
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
                "deleted_missing": deleted_missing_count,
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
                            "deleted_missing": deleted_missing_count,
                        },
                    )
                continue
            imported.add(tracked)
            # Persist immediately so interrupted runs don't replay this file.
            save_string_set(IMPORT_STATE_PATH, imported)
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
            "deleted_missing": deleted_missing_count,
        },
    )


def import_files() -> None:  # noqa: C901, PLR0912, PLR0915
    credentials, fallback_owner = load_user_credentials()
    default_import_user = load_default_import_user() or fallback_owner
    if default_import_user not in credentials:
        raise ImportConfigError(
            f"Configured import_user is not present in users.yml: {default_import_user}"
        )
    reimport_legacy_import_state = load_reimport_legacy_import_state()
    cleanup_duplicates = load_cleanup_obsolete_duplicate_projects()
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
        delete_omero_missing_files,
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
    hcs_imported = load_string_set(HCS_FIELD_STATE_PATH)
    hcs_plate_state = load_int_map(HCS_PLATE_STATE_PATH)
    hcs_well_state = load_int_map(HCS_WELL_STATE_PATH)

    imported_count = 0
    failures: dict[str, str] = {}
    total_candidates = 0
    total_queued = 0
    total_skipped_tracked = 0
    total_skipped_existing = 0
    total_deleted_missing = 0
    total_deleted_duplicate_projects = 0
    total_deleted_duplicate_datasets = 0
    interrupted = False
    try:
        for root_key, root_data in sorted(roots.items()):
            source = root_data["source"]
            container_root = root_data["container_root"]
            root_group = root_data.get("group", shared_group)
            owner = root_data.get("import_user", default_import_user)
            hcs_enabled = (
                root_data.get("hcs_enabled", "true").strip().lower() != "false"
            )
            hcs_channels = (
                int(root_data["hcs_channels"])
                if hcs_enabled and root_data.get("hcs_channels")
                else 0
            )
            if owner not in credentials:
                raise ImportConfigError(
                    f"Configured root import_user is not present in users.yml: {owner}"
                )
            owner_password = credentials[owner]
            print(f"[root-begin] {root_key} source={source}")
            print(f"[root-begin] {root_key} container_root={container_root}")
            print(f"[root-begin] {root_key} import_user={owner}")
            if root_group:
                print(f"[root-begin] {root_key} group={root_group}")
            try:
                project_id = get_or_create_project(
                    owner,
                    owner_password,
                    root_group,
                    root_key,
                    root_prefix,
                    source,
                    project_state,
                )
            except RuntimeError as exc:
                print(f"[root-failed] {source}: {exc}")
                continue
            project_id = reconcile_project_id(
                owner,
                root_group,
                root_key,
                root_prefix,
                source,
                project_id,
                project_state,
            )

            budget_remaining = (
                max_files_per_run - imported_count if max_files_per_run > 0 else 0
            )
            root_imported, root_failures, hit_cap, root_stats = import_root_files(
                owner,
                owner_password,
                root_group,
                import_mode,
                root_key,
                source,
                container_root,
                hcs_channels,
                hcs_imported,
                hcs_plate_state,
                hcs_well_state,
                project_id,
                imported,
                dataset_state,
                budget_remaining,
                max_failures_per_run,
                sleep_between_imports_seconds,
                scan_progress_every_paths,
                import_progress_every_files,
                import_workers,
                delete_omero_missing_files,
                reimport_legacy_import_state,
            )
            imported_count += root_imported
            failures.update(root_failures)
            total_candidates += root_stats["candidates"]
            total_queued += root_stats["queued"]
            total_skipped_tracked += root_stats["skipped_tracked"]
            total_skipped_existing += root_stats["skipped_existing"]
            total_deleted_missing += root_stats["deleted_missing"]
            print(
                f"[root-end] {root_key} imported_this_root={root_imported} "
                f"failures_this_root={len(root_failures)} "
                f"candidates={root_stats['candidates']} "
                f"queued={root_stats['queued']} "
                f"skipped_tracked={root_stats['skipped_tracked']} "
                f"skipped_existing={root_stats['skipped_existing']} "
                f"deleted_missing={root_stats['deleted_missing']}"
            )
            if cleanup_duplicates and not root_failures and not hit_cap:
                total_deleted_duplicate_projects += cleanup_obsolete_duplicate_projects(
                    owner,
                    root_group,
                    root_prefix,
                    source,
                    project_id,
                )
                total_deleted_duplicate_datasets += cleanup_obsolete_duplicate_datasets(
                    project_id,
                    current_dataset_ids_for_root(
                        dataset_state,
                        root_key,
                        owner,
                        root_group,
                    ),
                )
            elif cleanup_duplicates:
                print(
                    "[duplicate-project-cleanup-skip] "
                    f"{root_key}: root did not finish cleanly "
                    f"hit_cap={hit_cap} failures={len(root_failures)}"
                )
            # Checkpoint state per root so restart can't replay completed root work.
            save_string_set(IMPORT_STATE_PATH, imported)
            save_int_map(DATASET_STATE_PATH, dataset_state)
            save_int_map(PROJECT_STATE_PATH, project_state)
            save_int_map(HCS_PLATE_STATE_PATH, hcs_plate_state)
            save_int_map(HCS_WELL_STATE_PATH, hcs_well_state)
            save_failure_map(FAILURE_STATE_PATH, failures)
            if hit_cap:
                break
    except KeyboardInterrupt:
        interrupted = True
        print("[import] interrupted, checkpointing state before exit")
    finally:
        save_string_set(IMPORT_STATE_PATH, imported)
        save_int_map(DATASET_STATE_PATH, dataset_state)
        save_int_map(PROJECT_STATE_PATH, project_state)
        save_int_map(HCS_PLATE_STATE_PATH, hcs_plate_state)
        save_int_map(HCS_WELL_STATE_PATH, hcs_well_state)
        save_failure_map(FAILURE_STATE_PATH, failures)
    imported_after = len(imported)
    print(
        "[run-summary] "
        f"candidates={total_candidates} queued={total_queued} "
        f"imported_new={imported_count} skipped_tracked={total_skipped_tracked} "
        f"skipped_existing={total_skipped_existing} failures={len(failures)} "
        f"deleted_missing={total_deleted_missing} tracked_before={imported_before} "
        f"deleted_duplicate_projects={total_deleted_duplicate_projects} "
        f"deleted_duplicate_datasets={total_deleted_duplicate_datasets} "
        f"tracked_after={imported_after}"
    )
    if interrupted:
        return
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
