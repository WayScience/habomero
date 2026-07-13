"""Safely restart the OMERO stack while preserving existing data."""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCAN_ROOTS_COMPOSE = PROJECT_ROOT / "data/state/scan_roots.compose.yml"
OMERO_REPOSITORY_ROOT = PROJECT_ROOT / "data/omero/.omero/repository"
BACKUP_DIR = PROJECT_ROOT / "data/backups"
ENV_PATH = PROJECT_ROOT / ".env"
DB_WAIT_SECONDS = 60
DB_WAIT_INTERVAL_SECONDS = 2


def compose_args() -> list[str]:
    """Return docker compose arguments, including generated scan mounts if present."""

    args = ["docker", "compose", "-f", "compose.yml"]
    if SCAN_ROOTS_COMPOSE.exists():
        args.extend(["-f", str(SCAN_ROOTS_COMPOSE)])
    return args


def run(command: list[str]) -> None:
    """Run a command from the project root."""

    subprocess.run(command, cwd=PROJECT_ROOT, check=True)


def read_env_var(name: str, default: str) -> str:
    """Read a value from process env, local .env, or a default."""

    if value := os.getenv(name):
        return value
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, raw = line.split("=", maxsplit=1)
            if key.strip() == name:
                return raw.strip()
    return default


def database_config() -> tuple[str, str]:
    """Return database name and user from the same config Compose uses."""

    return read_env_var("POSTGRES_DB", "omero"), read_env_var("POSTGRES_USER", "omero")


def wait_for_database() -> None:
    """Wait for Postgres to accept connections before creating a backup."""

    db_name, db_user = database_config()
    deadline = time.time() + DB_WAIT_SECONDS
    command = [
        *compose_args(),
        "exec",
        "-T",
        "db",
        "pg_isready",
        "-U",
        db_user,
        "-d",
        db_name,
    ]

    while time.time() < deadline:
        result = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
        if result.returncode == 0:
            return
        time.sleep(DB_WAIT_INTERVAL_SECONDS)

    raise RuntimeError("PostgreSQL did not become ready for safe-restart backup")


def backup_postgres() -> Path:
    """Create a timestamped pg_dump backup from the existing database."""

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = BACKUP_DIR / f"safe-restart-postgres-{timestamp}.sql"
    db_name, db_user = database_config()

    with output_path.open("w", encoding="utf-8") as handle:
        subprocess.run(
            [
                *compose_args(),
                "exec",
                "-T",
                "db",
                "pg_dump",
                "-U",
                db_user,
                db_name,
            ],
            cwd=PROJECT_ROOT,
            check=True,
            stdout=handle,
        )
    return output_path


def stale_lock_files() -> list[Path]:
    """Return stale repository lock files that can block a clean OMERO restart."""

    if not OMERO_REPOSITORY_ROOT.exists():
        return []
    return sorted(OMERO_REPOSITORY_ROOT.glob("*/.lock"))


def remove_locks_with_root_helper(lock_paths: list[Path]) -> None:
    """Remove permission-protected lock files via a minimal root container."""

    if not lock_paths:
        return

    relative_paths = [
        str(path.relative_to(OMERO_REPOSITORY_ROOT)) for path in lock_paths
    ]
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            "0:0",
            "-v",
            f"{OMERO_REPOSITORY_ROOT}:/repository",
            "postgres:16",
            "rm",
            "-f",
            *[f"/repository/{path}" for path in relative_paths],
        ],
        cwd=PROJECT_ROOT,
        check=True,
    )


def remove_stale_lock_files() -> list[Path]:
    """Remove OMERO repository lock files after OMERO.server has stopped."""

    removed: list[Path] = []
    protected: list[Path] = []
    for lock_path in stale_lock_files():
        try:
            lock_path.unlink()
            removed.append(lock_path)
        except PermissionError:
            protected.append(lock_path)

    remove_locks_with_root_helper(protected)
    removed.extend(protected)
    return removed


def main() -> None:
    """Backup the database, remove stale repository locks, and restart services."""

    print("[safe-restart] stopping OMERO app services")
    run([*compose_args(), "stop", "omero-web", "omero-server"])

    print("[safe-restart] ensuring database is running")
    run([*compose_args(), "up", "-d", "db"])
    wait_for_database()

    backup_path = backup_postgres()
    backup_display = backup_path.relative_to(PROJECT_ROOT)
    print(f"[safe-restart] database backup written: {backup_display}")

    removed = remove_stale_lock_files()
    if removed:
        for path in removed:
            lock_display = path.relative_to(PROJECT_ROOT)
            print(f"[safe-restart] removed stale lock: {lock_display}")
    else:
        print("[safe-restart] no stale repository lock files found")

    print("[safe-restart] starting full stack")
    run([*compose_args(), "up", "-d"])
    print("[safe-restart] complete; run `uv run poe healthcheck` to verify readiness")


if __name__ == "__main__":
    main()
