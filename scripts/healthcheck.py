"""Quick container-level health checks for local OMERO services."""

from __future__ import annotations

import json
import subprocess
import sys


def run_cmd(command: list[str]) -> bool:
    """Return True when a command exits successfully."""

    result = subprocess.run(command, check=False, capture_output=True, text=True)
    return result.returncode == 0


def probe_server_health() -> str:
    """Probe OMERO.server readiness via CLI inside container."""

    ok = run_cmd(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "omero-server",
            "bash",
            "-lc",
            'export PATH="/opt/omero/server/venv3/bin:$PATH"; omero version >/dev/null',
        ]
    )
    return "healthy" if ok else "unhealthy"


def probe_web_health() -> str:
    """Probe OMERO.web endpoint from host."""

    ok = run_cmd(
        [
            "curl",
            "-fsSI",
            "http://localhost:4080/webclient/",
        ]
    )
    return "healthy" if ok else "unhealthy"


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

    rows_out = [
        {
            "service": row.get("Service", "unknown"),
            "state": row.get("State", "unknown"),
            "health": row.get("Health", "n/a"),
        }
        for row in rows
    ]
    for row in rows_out:
        health = row["health"].strip()
        if row["service"] == "omero-server" and not health:
            row["health"] = probe_server_health()
        if row["service"] == "omero-web" and not health:
            row["health"] = probe_web_health()
    return rows_out


if __name__ == "__main__":
    services = get_compose_health()
    failed = False
    for service in services:
        print(
            f"{service['service']}: state={service['state']} health={service['health']}"
        )
        state = service["state"].strip().lower()
        health = service["health"].strip().lower()
        if state != "running":
            failed = True
        if health not in {"", "n/a", "healthy"}:
            failed = True
    if failed:
        sys.exit(1)
