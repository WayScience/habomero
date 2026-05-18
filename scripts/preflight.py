"""Host preflight checks for Docker and Linux container networking."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a command and capture output without raising."""

    return subprocess.run(command, check=False, capture_output=True, text=True)


def check_binary(name: str) -> str | None:
    """Return an error message if a required binary is missing."""

    if shutil.which(name):
        return None
    return f"Missing required binary: {name}"


def check_docker_info() -> str | None:
    """Validate that Docker daemon is reachable."""

    result = run(["docker", "info"])
    if result.returncode == 0:
        return None
    detail = result.stderr.strip() or result.stdout.strip() or "unknown docker error"
    return f"Docker daemon is not reachable: {detail}"


def read_sysctl(path: str) -> str | None:
    """Read a sysctl value from /proc/sys style path."""

    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return None


def check_linux_networking() -> list[str]:
    """Collect Linux-specific warnings/errors for bridge/veth networking."""

    issues: list[str] = []

    modules = ""
    try:
        with open("/proc/modules", encoding="utf-8") as handle:
            modules = handle.read()
    except OSError:
        issues.append(
            "Could not read /proc/modules to verify kernel networking modules"
        )
        return issues

    if "veth " not in modules:
        issues.append("Kernel module 'veth' is not loaded (try: sudo modprobe veth)")
    if "br_netfilter " not in modules:
        issues.append(
            "Kernel module 'br_netfilter' is not loaded "
            "(try: sudo modprobe br_netfilter)"
        )

    ip_forward = read_sysctl("/proc/sys/net/ipv4/ip_forward")
    if ip_forward is not None and ip_forward != "1":
        issues.append(
            "net.ipv4.ip_forward is not enabled (try: sudo sysctl -w "
            "net.ipv4.ip_forward=1)"
        )

    bridge_nf = read_sysctl("/proc/sys/net/bridge/bridge-nf-call-iptables")
    if bridge_nf is not None and bridge_nf != "1":
        issues.append(
            "net.bridge.bridge-nf-call-iptables is not enabled "
            "(try: sudo sysctl -w net.bridge.bridge-nf-call-iptables=1)"
        )

    return issues


def check_docker_bridge_network() -> str | None:
    """Ensure Docker bridge networking is available."""

    result = run(["docker", "network", "inspect", "bridge"])
    if result.returncode == 0:
        return None
    detail = result.stderr.strip() or result.stdout.strip() or "bridge inspect failed"
    return f"Docker bridge network is unavailable: {detail}"


def check_docker_bridge_container_start() -> str | None:
    """Best-effort bridge datapath check using a local Alpine image."""

    present = run(["docker", "image", "inspect", "alpine:3.20"])
    if present.returncode != 0:
        return None

    result = run(
        ["docker", "run", "--rm", "--network", "bridge", "alpine:3.20", "true"]
    )
    if result.returncode == 0:
        return None
    detail = (
        result.stderr.strip()
        or result.stdout.strip()
        or "bridge container start failed"
    )
    return f"Docker bridge container start failed: {detail}"


def check_core_requirements(errors: list[str]) -> None:
    """Validate required binaries and Docker daemon availability."""

    for binary in ("docker",):
        message = check_binary(binary)
        if message:
            errors.append(message)

    if errors:
        return

    docker_error = check_docker_info()
    if docker_error:
        errors.append(docker_error)
    bridge_error = check_docker_bridge_network()
    if bridge_error:
        errors.append(bridge_error)
    bridge_start_error = check_docker_bridge_container_start()
    if bridge_start_error:
        errors.append(bridge_start_error)


def check_linux_host(errors: list[str], warnings: list[str]) -> None:
    """Validate Linux host prerequisites for Docker bridge/veth networking."""

    if platform.system().lower() != "linux":
        return

    linux_issues = check_linux_networking()
    warnings.extend(linux_issues)
    if os.geteuid() != 0:
        warnings.append(
            "Running as non-root user; if Docker is rootless, bridge/veth "
            "support may be limited."
        )


def main() -> None:
    """Run preflight checks and exit non-zero if required checks fail."""

    errors: list[str] = []
    warnings: list[str] = []
    check_core_requirements(errors)
    check_linux_host(errors, warnings)

    if errors:
        print("[preflight] failed")
        for issue in errors:
            print(f"[preflight][error] {issue}")
        for issue in warnings:
            print(f"[preflight][warn] {issue}")
        sys.exit(1)

    print("[preflight] ok")
    for issue in warnings:
        print(f"[preflight][warn] {issue}")


if __name__ == "__main__":
    main()
