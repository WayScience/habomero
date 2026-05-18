"""Quick container-level health checks for local OMERO services."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time


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


def all_services_healthy(services: list[dict[str, str]]) -> bool:
    """Return True when all known services are running and healthy."""

    for service in services:
        state = service["state"].strip().lower()
        health = service["health"].strip().lower()
        if state != "running":
            return False
        if health not in {"", "n/a", "healthy"}:
            return False
    return True


def format_service_status(service: dict[str, str]) -> str:
    """Render one service status line."""

    name = service["service"]
    state = service["state"]
    health = service["health"]
    return f"{name}: state={state} health={health}"


if __name__ == "__main__":
    wait_seconds = int(os.environ.get("HEALTHCHECK_WAIT_SECONDS", "0"))
    interval_seconds = int(os.environ.get("HEALTHCHECK_WAIT_INTERVAL_SECONDS", "3"))
    deadline = time.time() + max(wait_seconds, 0)
    last: list[dict[str, str]] = []
    while True:
        services = get_compose_health()
        last = services
        if all_services_healthy(services):
            for service in services:
                print(format_service_status(service))
            sys.exit(0)
        if time.time() >= deadline:
            break
        time.sleep(max(interval_seconds, 1))

    for service in last:
        print(format_service_status(service))
    sys.exit(1)
