"""Import configured multi-root scan directories into OMERO."""

from __future__ import annotations

import re
import shlex
import subprocess
import time
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


def load_import_config() -> tuple[str, str, str, int, int, int, int, int]:
    shared_group = ""
    root_prefix = "scan-root"
    import_mode = "copy"
    max_files_per_run = MAX_FILES_PER_RUN
    db_stable_checks = DB_STABLE_CHECKS_REQUIRED
    db_stable_interval = DB_STABLE_CHECK_INTERVAL_SECONDS
    max_failures_per_run = MAX_FAILURES_PER_RUN
    sleep_between_files = SLEEP_BETWEEN_IMPORTS_SECONDS
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
    return (
        shared_group,
        root_prefix,
        import_mode,
        max_files_per_run,
        db_stable_checks,
        db_stable_interval,
        max_failures_per_run,
        sleep_between_files,
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


def list_root_files(source_root: str, container_root: str) -> list[str]:
    """List files from host source path, mapped to container-root paths."""

    source_path = Path(source_root)
    if not source_path.exists() or not source_path.is_dir():
        raise RuntimeError(f"Source root is not available on host: {source_root}")

    print(f"[scan] source_root={source_root}")
    print(f"[scan] container_root={container_root}")
    print(f"[scan] indexing files under {source_root}")
    files: list[str] = []
    scanned = 0
    for path in source_path.rglob("*"):
        scanned += 1
        if scanned % 100 == 0:
            print(f"[scan] visited={scanned} matched={len(files)}")
        if not path.is_file():
            continue
        lowered = path.name.lower()
        if not any(lowered.endswith(ext) for ext in SUPPORTED_EXTENSIONS):
            continue
        rel = path.relative_to(source_path).as_posix()
        files.append(f"{container_root.rstrip('/')}/{rel}")
    print(f"[scan] completed visited={scanned} matched={len(files)}")
    return sorted(files)


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
    if link.returncode != 0 and "already" not in link.stderr.lower():
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


def build_import_command(import_mode: str, dataset_id: int, abs_path: str) -> str:
    """Build OMERO import command for configured transfer mode."""

    transfer_args = "--transfer=ln_s " if import_mode == "inplace" else ""
    return f"omero import {transfer_args}-d {dataset_id} {shlex.quote(abs_path)}"


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


def import_root_files(  # noqa: PLR0913, C901
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
) -> tuple[int, dict[str, str], bool]:
    """Import files for a single root. Returns count, failures, hit_cap."""

    imported_count = 0
    failures: dict[str, str] = {}
    root_files: list[str] | None = None
    for attempt in range(1, LIST_RETRY_ATTEMPTS + 1):
        try:
            root_files = list_root_files(source_root, container_root)
            break
        except RuntimeError as exc:
            print(
                f"[retry {attempt}/{LIST_RETRY_ATTEMPTS}] transient list failure "
                f"for {source_root}: {exc}"
            )
            time.sleep(LIST_RETRY_BACKOFF_SECONDS * attempt)
    if root_files is None:
        print(f"[root-list-failed] {source_root}")
        return 0, {}, False
    print(
        f"[root] {source_root}: candidate files={len(root_files)} "
        f"budget={budget_remaining}"
    )

    for abs_path in root_files:
        if imported_count >= budget_remaining:
            print(f"Reached max_files_per_run budget for this run ({budget_remaining})")
            return imported_count, failures, True
        tracking_key = f"{root_key}:{abs_path}"
        if imported_count % 10 == 0:
            print(f"[import-candidate] {abs_path}")
        if tracking_key in imported:
            continue
        ok, tracked, error = import_one_file(
            owner,
            owner_password,
            shared_group,
            import_mode,
            root_key,
            project_id,
            container_root,
            abs_path,
            dataset_state,
        )
        if not ok:
            failures[tracked] = error
            rel_path = rel_path_from_root(abs_path, container_root)
            last_line = error.splitlines()[-1] if error else "unknown error"
            print(f"[failed] {rel_path}: {last_line}")
            if len(failures) >= max_failures_per_run:
                print(
                    f"Reached failure cap ({max_failures_per_run}), stopping import run"
                )
                return imported_count, failures, True
            continue

        imported.add(tracked)
        imported_count += 1
        if imported_count % 5 == 0:
            print(f"[root-progress] imported={imported_count} for {source_root}")
        time.sleep(sleep_between_imports_seconds)
        if sleep_between_imports_seconds > 0:
            print(f"[pace] slept {sleep_between_imports_seconds}s")
    return imported_count, failures, False


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
    ) = load_import_config()
    wait_for_db_stable(db_stable_checks, db_stable_interval)

    roots = load_scan_roots()
    if not roots:
        print("No scan roots configured")
        return
    print(f"[import] loaded roots={len(roots)} mode={import_mode}")

    imported = load_string_set(IMPORT_STATE_PATH)
    dataset_state = load_int_map(DATASET_STATE_PATH)
    project_state = load_int_map(PROJECT_STATE_PATH)

    imported_count = 0
    failures: dict[str, str] = {}
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

        root_imported, root_failures, hit_cap = import_root_files(
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
        )
        imported_count += root_imported
        failures.update(root_failures)
        print(
            f"[root-end] {root_key} imported_this_root={root_imported} "
            f"failures_this_root={len(root_failures)}"
        )
        if hit_cap:
            break

    save_string_set(IMPORT_STATE_PATH, imported)
    save_int_map(DATASET_STATE_PATH, dataset_state)
    save_int_map(PROJECT_STATE_PATH, project_state)
    save_failure_map(FAILURE_STATE_PATH, failures)
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
