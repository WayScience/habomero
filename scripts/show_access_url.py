"""Print copy/paste OMERO.web URLs using localhost and local network IP."""

from __future__ import annotations

import os
import socket
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"


def read_env_var(name: str, default: str) -> str:
    """Read variable from process env or project .env file."""

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


def get_local_ip() -> str:
    """Best-effort local network IP address."""

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def main() -> None:
    """Emit access URLs for OMERO.web."""

    port = read_env_var("OMERO_WEB_PORT", "4080")
    public_hostname = read_env_var("OMERO_PUBLIC_HOSTNAME", "").strip()
    host_ip = get_local_ip()
    print(f"OMERO.web local:    http://localhost:{port}/webclient/")
    print(f"OMERO.web network:  http://{host_ip}:{port}/webclient/")
    if public_hostname:
        print(f"OMERO.web hostname: http://{public_hostname}:{port}/webclient/")


if __name__ == "__main__":
    main()
