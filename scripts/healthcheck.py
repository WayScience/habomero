"""Quick container-level health checks for local OMERO services."""

from __future__ import annotations

import json
import subprocess


def get_compose_health() -> list[dict[str, str]]:
    """Collect service status from docker compose ps JSON output."""

    output = subprocess.check_output(
        ["docker", "compose", "ps", "--format", "json"],
        text=True,
    )
    output = output.strip()
    if output.startswith("["):
        rows = json.loads(output)
    else:
        rows = [json.loads(line) for line in output.splitlines() if line.strip()]

    if isinstance(rows, dict):
        rows = [rows]

    return [
        {
            "service": row.get("Service", "unknown"),
            "state": row.get("State", "unknown"),
            "health": row.get("Health", "n/a"),
        }
        for row in rows
    ]


if __name__ == "__main__":
    for service in get_compose_health():
        print(
            f"{service['service']}: state={service['state']} health={service['health']}"
        )
