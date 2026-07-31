"""Import configured multi-root scan directories into OMERO."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
USERS_CONFIG_PATH = PROJECT_ROOT / "config/omero/users.yml"
SCAN_CONFIG_PATH = PROJECT_ROOT / "config/omero/scan_dirs.yml"
ENV_PATH = PROJECT_ROOT / ".env"
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
PROJECT_RECORD_COLUMN_COUNT = 5
DATASET_RECORD_COLUMN_COUNT = 3
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
            if isinstance(source, str) and isinstance(container_root, str):
                result[key] = {"source": source, "container_root": container_root}
                if isinstance(group, str) and group.strip():
                    result[key]["group"] = group.strip()
                if isinstance(import_user, str) and import_user.strip():
                    result[key]["import_user"] = import_user.strip()
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
    result = run_as_root(f"hql {shlex.quote(query)}")
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

    result = run_as_root(f"delete Project:{project_id} -w --no-wait")
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
    result = run_as_root(f"hql {shlex.quote(query)}")
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        print(f"[duplicate-dataset-cleanup-warn] list failed: {detail}")
        return []
    return parse_dataset_records(result.stdout)


def delete_dataset_as_root(dataset_id: int) -> bool:
    """Delete an obsolete duplicate OMERO Dataset."""

    result = run_as_root(f"delete Dataset:{dataset_id} -w --no-wait")
    if result.returncode == 0:
        return True
    detail = result.stderr.strip() or result.stdout.strip()
    print(f"[duplicate-dataset-cleanup-failed] Dataset:{dataset_id}: {detail}")
    return False


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

    ids: list[int] = []
    for match in re.finditer(r"\[(\d+)\]", result.stdout):
        ids.append(int(match.group(1)))
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
        f"omero delete Image:{image_id} -w --no-wait",
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
    if deleted:
        save_string_set(IMPORT_STATE_PATH, imported)
    return deleted


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
