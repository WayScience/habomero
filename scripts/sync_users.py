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
        'omero login root@localhost:4064 -w "$OMERO_ROOT_PASSWORD" >/dev/null; '
        f"{command}"
    )
    return subprocess.run(
        [
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
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def wait_for_server(root_password: str) -> None:
    """Wait until OMERO server auth endpoint is ready."""

    check_cmd = 'omero login root@localhost:4064 -w "$OMERO_ROOT_PASSWORD" >/dev/null'
    for _ in range(40):
        result = run_in_omero(root_password, check_cmd)
        if result.returncode == 0:
            return
        time.sleep(3)

    raise RuntimeError("OMERO server did not become ready in time for user sync")


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


def main() -> None:
    """Load user config and sync each entry into OMERO."""

    root_password = read_env_var("OMERO_ROOT_PASSWORD")
    wait_for_server(root_password)
    users = load_users()
    shared_group = load_shared_group()
    for user in users:
        ensure_user(root_password, user)
        if shared_group and user["group"] != shared_group:
            ensure_group(root_password, shared_group)
            join_result = run_in_omero(
                root_password,
                f"omero user joingroup {shared_group} --name={user['username']}",
            )
            if join_result.returncode == 0:
                print(f"group-ok: {user['username']} -> {shared_group}")
            elif "already" in join_result.stderr.lower():
                print(f"group-exists: {user['username']} -> {shared_group}")


if __name__ == "__main__":
    main()
