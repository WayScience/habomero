"""Import configured scan directory content into OMERO automatically."""

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
IMPORT_STATE_PATH = PROJECT_ROOT / "data/state/imported_files.txt"
DATASET_STATE_PATH = PROJECT_ROOT / "data/state/path_datasets.yml"
SUPPORTED_EXTENSIONS = {".tif", ".tiff", ".ome.tif", ".ome.tiff"}
SCAN_ROOT = "/scan/inbox"


class ImportConfigError(ValueError):
    """Raised for invalid import configuration."""


def run_in_omero(command: str) -> subprocess.CompletedProcess[str]:
    """Run a command inside the omero-server container."""

    return subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "omero-server",
            "bash",
            "-lc",
            command,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def wait_for_server(max_attempts: int = 40, interval_seconds: int = 3) -> None:
    """Wait until OMERO server CLI responds."""

    cmd = 'export PATH="/opt/omero/server/venv3/bin:$PATH"; omero version >/dev/null'
    for _ in range(max_attempts):
        result = run_in_omero(cmd)
        if result.returncode == 0:
            return
        time.sleep(interval_seconds)

    raise RuntimeError("OMERO server did not become ready in time")


def load_user_credentials() -> tuple[dict[str, str], str]:
    """Return a username->password map and the first configured username."""

    if not USERS_CONFIG_PATH.exists():
        raise FileNotFoundError(f"User config is missing: {USERS_CONFIG_PATH}")

    payload = yaml.safe_load(USERS_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    users = payload.get("users", [])
    if not isinstance(users, list) or not users:
        raise ImportConfigError("config/omero/users.yml must define at least one user")

    credentials: dict[str, str] = {}
    first_username = ""
    for index, item in enumerate(users):
        if not isinstance(item, dict):
            raise ImportConfigError("Each user entry must be a mapping")

        username = item.get("username", "")
        password = item.get("password", "")
        if not isinstance(username, str) or not username.strip():
            raise ImportConfigError("Each user requires a non-empty username")
        if not isinstance(password, str) or not password.strip():
            raise ImportConfigError(
                "Each user requires a non-empty password for automatic import"
            )

        username = username.strip()
        credentials[username] = password.strip()
        if index == 0:
            first_username = username

    return credentials, first_username


def load_path_routes(credentials: dict[str, str]) -> list[dict[str, str]]:
    """Load optional path prefix to user routes from scan config."""

    if not SCAN_CONFIG_PATH.exists():
        return []

    payload = yaml.safe_load(SCAN_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    routes = payload.get("path_user_routes", [])
    if routes in (None, ""):
        return []
    if not isinstance(routes, list):
        raise ImportConfigError("path_user_routes must be a list")

    normalized: list[dict[str, str]] = []
    for entry in routes:
        if not isinstance(entry, dict):
            raise ImportConfigError("Each path_user_routes entry must be a mapping")

        prefix = entry.get("prefix", "")
        username = entry.get("username", "")
        if not isinstance(prefix, str) or not prefix.strip():
            raise ImportConfigError("Route prefix must be a non-empty string")
        if not isinstance(username, str) or not username.strip():
            raise ImportConfigError("Route username must be a non-empty string")

        normalized_prefix = prefix.strip().strip("/")
        normalized_username = username.strip()
        if normalized_username not in credentials:
            raise ImportConfigError(
                f"Route username '{normalized_username}' not found in users.yml"
            )

        normalized.append(
            {
                "prefix": normalized_prefix,
                "username": normalized_username,
            }
        )

    return sorted(normalized, key=lambda x: len(x["prefix"]), reverse=True)


def list_scan_files() -> list[str]:
    """List importable files from the mounted scan directory in the container."""

    cmd = f"find {SCAN_ROOT} -type f"
    result = run_in_omero(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to list scan files: {result.stderr.strip()}")

    files = []
    for line in result.stdout.splitlines():
        path = line.strip()
        if not path:
            continue
        lowered = path.lower()
        if any(lowered.endswith(ext) for ext in SUPPORTED_EXTENSIONS):
            files.append(path)
    return sorted(files)


def rel_scan_path(path: str) -> str:
    """Return path relative to scan root."""

    return path.removeprefix(f"{SCAN_ROOT}/")


def directory_key(relative_path: str) -> str:
    """Map a relative file path to its directory key."""

    parent = str(Path(relative_path).parent)
    return "." if parent == "." else parent


def pick_user_for_path(
    rel_dir: str, routes: list[dict[str, str]], fallback: str
) -> str:
    """Pick import owner user from longest matching prefix route."""

    for route in routes:
        prefix = route["prefix"]
        if rel_dir == prefix or rel_dir.startswith(f"{prefix}/"):
            return route["username"]
    return fallback


def load_imported_state() -> set[str]:
    """Load previously imported file paths."""

    if not IMPORT_STATE_PATH.exists():
        return set()
    return {
        line.strip()
        for line in IMPORT_STATE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def save_imported_state(imported: set[str]) -> None:
    """Persist imported-file state."""

    IMPORT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    IMPORT_STATE_PATH.write_text("\n".join(sorted(imported)) + "\n", encoding="utf-8")


def load_dataset_state() -> dict[str, int]:
    """Load path->dataset id cache."""

    if not DATASET_STATE_PATH.exists():
        return {}

    payload = yaml.safe_load(DATASET_STATE_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        return {}

    parsed: dict[str, int] = {}
    for key, value in payload.items():
        if isinstance(key, str) and isinstance(value, int):
            parsed[key] = value
    return parsed


def save_dataset_state(state: dict[str, int]) -> None:
    """Persist path->dataset id cache."""

    DATASET_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATASET_STATE_PATH.write_text(
        yaml.safe_dump(state, sort_keys=True),
        encoding="utf-8",
    )


def run_as_user(
    username: str, password: str, command: str
) -> subprocess.CompletedProcess[str]:
    """Run an OMERO command authenticated as a specific user."""

    login_user = shlex.quote(username)
    login_password = shlex.quote(password)
    full_command = (
        "set -euo pipefail; "
        'export PATH="/opt/omero/server/venv3/bin:$PATH"; '
        f"omero login {login_user}@localhost:4064 -w {login_password} >/dev/null; "
        f"{command}"
    )
    return run_in_omero(full_command)


def build_dataset_name(rel_dir: str) -> str:
    """Construct a stable dataset name from a scan-relative directory."""

    cleaned = "root" if rel_dir == "." else rel_dir.replace("/", "__")
    return f"scan::{cleaned}"


def extract_dataset_id(output: str) -> int:
    """Extract Dataset ID from `omero obj new Dataset ...` output."""

    match = re.search(r"Dataset:(\d+)", output)
    if not match:
        raise RuntimeError(f"Could not parse dataset id from output: {output}")
    return int(match.group(1))


def get_or_create_dataset(
    username: str,
    password: str,
    rel_dir: str,
    dataset_state: dict[str, int],
) -> int:
    """Return dataset id for this user/path bucket, creating if needed."""

    key = f"{username}|{rel_dir}"
    existing = dataset_state.get(key)
    if existing is not None:
        return existing

    dataset_name = shlex.quote(build_dataset_name(rel_dir))
    create = run_as_user(
        username, password, f"omero obj new Dataset name={dataset_name}"
    )
    if create.returncode != 0:
        raise RuntimeError(f"Failed to create dataset: {create.stderr.strip()}")

    dataset_id = extract_dataset_id(create.stdout)
    dataset_state[key] = dataset_id
    save_dataset_state(dataset_state)
    return dataset_id


def import_bucket(
    username: str,
    password: str,
    rel_dir: str,
    paths: list[str],
    dataset_state: dict[str, int],
) -> None:
    """Import one directory bucket to its dataset as the selected user."""

    dataset_id = get_or_create_dataset(username, password, rel_dir, dataset_state)
    file_args = " ".join(shlex.quote(path) for path in paths)
    command = f"omero import -d {dataset_id} {file_args}"
    result = run_as_user(username, password, command)
    if result.returncode != 0:
        raise RuntimeError(f"Import failed for '{rel_dir}': {result.stderr.strip()}")


def import_files() -> None:
    """Route and import scan files by directory path."""

    credentials, fallback_user = load_user_credentials()
    routes = load_path_routes(credentials)
    all_files = list_scan_files()

    if not all_files:
        print("No importable files found under /scan/inbox")
        return

    imported = load_imported_state()
    pending = [path for path in all_files if path not in imported]
    if not pending:
        print("No new files to import")
        return

    buckets: dict[tuple[str, str], list[str]] = {}
    for abs_path in pending:
        rel_path = rel_scan_path(abs_path)
        rel_dir = directory_key(rel_path)
        username = pick_user_for_path(rel_dir, routes, fallback_user)
        bucket_key = (username, rel_dir)
        buckets.setdefault(bucket_key, []).append(abs_path)

    dataset_state = load_dataset_state()
    imported_count = 0
    for (username, rel_dir), paths in sorted(buckets.items()):
        password = credentials[username]
        import_bucket(username, password, rel_dir, sorted(paths), dataset_state)
        imported.update(paths)
        imported_count += len(paths)
        print(f"Imported {len(paths)} file(s) for user '{username}' from '{rel_dir}'")

    save_imported_state(imported)
    print(f"Imported {imported_count} new file(s) total")


def main() -> None:
    """Wait for server, then import new scan files grouped by path."""

    wait_for_server()
    import_files()


if __name__ == "__main__":
    main()
