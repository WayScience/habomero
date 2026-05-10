"""Create timestamped PostgreSQL backups from the running OMERO stack."""

from __future__ import annotations

import datetime as dt
import os
import subprocess
from pathlib import Path


def backup_postgres() -> Path:
    """Run pg_dump inside the db container and store output under data/backups."""

    backup_dir = Path("data/backups")
    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = backup_dir / f"postgres-{timestamp}.sql"

    db_name = os.getenv("POSTGRES_DB", "omero")
    db_user = os.getenv("POSTGRES_USER", "omero")

    with output_path.open("w", encoding="utf-8") as handle:
        subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "db",
                "pg_dump",
                "-U",
                db_user,
                db_name,
            ],
            check=True,
            stdout=handle,
        )

    return output_path


if __name__ == "__main__":
    result = backup_postgres()
    print(f"Backup written: {result}")
