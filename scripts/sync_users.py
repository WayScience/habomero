"""Sync an OMERO user allowlist into the running OMERO server."""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config/omero/users.yml"
SCAN_CONFIG_PATH = PROJECT_ROOT / "config/omero/scan_dirs.yml"
ENV_PATH = PROJECT_ROOT / ".env"
OMERO_CMD_TIMEOUT_SECONDS = 30
OMERO_WAIT_MAX_SECONDS = 180
OMERO_WAIT_POLL_SECONDS = 3


class UserConfigError(ValueError):
    """Raised when the user allowlist configuration is invalid."""


def read_env_var(name: str) -> str:
    """Read a required variable from process env or local .env file."""

    if value := os.getenv(name):
        return value

    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, raw = line.split("=", maxsplit=1)
            if key.strip() == name:
                return raw.strip()

    raise UserConfigError(f"Missing required environment variable: {name}")


RESERVED_GROUPS = {"user", "guest", "system"}


def run_in_omero(root_password: str, command: str) -> subprocess.CompletedProcess[str]:
    """Run an OMERO CLI command in the server container as root admin."""

    full_command = (
        "set -euo pipefail; "
        'export PATH="/opt/omero/server/venv3/bin:$PATH"; '
        "omero logout >/dev/null 2>&1 || true; "
        "omero login root@localhost:4064 -g system "
        '-w "$OMERO_ROOT_PASSWORD" >/dev/null; '
        f"{command}"
    )
    args = [
        "docker",
        "compose",
        "exec",
        "-T",
        "-e",
        f"OMERO_ROOT_PASSWORD={root_password}",
        "omero-server",
        "bash",
        "-lc",
        full_command,
    ]
    try:
        return subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=OMERO_CMD_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=args,
            returncode=124,
            stdout="",
            stderr=(
                f"Timed out running OMERO command after {OMERO_CMD_TIMEOUT_SECONDS}s"
            ),
        )


def wait_for_server(root_password: str) -> None:
    """Wait until OMERO server auth endpoint is ready."""

    check_cmd = 'omero login root@localhost:4064 -w "$OMERO_ROOT_PASSWORD" >/dev/null'
    attempts = max(OMERO_WAIT_MAX_SECONDS // OMERO_WAIT_POLL_SECONDS, 1)
    for attempt in range(1, attempts + 1):
        result = run_in_omero(root_password, check_cmd)
        if result.returncode == 0:
            print(f"[sync-users] OMERO server ready after attempt {attempt}/{attempts}")
            return
        if attempt == 1 or attempt % 5 == 0:
            detail = (
                result.stderr.strip() or result.stdout.strip() or "not ready"
            ).splitlines()[0]
            print(
                "[sync-users] waiting for OMERO server "
                f"attempt={attempt}/{attempts}: {detail}"
            )
        time.sleep(OMERO_WAIT_POLL_SECONDS)

    raise RuntimeError(
        "OMERO server did not become ready in time for user sync "
        f"(waited {OMERO_WAIT_MAX_SECONDS}s)"
    )


def user_exists(root_password: str, username: str) -> bool:
    """Return True when user exists in OMERO."""

    result = run_in_omero(root_password, f"omero user info --user-name {username}")
    return result.returncode == 0


def ensure_group(root_password: str, group: str) -> None:
    """Create a non-reserved group if it does not already exist."""

    result = run_in_omero(root_password, f"omero group info --group-name {group}")
    if result.returncode == 0:
        return

    created = run_in_omero(root_password, f"omero group add {group}")
    if created.returncode != 0 and "group exists" not in created.stderr.lower():
        raise RuntimeError(
            f"Failed to create group '{group}': {created.stderr.strip()}"
        )
    if created.returncode == 0:
        print(f"group-created: {group}")
    else:
        print(f"group-exists: {group}")


def ensure_group_permissions(
    root_password: str,
    group: str,
    permissions: str,
) -> None:
    """Best-effort set data-group permissions for shared visibility."""

    permission_map = {
        "private": "rw----",
        "read-only": "rwr---",
        "read-annotate": "rwra--",
        "read-write": "rwrw--",
    }
    normalized = permissions.strip().lower()
    perm_value = permission_map.get(normalized, permissions.strip())
    quoted_perm = shlex.quote(perm_value)
    quoted_group = shlex.quote(group)

    commands = [
        f"omero group perms --perms={quoted_perm} --name={quoted_group}",
        f"omero group perms --perms={quoted_perm} {quoted_group}",
        f"omero group perms {quoted_group} --perms={quoted_perm}",
    ]
    last_error = ""
    for cmd in commands:
        result = run_in_omero(root_password, cmd)
        if result.returncode == 0:
            print(f"group-perms-ok: {group} -> {perm_value}")
            return
        last_error = result.stderr.strip() or result.stdout.strip()
    print(
        "group-perms-warn: "
        f"could not set permissions for {group} -> {perm_value}: {last_error}"
    )


def ensure_user_group_membership(root_password: str, username: str, group: str) -> None:
    """Ensure a user is a member of a target group."""

    join_result = run_in_omero(
        root_password,
        f"omero user joingroup {group} --name={username}",
    )
    if join_result.returncode == 0:
        print(f"group-ok: {username} -> {group}")
        return

    stderr = join_result.stderr.strip()
    if "already" in stderr.lower():
        print(f"group-exists: {username} -> {group}")
        return

    raise RuntimeError(f"Failed to set group membership for '{username}': {stderr}")


def ensure_default_group(
    root_password: str,
    username: str,
    group: str,
) -> None:
    """Best-effort set user's default group for OMERO.web visibility."""

    help_result = run_in_omero(root_password, "omero user -h")
    text = f"{help_result.stdout}\n{help_result.stderr}".lower()
    if "defaultgroup" not in text:
        print(f"default-group-skip: CLI unsupported; keep existing for {username}")
        return

    commands = [
        f"omero user defaultgroup --name={username} {group}",
        f"omero user defaultgroup {group} --name={username}",
        f"omero user defaultgroup --user-name {username} {group}",
    ]
    last_error = ""
    for cmd in commands:
        result = run_in_omero(root_password, cmd)
        if result.returncode == 0:
            print(f"default-group-ok: {username} -> {group}")
            return
        last_error = result.stderr.strip() or result.stdout.strip()
    print(
        "default-group-warn: "
        f"could not set default group for {username} -> {group} "
        f"(manual group switch may be needed): {last_error}"
    )


def ensure_user(root_password: str, user: dict[str, str]) -> None:
    """Create user if missing and ensure group membership is present."""

    username = user["username"]
    group = user["group"]
    ensure_group(root_password, group)

    if not user_exists(root_password, username):
        username_q = shlex.quote(username)
        first_q = shlex.quote(user["first_name"])
        last_q = shlex.quote(user["last_name"])
        group_q = shlex.quote(group)
        email_q = shlex.quote(user["email"])
        institution_q = shlex.quote(user["institution"])
        password_q = shlex.quote(user.get("password", "changeme123"))
        add_cmd = (
            "omero user add "
            f"{username_q} "
            f"{first_q} "
            f"{last_q} "
            f"--group-name {group_q} "
            f"--email {email_q} "
            f"--institution {institution_q} "
            f"-P {password_q}"
        )
        created = run_in_omero(root_password, add_cmd)
        if created.returncode != 0:
            raise RuntimeError(
                f"Failed to create user '{username}': {created.stderr.strip()}"
            )
        print(f"created: {username}")
    else:
        print(f"exists: {username}")

    password = user.get("password", "")
    if password and not user_exists(root_password, username):
        passwd_commands = [
            f"omero user password --user-name {username} {password}",
            f"omero user password {username} {password}",
        ]
        password_set = False
        last_error = ""
        for cmd in passwd_commands:
            update_pass = run_in_omero(root_password, cmd)
            if update_pass.returncode == 0:
                print(f"password-set: {username}")
                password_set = True
                break
            last_error = update_pass.stderr.strip()
        if not password_set:
            print(f"password-unchanged: {username} ({last_error})")

    ensure_user_group_membership(root_password, username, group)


def load_users() -> list[dict[str, str]]:  # noqa: C901
    """Load and validate user allowlist entries from YAML."""

    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"User config is missing: {CONFIG_PATH}")

    payload = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    users = payload.get("users", [])
    if not isinstance(users, list):
        raise UserConfigError("users must be a YAML list")

    required = {
        "username",
        "first_name",
        "last_name",
        "group",
        "email",
        "institution",
    }

    normalized: list[dict[str, str]] = []
    for item in users:
        if not isinstance(item, dict):
            raise UserConfigError("Each user entry must be a mapping")

        missing = sorted(required - set(item.keys()))
        if missing:
            raise UserConfigError(f"Missing required user fields: {', '.join(missing)}")

        row: dict[str, str] = {}
        for key in required:
            value = item[key]
            if not isinstance(value, str) or not value.strip():
                raise UserConfigError(f"User field '{key}' must be a non-empty string")
            row[key] = value.strip()
        if row["group"] in RESERVED_GROUPS:
            raise UserConfigError(
                "User field 'group' must be a non-reserved data group "
                "(not one of: user, guest, system)"
            )

        if "password" in item:
            password = item["password"]
            if not isinstance(password, str) or not password.strip():
                raise UserConfigError(
                    "User field 'password' must be a non-empty string"
                )
            row["password"] = password.strip()

        normalized.append(row)

    return normalized


def load_shared_group() -> str | None:
    """Load optional shared group name for all users."""

    if not SCAN_CONFIG_PATH.exists():
        return None
    payload = yaml.safe_load(SCAN_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    group = payload.get("shared_group")
    if group is None:
        return None
    if not isinstance(group, str) or not group.strip():
        raise UserConfigError("shared_group must be a non-empty string when provided")
    if group.strip() in RESERVED_GROUPS:
        raise UserConfigError(
            "shared_group must be a non-reserved data group "
            "(not one of: user, guest, system)"
        )
    return group.strip()


def load_shared_group_permissions() -> str:
    """Load optional shared-group permissions for imported-data visibility."""

    default = "read-annotate"
    if not SCAN_CONFIG_PATH.exists():
        return default
    payload = yaml.safe_load(SCAN_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    raw = payload.get("shared_group_permissions")
    if raw is None:
        return default
    if not isinstance(raw, str) or not raw.strip():
        raise UserConfigError(
            "shared_group_permissions must be a non-empty string when provided"
        )
    return raw.strip()


def main() -> None:
    """Load user config and sync each entry into OMERO."""

    root_password = read_env_var("OMERO_ROOT_PASSWORD")
    wait_for_server(root_password)
    users = load_users()
    shared_group = load_shared_group()
    shared_group_permissions = load_shared_group_permissions()
    if shared_group:
        ensure_group(root_password, shared_group)
        ensure_group_permissions(root_password, shared_group, shared_group_permissions)
    for user in users:
        ensure_user(root_password, user)
        ensure_default_group(root_password, user["username"], user["group"])
        if shared_group and user["group"] != shared_group:
            ensure_user_group_membership(root_password, user["username"], shared_group)
            ensure_default_group(root_password, user["username"], shared_group)


if __name__ == "__main__":
    main()
