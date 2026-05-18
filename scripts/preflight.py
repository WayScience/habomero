"""Host preflight checks for Docker and Linux container networking."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUTO_FIX_PERMS = os.environ.get("PREFLIGHT_AUTO_FIX_PERMISSIONS", "1").strip() != "0"
EXPECTED_ID_LINES = 2


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


def check_container_volume_write(image: str, host_path: Path) -> str | None:
    """Return an error if the image's default user cannot write to host_path."""

    if not host_path.exists():
        return (
            f"Required runtime directory is missing: {host_path}. "
            "Run: uv run poe provision"
        )
    if not host_path.is_dir():
        return f"Runtime path is not a directory: {host_path}"

    present = run(["docker", "image", "inspect", image])
    if present.returncode != 0:
        # Don't force image pull during preflight.
        return None

    probe = run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{host_path}:/permcheck",
            image,
            "bash",
            "-lc",
            "set -e; touch /permcheck/.writecheck && rm -f /permcheck/.writecheck",
        ]
    )
    if probe.returncode == 0:
        return None

    detail = probe.stderr.strip() or probe.stdout.strip() or "permission probe failed"
    return (
        "Container write access check failed for "
        f"{host_path} with image {image}: {detail}. "
        "Fix by chown/chmod on host path so container user can write."
    )


def default_container_uid_gid(image: str) -> tuple[int, int] | None:
    """Return default runtime uid/gid for an image."""

    probe = run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "bash",
            image,
            "-lc",
            "id -u; id -g",
        ]
    )
    if probe.returncode != 0:
        return None
    lines = [line.strip() for line in probe.stdout.splitlines() if line.strip()]
    if len(lines) < EXPECTED_ID_LINES:
        return None
    try:
        return int(lines[0]), int(lines[1])
    except ValueError:
        return None


def try_fix_container_volume_write(
    image: str, host_path: Path, target_uid: int, target_gid: int
) -> str | None:
    """Attempt to repair host-path ownership and perms for container writes."""

    fix = run(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            "0:0",
            "-v",
            f"{host_path}:/permcheck",
            image,
            "bash",
            "-lc",
            (
                f"chown -R {target_uid}:{target_gid} /permcheck "
                "&& chmod -R u+rwX,g+rwX /permcheck"
            ),
        ]
    )
    if fix.returncode == 0:
        return None
    detail = fix.stderr.strip() or fix.stdout.strip() or "auto-fix failed"
    return detail


def check_runtime_volume_permissions(errors: list[str], warnings: list[str]) -> None:
    """Validate that OMERO runtime volume mounts are writable by container users."""

    checks = [
        ("openmicroscopy/omero-server:latest", PROJECT_ROOT / "data/omero"),
        (
            "openmicroscopy/omero-web-standalone:latest",
            PROJECT_ROOT / "data/omero-web-var",
        ),
    ]
    for image, path in checks:
        if run(["docker", "image", "inspect", image]).returncode != 0:
            warnings.append(
                f"Skipped volume permission probe for {image} (image not present yet)"
            )
            continue
        issue = check_container_volume_write(image, path)
        if issue and AUTO_FIX_PERMS:
            uid_gid = default_container_uid_gid(image)
            if uid_gid is not None:
                uid, gid = uid_gid
                fix_issue = try_fix_container_volume_write(image, path, uid, gid)
                if fix_issue is None:
                    issue = check_container_volume_write(image, path)
                    if issue is None:
                        warnings.append(
                            "Auto-fixed runtime volume permissions for "
                            f"{path} (uid:gid {uid}:{gid})"
                        )
                else:
                    issue = (
                        f"{issue} Auto-fix attempt failed: {fix_issue}. "
                        f"Try: sudo chown -R {uid}:{gid} {path} && "
                        f"sudo chmod -R u+rwX,g+rwX {path}"
                    )
        if issue:
            errors.append(issue)


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
    check_runtime_volume_permissions(errors, warnings)
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
