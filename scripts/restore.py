"""Restore PostgreSQL backups into the running OMERO stack."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

EXPECTED_ARG_COUNT = 2


def restore_postgres(backup_file: str) -> None:
    """Restore a SQL dump file into the db container."""

    sql_path = Path(backup_file)
    if not sql_path.exists():
        raise FileNotFoundError(f"Backup not found: {sql_path}")

    db_name = os.getenv("POSTGRES_DB", "omero")
    db_user = os.getenv("POSTGRES_USER", "omero")

    with sql_path.open("r", encoding="utf-8") as handle:
        subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "db",
                "psql",
                "-U",
                db_user,
                "-d",
                db_name,
            ],
            check=True,
            stdin=handle,
        )


if __name__ == "__main__":
    if len(sys.argv) != EXPECTED_ARG_COUNT:
        raise SystemExit("Usage: python scripts/restore.py data/backups/<file>.sql")

    restore_postgres(sys.argv[1])
    print("Restore complete")
