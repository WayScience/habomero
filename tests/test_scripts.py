"""Unit tests for core script helpers."""

from __future__ import annotations

import struct
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from scripts import (
    hcs,
    import_scan,
    safe_restart,
    scan_dirs,
    show_access_url,
    sync_users,
    validate,
)


def test_read_env_var_prefers_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Environment variables should override .env file lookup."""

    monkeypatch.setenv("OMERO_WEB_PORT", "9999")
    value = show_access_url.read_env_var("OMERO_WEB_PORT", "4080")
    assert value == "9999"


def test_show_access_url_prints_configured_hostname(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Configured LAN hostnames should appear in copy/paste URLs."""

    monkeypatch.setenv("OMERO_WEB_PORT", "4080")
    monkeypatch.setenv("OMERO_PUBLIC_HOSTNAME", "habomero.local")
    monkeypatch.setattr(show_access_url, "get_local_ip", lambda: "192.0.2.10")

    show_access_url.main()

    output = capsys.readouterr().out
    assert "http://habomero.local:4080/webclient/" in output


def test_scan_dirs_rejects_escape_without_external_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Paths outside project root are rejected unless explicitly enabled."""

    project_root = tmp_path / "project"
    project_root.mkdir()
    config_path = project_root / "scan_dirs.yml"
    config_path.write_text("scan_directories:\n  - ../outside\n", encoding="utf-8")

    monkeypatch.setattr(scan_dirs, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(scan_dirs, "CONFIG_PATH", config_path)

    with pytest.raises(ValueError, match="escapes project root"):
        scan_dirs.load_scan_directories()


def test_validate_layout_missing_path_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validation should fail when required files are missing."""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(validate, "REQUIRED_PATHS", [Path("compose.yml")])

    with pytest.raises(FileNotFoundError, match="compose.yml"):
        validate.validate_layout()


def test_scan_dirs_collapses_overlapping_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nested configured scan roots should collapse to the broader root."""

    project_root = tmp_path / "project"
    project_root.mkdir()
    root = project_root / "data"
    nested = root / "nested"
    nested.mkdir(parents=True)

    config_path = project_root / "scan_dirs.yml"
    config_path.write_text(
        "scan_directories:\n  - data\n  - data/nested\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(scan_dirs, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(scan_dirs, "CONFIG_PATH", config_path)

    result = scan_dirs.load_scan_directories()
    assert result == [root.resolve()]


def test_scan_dirs_materializes_per_root_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mapping entries can attach an OMERO group to a scan root."""

    project_root = tmp_path / "project"
    source = project_root / "cardiac"
    source.mkdir(parents=True)
    config_path = project_root / "scan_dirs.yml"
    state_path = project_root / "state.yml"
    compose_path = project_root / "compose.yml"
    config_path.write_text(
        "scan_directories:\n"
        "  - path: cardiac\n"
        "    group: way_mckinsey_cardiac_fibrosis\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(scan_dirs, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(scan_dirs, "CONFIG_PATH", config_path)
    monkeypatch.setattr(scan_dirs, "STATE_PATH", state_path)
    monkeypatch.setattr(scan_dirs, "COMPOSE_OVERRIDE_PATH", compose_path)

    entries = scan_dirs.load_scan_directory_entries()
    mapping = scan_dirs.materialize_scan_roots(entries)

    assert entries == [
        {
            "path": str(source.resolve()),
            "group": "way_mckinsey_cardiac_fibrosis",
        }
    ]
    assert next(iter(mapping.values()))["group"] == "way_mckinsey_cardiac_fibrosis"


def test_scan_dirs_rejects_bool_hcs_channels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A YAML boolean (e.g. `yes`) must not silently pass as a channel count.

    `bool` is a subclass of `int` in Python, so `isinstance(True, int)` is
    True and `True <= 0` is False -- without an explicit bool check, a YAML
    `hcs_channels: yes` would slip through as `str(True)` and only fail much
    later, far from the actual config mistake.
    """

    project_root = tmp_path / "project"
    source = project_root / "cardiac"
    source.mkdir(parents=True)
    config_path = project_root / "scan_dirs.yml"
    config_path.write_text(
        "scan_directories:\n  - path: cardiac\n    hcs_channels: yes\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(scan_dirs, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(scan_dirs, "CONFIG_PATH", config_path)

    with pytest.raises(ValueError, match="positive integers"):
        scan_dirs.load_scan_directory_entries()


def test_scan_dirs_materializes_per_root_import_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mapping entries can attach an explicit import owner to a scan root."""

    project_root = tmp_path / "project"
    source = project_root / "cardiac"
    source.mkdir(parents=True)
    config_path = project_root / "scan_dirs.yml"
    state_path = project_root / "state.yml"
    compose_path = project_root / "compose.yml"
    config_path.write_text(
        "scan_directories:\n  - path: cardiac\n    import_user: habomero\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(scan_dirs, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(scan_dirs, "CONFIG_PATH", config_path)
    monkeypatch.setattr(scan_dirs, "STATE_PATH", state_path)
    monkeypatch.setattr(scan_dirs, "COMPOSE_OVERRIDE_PATH", compose_path)

    entries = scan_dirs.load_scan_directory_entries()
    mapping = scan_dirs.materialize_scan_roots(entries)

    assert entries == [
        {
            "path": str(source.resolve()),
            "import_user": "habomero",
        }
    ]
    assert next(iter(mapping.values()))["import_user"] == "habomero"


def test_sync_users_loads_shared_group_opt_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restricted users can avoid joining the global shared import group."""

    config_path = tmp_path / "users.yml"
    config_path.write_text(
        "users:\n"
        "  - username: way_mckinsey\n"
        "    first_name: Way\n"
        "    last_name: McKinsey\n"
        "    group: way_mckinsey_cardiac_fibrosis\n"
        "    join_shared_group: false\n"
        "    email: way@example.org\n"
        "    institution: Local Lab\n"
        "    password: way_mckinsey\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(sync_users, "CONFIG_PATH", config_path)

    users = sync_users.load_users()

    assert users[0]["join_shared_group"] is False
    assert users[0]["extra_groups"] == []


def test_sync_users_does_not_join_shared_group_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shared group visibility is opt-in for non-primary-group users."""

    config_path = tmp_path / "users.yml"
    config_path.write_text(
        "users:\n"
        "  - username: viewer\n"
        "    first_name: View\n"
        "    last_name: User\n"
        "    group: restricted\n"
        "    email: viewer@example.org\n"
        "    institution: Local Lab\n"
        "    password: viewer\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(sync_users, "CONFIG_PATH", config_path)

    users = sync_users.load_users()

    assert users[0]["join_shared_group"] is False


def test_sync_users_group_absence_removes_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opted-out users are removed from an old shared-group membership."""

    commands: list[str] = []

    def fake_run(root_password: str, command: str) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(sync_users, "run_in_omero_with_retry", fake_run)

    sync_users.ensure_user_group_absence("root-password", "viewer", "lab")

    assert commands == ["omero user leavegroup lab --name=viewer"]


def test_sync_users_loads_scan_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-root scan groups are discovered for sync and permissions."""

    config_path = tmp_path / "scan_dirs.yml"
    config_path.write_text(
        "scan_directories:\n"
        "  - path: a\n"
        "    group: rxrx19a\n"
        "  - path: b\n"
        "    group: cfret_subtyping_data\n"
        "scan_group_permissions: read-annotate\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(sync_users, "SCAN_CONFIG_PATH", config_path)

    assert sync_users.load_scan_groups() == {"rxrx19a", "cfret_subtyping_data"}
    assert sync_users.load_scan_group_permissions() == "read-annotate"


def test_sync_users_loads_password_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """User templates can reference password environment variables."""

    config_path = tmp_path / "users.yml"
    config_path.write_text(
        "users:\n"
        "  - username: habomero\n"
        "    first_name: Habomero\n"
        "    last_name: Service\n"
        "    group: lab\n"
        "    email: habomero@example.org\n"
        "    institution: Local Lab\n"
        "    password_env: HABOMERO_TEST_PASSWORD\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(sync_users, "CONFIG_PATH", config_path)
    monkeypatch.setenv("HABOMERO_TEST_PASSWORD", "from-env")

    users = sync_users.load_users()

    assert users[0]["password"] == "from-env"


def test_import_scan_loads_password_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Import credentials can come from password_env entries."""

    config_path = tmp_path / "users.yml"
    config_path.write_text(
        "users:\n  - username: habomero\n    password_env: HABOMERO_IMPORT_PASSWORD\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(import_scan, "USERS_CONFIG_PATH", config_path)
    monkeypatch.setenv("HABOMERO_IMPORT_PASSWORD", "import-password")

    credentials, first_username = import_scan.load_user_credentials()

    assert first_username == "habomero"
    assert credentials == {"habomero": "import-password"}


def test_delete_missing_imports_removes_deleted_image_tracking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stale source files are deleted from OMERO and removed from state."""

    state_path = tmp_path / "imported_files.txt"
    keep_key = import_scan.imported_file_key(
        "root_a", "habomero", "lab", "/scan/roots/root_a/keep.tif"
    )
    missing_key = import_scan.imported_file_key(
        "root_a", "habomero", "lab", "/scan/roots/root_a/missing.tif"
    )
    other_key = import_scan.imported_file_key(
        "root_b", "habomero", "lab", "/scan/roots/root_b/other.tif"
    )
    imported = {
        keep_key,
        missing_key,
        other_key,
    }
    deleted_ids: list[int] = []

    monkeypatch.setattr(import_scan, "IMPORT_STATE_PATH", state_path)
    monkeypatch.setattr(
        import_scan,
        "load_dataset_image_ids_by_name",
        lambda *args: [123],
    )
    # Dataset still has other images, so the now-empty-dataset cleanup no-ops.
    monkeypatch.setattr(
        import_scan, "load_dataset_image_names", lambda *args: {"other.tif"}
    )

    def fake_delete_image(
        owner: str,
        owner_password: str,
        shared_group: str,
        image_id: int,
    ) -> bool:
        deleted_ids.append(image_id)
        return True

    monkeypatch.setattr(import_scan, "delete_image", fake_delete_image)

    deleted = import_scan.delete_missing_imports(
        "habomero",
        "habomero",
        "lab",
        "root_a",
        "/scan/roots/root_a",
        {"/scan/roots/root_a/keep.tif"},
        imported,
        {"root_a|owner=habomero|group=lab|root": 99},
    )

    assert deleted == 1
    assert deleted_ids == [123]
    assert imported == {
        keep_key,
        other_key,
    }
    assert state_path.read_text(encoding="utf-8").splitlines() == sorted(imported)


def test_delete_missing_imports_removes_now_empty_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dataset left with zero images after cleanup is deleted too.

    A non-empty sibling dataset (still holding other images) must be left alone.
    """

    state_path = tmp_path / "imported_files.txt"
    empties_key = import_scan.imported_file_key(
        "root_a", "habomero", "lab", "/scan/roots/root_a/dirA/missing1.tif"
    )
    stays_key = import_scan.imported_file_key(
        "root_a", "habomero", "lab", "/scan/roots/root_a/dirB/missing2.tif"
    )
    imported = {empties_key, stays_key}
    dataset_state = {
        "root_a|owner=habomero|group=lab|dirA": 501,
        "root_a|owner=habomero|group=lab|dirB": 502,
    }
    remaining_images_by_dataset = {501: set(), 502: {"other.tif"}}
    deleted_dataset_ids: list[int] = []

    monkeypatch.setattr(import_scan, "IMPORT_STATE_PATH", state_path)
    monkeypatch.setattr(
        import_scan,
        "load_dataset_image_ids_by_name",
        lambda *args: [111],
    )
    monkeypatch.setattr(import_scan, "delete_image", lambda *args: True)
    monkeypatch.setattr(
        import_scan,
        "load_dataset_image_names",
        lambda owner, password, group, dataset_id: remaining_images_by_dataset[
            dataset_id
        ],
    )

    def fake_delete_dataset_as_root(dataset_id: int) -> bool:
        deleted_dataset_ids.append(dataset_id)
        return True

    monkeypatch.setattr(
        import_scan, "delete_dataset_as_root", fake_delete_dataset_as_root
    )

    import_scan.delete_missing_imports(
        "habomero",
        "habomero",
        "lab",
        "root_a",
        "/scan/roots/root_a",
        set(),
        imported,
        dataset_state,
    )

    surviving_dataset_id = 502
    assert deleted_dataset_ids == [501]
    assert "root_a|owner=habomero|group=lab|dirA" not in dataset_state
    assert dataset_state["root_a|owner=habomero|group=lab|dirB"] == surviving_dataset_id


def test_delete_calls_use_wait_flag_not_broken_password_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delete helpers must pass --wait, not -w --no-wait.

    -w is the global OMERO CLI --password flag, not a --wait shortcut, so
    `-w --no-wait` gets parsed as `--password "--no-wait"` and the delete
    subcommand silently runs with no wait behavior at all (fire-and-forget,
    reporting success without confirming the delete actually completed).
    """

    root_commands: list[str] = []
    user_commands: list[str] = []

    def fake_run_as_root(command: str) -> subprocess.CompletedProcess[str]:
        root_commands.append(command)
        return subprocess.CompletedProcess(
            args=command, returncode=0, stdout="ok", stderr=""
        )

    def fake_run_in_omero(command: str) -> subprocess.CompletedProcess[str]:
        user_commands.append(command)
        return subprocess.CompletedProcess(
            args=command, returncode=0, stdout="ok", stderr=""
        )

    monkeypatch.setattr(import_scan, "run_as_root", fake_run_as_root)
    monkeypatch.setattr(import_scan, "run_in_omero", fake_run_in_omero)

    import_scan.delete_project(101)
    import_scan.delete_dataset_as_root(202)
    import_scan.delete_image("habomero", "pw", "lab", 303)
    import_scan.delete_dataset(
        "habomero", "pw", "lab", "root_a", "some/dir", 404, {"root_a|some/dir": 404}
    )

    expected_delete_call_count = 4
    all_commands = root_commands + user_commands
    assert len(all_commands) == expected_delete_call_count
    for command in all_commands:
        delete_call = command.rsplit(";", 1)[-1].strip()
        assert delete_call.startswith(("delete ", "omero delete "))
        assert "--wait -1" in delete_call
        assert "--no-wait" not in delete_call
        assert " -w " not in delete_call
        assert not delete_call.endswith(" -w")


def test_import_config_loads_explicit_omero_delete_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The public cleanup flag names OMERO as the deletion target."""

    config_path = tmp_path / "scan_dirs.yml"
    config_path.write_text("delete_omero_missing_files: true\n", encoding="utf-8")

    monkeypatch.setattr(import_scan, "SCAN_CONFIG_PATH", config_path)

    config = import_scan.load_import_config()

    assert config[-1] is True


def test_import_config_loads_default_import_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default import owner can be configured explicitly."""

    config_path = tmp_path / "scan_dirs.yml"
    config_path.write_text("import_user: habomero\n", encoding="utf-8")

    monkeypatch.setattr(import_scan, "SCAN_CONFIG_PATH", config_path)

    assert import_scan.load_default_import_user() == "habomero"


def test_import_config_loads_legacy_reimport_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy imported-file state reprocessing requires an explicit flag."""

    config_path = tmp_path / "scan_dirs.yml"
    config_path.write_text("reimport_legacy_import_state: true\n", encoding="utf-8")

    monkeypatch.setattr(import_scan, "SCAN_CONFIG_PATH", config_path)

    assert import_scan.load_reimport_legacy_import_state() is True


def test_import_config_loads_duplicate_project_cleanup_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Obsolete duplicate Project cleanup is enabled by default and configurable."""

    config_path = tmp_path / "scan_dirs.yml"
    config_path.write_text(
        "cleanup_obsolete_duplicate_projects: false\n", encoding="utf-8"
    )

    monkeypatch.setattr(import_scan, "SCAN_CONFIG_PATH", config_path)

    assert import_scan.load_cleanup_obsolete_duplicate_projects() is False


def test_import_state_keys_include_owner_and_group() -> None:
    """Project/dataset state is scoped to avoid reusing old ownership."""

    assert (
        import_scan.state_scope_key("root_a", "habomero", "lab")
        == "root_a|owner=habomero|group=lab"
    )
    assert (
        import_scan.imported_file_key(
            "root_a",
            "habomero",
            "lab",
            "/scan/roots/root_a/image.tif",
        )
        == "root_a|owner=habomero|group=lab:/scan/roots/root_a/image.tif"
    )
    assert (
        import_scan.legacy_imported_file_key("root_a", "/scan/roots/root_a/image.tif")
        == "root_a:/scan/roots/root_a/image.tif"
    )


def test_parse_project_records() -> None:
    """Project placement rows are parsed from OMERO HQL table output."""

    records = import_scan.parse_project_records(
        " # | Col1 | Col2 | Col3 | Col4\n"
        "---+------+-------+------+------\n"
        " 0 | 51   | scan-root :: test | 53 | habomero\n"
    )

    assert records == [
        import_scan.ProjectRecord(
            project_id=51,
            name="scan-root :: test",
            group="53",
            owner="habomero",
        )
    ]


def test_list_projects_by_name_filters_in_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Duplicate cleanup avoids HQL literals for names containing colons."""

    commands: list[str] = []

    def fake_run_as_root(command: str) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=(
                " # | Col1 | Col2 | Col3 | Col4\n"
                "---+------+-------+------+------\n"
                " 0 | 10   | scan-root :: other | 1 | habomero\n"
                " 1 | 51   | scan-root :: test | 53 | habomero\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(import_scan, "run_as_root", fake_run_as_root)

    records = import_scan.list_projects_by_name("scan-root :: test")

    assert records == [
        import_scan.ProjectRecord(51, "scan-root :: test", "53", "habomero")
    ]
    assert "p.name =" not in commands[0]
    assert "scan-root :: test" not in commands[0]
    assert "details.group.id" in commands[0]
    assert "details.group.name" not in commands[0]


def test_root_cleanup_commands_retry_transient_session_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Root cleanup commands retry when OMERO is temporarily not initialized."""

    attempts: list[str] = []

    def fake_run_as_root(command: str) -> subprocess.CompletedProcess[str]:
        attempts.append(command)
        if len(attempts) == 1:
            return subprocess.CompletedProcess(
                args=command,
                returncode=1,
                stdout="",
                stderr="ApiUsageException:Server not fully initialized",
            )
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="ok",
            stderr="",
        )

    monkeypatch.setattr(import_scan, "run_as_root", fake_run_as_root)
    monkeypatch.setattr(import_scan.time, "sleep", lambda seconds: None)

    result = import_scan.run_as_root_with_retry("hql test")

    assert result.returncode == 0
    assert attempts == ["hql test", "hql test"]


def test_cleanup_obsolete_duplicate_projects_keeps_current_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Duplicate cleanup removes same-named Projects except the configured one."""

    deleted: list[int] = []

    monkeypatch.setattr(
        import_scan,
        "list_projects_by_name",
        lambda name: [
            import_scan.ProjectRecord(
                10, "scan-root :: Way_McKinsey_Cardiac_Fibrosis", "1", "habomero"
            ),
            import_scan.ProjectRecord(
                51, "scan-root :: Way_McKinsey_Cardiac_Fibrosis", "53", "habomero"
            ),
        ],
    )
    monkeypatch.setattr(
        import_scan,
        "delete_project",
        lambda project_id: not deleted.append(project_id),
    )

    count = import_scan.cleanup_obsolete_duplicate_projects(
        "habomero",
        "way_mckinsey_cardiac_fibrosis",
        "scan-root",
        "/home/davebunten/mnt/Way_McKinsey_Cardiac_Fibrosis",
        51,
    )

    assert count == 1
    assert deleted == [10]


def test_parse_dataset_records() -> None:
    """Dataset rows are parsed from OMERO HQL table output."""

    records = import_scan.parse_dataset_records(
        " # | Col1 | Col2\n---+------+------\n 0 | 101  | SPLAT_data :: pilot_images\n"
    )

    assert records == [
        import_scan.DatasetRecord(
            dataset_id=101,
            name="SPLAT_data :: pilot_images",
        )
    ]


def test_parse_group_records() -> None:
    """Group rows are parsed from OMERO HQL table output."""

    records = import_scan.parse_group_records(
        " # | Col1 | Col2\n"
        "---+------+------\n"
        " 0 | 53   | way_mckinsey_cardiac_fibrosis\n"
    )

    assert records == [
        import_scan.GroupRecord(
            group_id=53,
            name="way_mckinsey_cardiac_fibrosis",
        )
    ]


def test_reconcile_project_id_prefers_configured_owner_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale local Project state is updated to configured OMERO placement."""

    configured_project_id = 51
    project_state = {"root_a|owner=habomero|group=way_mckinsey": 10}

    monkeypatch.setattr(
        import_scan,
        "list_projects_by_name",
        lambda name: [
            import_scan.ProjectRecord(10, name, "1", "habomero"),
            import_scan.ProjectRecord(configured_project_id, name, "53", "habomero"),
        ],
    )
    monkeypatch.setattr(import_scan, "group_ids_by_name", lambda group: {"53"})
    monkeypatch.setattr(import_scan, "project_dataset_count", lambda project_id: 0)

    project_id = import_scan.reconcile_project_id(
        "habomero",
        "way_mckinsey",
        "root_a",
        "scan-root",
        "/mnt/Way_McKinsey_Cardiac_Fibrosis",
        10,
        project_state,
    )

    assert project_id == configured_project_id
    assert (
        project_state["root_a|owner=habomero|group=way_mckinsey"]
        == configured_project_id
    )


def test_reconcile_project_id_prefers_project_with_more_datasets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When placement matches, the Project with more child Datasets is kept."""

    richer_project_id = 51
    project_state = {"root_a|owner=habomero|group=way_mckinsey": 10}

    monkeypatch.setattr(
        import_scan,
        "list_projects_by_name",
        lambda name: [
            import_scan.ProjectRecord(10, name, "53", "habomero"),
            import_scan.ProjectRecord(richer_project_id, name, "53", "habomero"),
        ],
    )
    monkeypatch.setattr(import_scan, "group_ids_by_name", lambda group: {"53"})
    monkeypatch.setattr(
        import_scan,
        "list_project_datasets",
        lambda project_id: [
            import_scan.DatasetRecord(100, "one"),
            import_scan.DatasetRecord(101, "two"),
            import_scan.DatasetRecord(102, "three"),
        ]
        if project_id == richer_project_id
        else [import_scan.DatasetRecord(99, "one")],
    )

    project_id = import_scan.reconcile_project_id(
        "habomero",
        "way_mckinsey",
        "root_a",
        "scan-root",
        "/mnt/Way_McKinsey_Cardiac_Fibrosis",
        10,
        project_state,
    )

    assert project_id == richer_project_id
    assert (
        project_state["root_a|owner=habomero|group=way_mckinsey"] == richer_project_id
    )


def test_cleanup_obsolete_duplicate_datasets_keeps_current_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dataset cleanup removes duplicate folder names under the kept Project."""

    deleted: list[int] = []

    monkeypatch.setattr(
        import_scan,
        "list_project_datasets",
        lambda project_id: [
            import_scan.DatasetRecord(100, "SPLAT_data :: pilot_images"),
            import_scan.DatasetRecord(101, "SPLAT_data :: pilot_images"),
            import_scan.DatasetRecord(200, "other"),
        ],
    )
    monkeypatch.setattr(
        import_scan,
        "delete_dataset_as_root",
        lambda dataset_id: not deleted.append(dataset_id),
    )

    count = import_scan.cleanup_obsolete_duplicate_datasets(51, {101})

    assert count == 1
    assert deleted == [100]


def test_current_dataset_ids_for_root_only_current_scope() -> None:
    """Current Dataset IDs come only from the configured root owner/group scope."""

    dataset_state = {
        "root_a|owner=habomero|group=way|path/a": 101,
        "root_a|owner=legacy|group=lab|path/a": 100,
        "root_b|owner=habomero|group=way|path/a": 200,
    }

    assert import_scan.current_dataset_ids_for_root(
        dataset_state,
        "root_a",
        "habomero",
        "way",
    ) == {101}


def test_safe_restart_compose_args_include_scan_roots_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Safe restart should preserve generated scan-root compose mounts."""

    scan_roots_compose = tmp_path / "data/state/scan_roots.compose.yml"
    scan_roots_compose.parent.mkdir(parents=True)
    scan_roots_compose.write_text("services: {}\n", encoding="utf-8")

    monkeypatch.setattr(safe_restart, "SCAN_ROOTS_COMPOSE", scan_roots_compose)

    assert safe_restart.compose_args() == [
        "docker",
        "compose",
        "-f",
        "compose.yml",
        "-f",
        str(scan_roots_compose),
    ]


def test_safe_restart_database_config_reads_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Safe restart should use the same database values as Compose."""

    env_path = tmp_path / ".env"
    env_path.write_text(
        "POSTGRES_DB=custom_omero\nPOSTGRES_USER=custom_user\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("POSTGRES_DB", raising=False)
    monkeypatch.delenv("POSTGRES_USER", raising=False)
    monkeypatch.setattr(safe_restart, "ENV_PATH", env_path)

    assert safe_restart.database_config() == ("custom_omero", "custom_user")


def test_safe_restart_removes_only_repository_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Safe restart should remove OMERO repository locks without touching data."""

    repository = tmp_path / "repository"
    lock_path = repository / "uuid/.lock"
    image_path = repository / "uuid/image.tiff"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text("stale", encoding="utf-8")
    image_path.write_text("pixels", encoding="utf-8")

    monkeypatch.setattr(safe_restart, "OMERO_REPOSITORY_ROOT", repository)

    removed = safe_restart.remove_stale_lock_files()

    assert removed == [lock_path]
    assert not lock_path.exists()
    assert image_path.read_text(encoding="utf-8") == "pixels"


def test_safe_restart_uses_root_helper_for_protected_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Safe restart should handle lock files owned by container users."""

    class ProtectedLock:
        def unlink(self) -> None:
            raise PermissionError("denied")

    lock_path = ProtectedLock()
    protected: list[Path] = []

    def fake_root_helper(lock_paths: list[Path]) -> None:
        protected.extend(lock_paths)

    monkeypatch.setattr(safe_restart, "stale_lock_files", lambda: [lock_path])
    monkeypatch.setattr(safe_restart, "remove_locks_with_root_helper", fake_root_helper)

    removed = safe_restart.remove_stale_lock_files()

    assert removed == [lock_path]
    assert protected == [lock_path]


def test_load_dataset_image_ids_by_name_parses_hql_table_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`omero hql` renders a `|`-delimited table, not bracket notation.

    Regression test: the previous `\\[(\\d+)\\]` regex never matched the CLI's real
    output format and silently returned an empty list for every query.
    """

    table_output = " # | Col1  \n---+-------\n 0 | 55912 \n(1 row)\n"
    monkeypatch.setattr(
        import_scan,
        "run_as_user_with_retry",
        lambda *args: subprocess.CompletedProcess("", 0, table_output, ""),
    )

    ids = import_scan.load_dataset_image_ids_by_name(
        "habomero", "pw", "lab", 10, "some_file.TIF"
    )

    assert ids == [55912]


def test_load_dataset_image_ids_by_name_handles_multiple_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple matching rows all get parsed, not just the first."""

    table_output = " # | Col1  \n---+-------\n 0 | 111 \n 1 | 222 \n(2 rows)\n"
    monkeypatch.setattr(
        import_scan,
        "run_as_user_with_retry",
        lambda *args: subprocess.CompletedProcess("", 0, table_output, ""),
    )

    ids = import_scan.load_dataset_image_ids_by_name(
        "habomero", "pw", "lab", 10, "some_file.TIF"
    )

    assert ids == [111, 222]


def test_load_dataset_image_ids_by_name_empty_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zero-row result parses to an empty list, not an error."""

    table_output = " # | Col1 \n---+------\n(0 rows)\n"
    monkeypatch.setattr(
        import_scan,
        "run_as_user_with_retry",
        lambda *args: subprocess.CompletedProcess("", 0, table_output, ""),
    )

    ids = import_scan.load_dataset_image_ids_by_name(
        "habomero", "pw", "lab", 10, "some_file.TIF"
    )

    assert ids == []


def test_get_or_create_plate_reuses_existing_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Plate already tracked in state is reused instead of recreated."""

    commands: list[str] = []

    def fake_run_as_user_with_retry(
        owner: str, password: str, command: str, group: str
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "Plate:5", "")

    monkeypatch.setattr(
        import_scan, "run_as_user_with_retry", fake_run_as_user_with_retry
    )
    monkeypatch.setattr(import_scan, "find_plate_id_by_name", lambda *args: None)

    expected_plate_id = 5
    plate_state: dict[str, int] = {}
    first = import_scan.get_or_create_plate(
        "habomero", "pw", "lab", "root_a", "DMSO_Plate/PLATE1", plate_state
    )
    second = import_scan.get_or_create_plate(
        "habomero", "pw", "lab", "root_a", "DMSO_Plate/PLATE1", plate_state
    )

    assert first == expected_plate_id
    assert second == expected_plate_id
    assert len(commands) == 1  # second call reused state, no new `omero obj new`


def test_get_or_create_plate_self_heals_stale_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty cache doesn't create a duplicate Plate if one already exists.

    Regression test for the Plate:7/Plate:12 duplicate: an interrupted run can
    lose plate_state before it's checkpointed, even though the Plate was already
    created in OMERO. Unlike Well, Plate has no unique DB constraint to catch
    this loudly, so the defensive lookup is the only thing preventing a silent
    duplicate.
    """

    create_calls: list[str] = []

    def fake_run_as_user_with_retry(
        owner: str, password: str, command: str, group: str
    ) -> subprocess.CompletedProcess[str]:
        create_calls.append(command)
        return subprocess.CompletedProcess(command, 0, "Plate:999", "")

    monkeypatch.setattr(
        import_scan, "run_as_user_with_retry", fake_run_as_user_with_retry
    )
    expected_plate_id = 42
    monkeypatch.setattr(
        import_scan, "find_plate_id_by_name", lambda *args: expected_plate_id
    )

    plate_id = import_scan.get_or_create_plate(
        "habomero", "pw", "lab", "root_a", "DMSO_Plate/PLATE1", {}
    )

    assert plate_id == expected_plate_id
    assert create_calls == []  # found via lookup, never called `omero obj new`


def test_find_plate_id_by_name_parses_hql_table_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """find_plate_id_by_name parses the real `|`-delimited HQL table format."""

    table_output = (
        " # | Col1 | Col2                    | Col3   \n"
        "---+------+-------------------------+--------\n"
        " 0 | 11   | Other_Plate              | None   \n"
        " 1 | 12   | DMSO_Plate :: PLATE1     | None   \n"
        "(2 rows)\n"
    )
    monkeypatch.setattr(
        import_scan,
        "run_as_user_with_retry",
        lambda *args: subprocess.CompletedProcess("", 0, table_output, ""),
    )

    expected_plate_id = 12
    plate_id = import_scan.find_plate_id_by_name(
        "habomero",
        "pw",
        "lab",
        "DMSO_Plate :: PLATE1",
        "root_a|owner=habomero|group=lab|DMSO_Plate/PLATE1",
    )

    assert plate_id == expected_plate_id


def test_find_plate_id_by_name_rejects_mismatched_root_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A name match from a different root's scope key is not reused."""

    other_scope = "root_b|owner=x|group=lab|DMSO_Plate/PLATE1"
    table_output = (
        " # | Col1 | Col2                    | Col3   \n"
        "---+------+-------------------------+--------\n"
        f" 0 | 12   | DMSO_Plate :: PLATE1     | {other_scope} \n"
        "(1 rows)\n"
    )
    monkeypatch.setattr(
        import_scan,
        "run_as_user_with_retry",
        lambda *args: subprocess.CompletedProcess("", 0, table_output, ""),
    )

    plate_id = import_scan.find_plate_id_by_name(
        "habomero",
        "pw",
        "lab",
        "DMSO_Plate :: PLATE1",
        "root_a|owner=habomero|group=lab|DMSO_Plate/PLATE1",
    )

    assert plate_id is None


def test_find_plate_id_by_name_returns_none_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A name with no matching Plate returns None rather than a false positive."""

    table_output = " # | Col1 | Col2 | Col3 \n---+------+------+------\n(0 rows)\n"
    monkeypatch.setattr(
        import_scan,
        "run_as_user_with_retry",
        lambda *args: subprocess.CompletedProcess("", 0, table_output, ""),
    )

    plate_id = import_scan.find_plate_id_by_name(
        "habomero", "pw", "lab", "Nonexistent", "root_a|owner=habomero|group=lab|x"
    )

    assert plate_id is None


def test_get_or_create_well_and_add_sample_creates_then_appends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First call creates a Well+WellSample; second appends a sample to it."""

    scripts_run: list[str] = []

    def fake_run_python(
        owner: str, password: str, group: str, script: str
    ) -> subprocess.CompletedProcess[str]:
        scripts_run.append(script)
        if "well_id = 0" in script:
            return subprocess.CompletedProcess(
                script, 0, "WELL_ID=10\nWELL_SAMPLE_ID=100\n", ""
            )
        return subprocess.CompletedProcess(
            script, 0, "WELL_ID=10\nWELL_SAMPLE_ID=101\n", ""
        )

    monkeypatch.setattr(import_scan, "run_python_as_user_with_retry", fake_run_python)

    well_state: dict[str, int] = {}
    well_id_1, sample_id_1 = import_scan.get_or_create_well_and_add_sample(
        "habomero", "pw", "lab", 3, "B02", 111, well_state
    )
    well_id_2, sample_id_2 = import_scan.get_or_create_well_and_add_sample(
        "habomero", "pw", "lab", 3, "B02", 112, well_state
    )

    assert (well_id_1, sample_id_1) == (10, 100)
    assert (well_id_2, sample_id_2) == (10, 101)
    assert well_state == {"3|B02": 10}
    assert "well_id = 0" in scripts_run[0]
    assert "well_id = 10" in scripts_run[1]
    assert "PlateI(3, False)" in scripts_run[0]
    assert "rint(1)" in scripts_run[0]  # row for 'B'


def test_get_or_create_well_and_add_sample_raises_on_unparseable_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed script response surfaces as a clear error, not a silent bad state."""

    monkeypatch.setattr(
        import_scan,
        "run_python_as_user_with_retry",
        lambda *args: subprocess.CompletedProcess("", 0, "unexpected output", ""),
    )

    with pytest.raises(RuntimeError, match="Could not parse Well/WellSample id"):
        import_scan.get_or_create_well_and_add_sample(
            "habomero", "pw", "lab", 3, "B02", 111, {}
        )


def test_hcs_parse_filename_matches_thermo_cx7_pattern() -> None:
    """A well-formed Thermo CX7 filename stem parses into its components."""

    info = hcs.parse_filename("CARD-CelIns-CX7_260803130001_B02f00d0")

    assert info == hcs.HcsFileInfo(
        computer="CARD-CelIns-CX7",
        plate_id="260803130001",
        well="B02",
        field="f00",
        channel=0,
    )


def test_hcs_parse_filename_rejects_non_matching_names() -> None:
    """Filenames outside the Thermo CX7 convention are not treated as HCS data."""

    assert hcs.parse_filename("CP_SPLAT_Cell_Density_2_061626") is None
    assert hcs.parse_filename("CARD-CelIns-CX7_260803130001") is None
    assert hcs.parse_filename("CARD-CelIns-CX7_260803130001_B02f00") is None


def test_hcs_well_indices_are_zero_based() -> None:
    """Well row/column indices follow the OME model's zero-based convention."""

    last_row_index = 7  # 'H', the 8th row
    last_column_index = 11  # '12', the 12th column

    assert hcs.well_row_index("B02") == 1
    assert hcs.well_row_index("A01") == 0
    assert hcs.well_row_index("H12") == last_row_index
    assert hcs.well_column_index("B02") == 1
    assert hcs.well_column_index("A01") == 0
    assert hcs.well_column_index("H12") == last_column_index


def test_hcs_field_grid_matches_spiral_reference() -> None:
    """Field grid positions match the user-supplied center-out spiral layout."""

    expected_rows = [
        ["f20", "f21", "f22", "f23", "f24"],
        ["f19", "f06", "f07", "f08", "f09"],
        ["f18", "f05", "f00", "f01", "f10"],
        ["f17", "f04", "f03", "f02", "f11"],
        ["f16", "f15", "f14", "f13", "f12"],
    ]
    for row_index, tokens in enumerate(expected_rows):
        for col_index, token in enumerate(tokens):
            assert hcs.field_grid_position(token) == (row_index, col_index)


def test_hcs_field_grid_position_rejects_unknown_field() -> None:
    """A field index outside the 25-field grid raises a clear error."""

    with pytest.raises(hcs.HcsError, match="Unknown field index"):
        hcs.field_grid_position("f25")


def _write_minimal_tiff(
    path: Path, width: int, height: int, bits_per_sample: int, sample_format: int | None
) -> None:
    """Write a minimal little-endian TIFF IFD header (no pixel data) for testing."""

    tags = [(256, 3, width), (257, 3, height), (258, 3, bits_per_sample)]
    if sample_format is not None:
        tags.append((339, 3, sample_format))
    ifd_offset = 8
    header = struct.pack("<2sHI", b"II", 42, ifd_offset)
    entry_count = struct.pack("<H", len(tags))
    entries = b"".join(
        struct.pack("<HHII", tag, typ, 1, value) for tag, typ, value in tags
    )
    next_ifd = struct.pack("<I", 0)
    path.write_bytes(header + entry_count + entries + next_ifd)


def test_hcs_read_tiff_geometry_parses_minimal_tiff(tmp_path: Path) -> None:
    """TIFF geometry is read correctly from a minimal single-IFD TIFF."""

    tiff_path = tmp_path / "test.tif"
    _write_minimal_tiff(tiff_path, 1104, 1104, 16, sample_format=None)

    width, height, bits_per_sample, sample_format = hcs.read_tiff_geometry(tiff_path)

    assert (width, height, bits_per_sample) == (1104, 1104, 16)
    assert sample_format == 1  # defaults to unsigned int when tag is absent


def test_hcs_read_tiff_geometry_rejects_non_tiff(tmp_path: Path) -> None:
    """A non-TIFF file is rejected with a clear error."""

    bogus = tmp_path / "not_a_tiff.tif"
    bogus.write_bytes(b"not a tiff file")

    with pytest.raises(hcs.HcsError, match="Not a TIFF file"):
        hcs.read_tiff_geometry(bogus)


def test_hcs_ome_pixel_type_maps_common_cases() -> None:
    """TIFF BitsPerSample/SampleFormat map to the correct OME Pixels Type."""

    assert hcs.ome_pixel_type(16, sample_format=1) == "uint16"
    assert hcs.ome_pixel_type(8, sample_format=1) == "uint8"
    assert hcs.ome_pixel_type(16, sample_format=2) == "int16"
    assert hcs.ome_pixel_type(32, sample_format=3) == "float"


def test_hcs_ome_pixel_type_rejects_unsupported_bit_depth() -> None:
    """An unsupported bit depth raises a clear error rather than silently mapping."""

    with pytest.raises(hcs.HcsError, match="Unsupported bits-per-sample"):
        hcs.ome_pixel_type(12, sample_format=1)


def test_hcs_generate_companion_xml_structure() -> None:
    """The companion XML declares the right channel count and file references."""

    channel_count = 5
    xml_text = hcs.generate_companion_xml(
        image_name="B02_f00",
        size_x=1104,
        size_y=1104,
        pixel_type="uint16",
        channel_relative_paths=[f"source/ch{c}.TIF" for c in range(channel_count)],
    )

    root = ET.fromstring(xml_text)
    ns = {"ome": hcs.OME_NAMESPACE}
    pixels = root.find("ome:Image/ome:Pixels", ns)
    assert pixels is not None
    assert pixels.get("SizeC") == str(channel_count)
    assert pixels.get("SizeX") == "1104"
    assert pixels.get("Type") == "uint16"
    channels = pixels.findall("ome:Channel", ns)
    assert len(channels) == channel_count
    tiffdata = pixels.findall("ome:TiffData", ns)
    assert len(tiffdata) == channel_count
    file_names = [
        td.find("ome:UUID", ns).get("FileName")  # type: ignore[union-attr]
        for td in tiffdata
    ]
    assert file_names == [f"source/ch{c}.TIF" for c in range(channel_count)]


def test_hcs_generate_companion_xml_rejects_parent_traversal() -> None:
    """Relative paths containing '..' are rejected before they can break checksums."""

    with pytest.raises(hcs.HcsError, match=r"must not contain '\.\.'"):
        hcs.generate_companion_xml(
            image_name="B02_f00",
            size_x=1104,
            size_y=1104,
            pixel_type="uint16",
            channel_relative_paths=["../../scan/roots/root_a/ch0.TIF"],
        )


def test_hcs_ensure_shadow_symlink_is_idempotent(tmp_path: Path) -> None:
    """A second call for the same plate reuses the symlink instead of recreating it."""

    real_plate_dir = tmp_path / "real" / "plate1"
    real_plate_dir.mkdir(parents=True)
    shadow_root = tmp_path / "shadow"

    first = hcs.ensure_shadow_symlink(
        shadow_root, "root_a", "plate1", str(real_plate_dir)
    )
    second = hcs.ensure_shadow_symlink(
        shadow_root, "root_a", "plate1", str(real_plate_dir)
    )

    assert first == second
    assert (first / "source").resolve() == real_plate_dir.resolve()


def test_hcs_ensure_shadow_symlink_rejects_conflicting_target(tmp_path: Path) -> None:
    """A shadow symlink pointing elsewhere is a hard error, not silently rebound."""

    real_a = tmp_path / "real_a"
    real_a.mkdir()
    real_b = tmp_path / "real_b"
    real_b.mkdir()
    shadow_root = tmp_path / "shadow"

    hcs.ensure_shadow_symlink(shadow_root, "root_a", "plate1", str(real_a))

    with pytest.raises(hcs.HcsError, match="already points elsewhere"):
        hcs.ensure_shadow_symlink(shadow_root, "root_a", "plate1", str(real_b))


def test_strip_supported_extension_removes_recognized_extensions() -> None:
    """Only recognized image extensions are stripped, case-insensitively."""

    assert import_scan.strip_supported_extension("foo.TIF") == "foo"
    assert import_scan.strip_supported_extension("foo.tiff") == "foo"
    assert import_scan.strip_supported_extension("foo.txt") == "foo.txt"


def test_hcs_field_state_key_is_scoped_and_stable() -> None:
    """The HCS field tracking key encodes root/owner/group/plate/well/field."""

    key = import_scan.hcs_field_state_key(
        "root_a", "habomero", "lab", "DMSO_Plate/PLATE1", "B02", "f00"
    )

    assert key == "root_a|owner=habomero|group=lab|DMSO_Plate/PLATE1:B02:f00"


def test_partition_hcs_candidates_groups_ready_fields(tmp_path: Path) -> None:
    """A complete channel set becomes a ready group; non-matching files pass through."""

    container_root = "/scan/roots/root_a"
    root_files = [
        f"{container_root}/Plate1/CARD-CelIns-CX7_260803130001_B02f00d0.TIF",
        f"{container_root}/Plate1/CARD-CelIns-CX7_260803130001_B02f00d1.TIF",
        f"{container_root}/other_experiment/some_image.tif",
    ]

    ready, flat_files = import_scan.partition_hcs_candidates(
        root_files, container_root, hcs_channels=2
    )

    assert len(ready) == 1
    group = ready[0]
    assert group.rel_dir == "Plate1"
    assert group.well == "B02"
    assert group.field == "f00"
    assert group.plate_id == "260803130001"
    assert group.channel_files == {
        0: f"{container_root}/Plate1/CARD-CelIns-CX7_260803130001_B02f00d0.TIF",
        1: f"{container_root}/Plate1/CARD-CelIns-CX7_260803130001_B02f00d1.TIF",
    }
    assert flat_files == [f"{container_root}/other_experiment/some_image.tif"]


def test_partition_hcs_candidates_omits_incomplete_groups(tmp_path: Path) -> None:
    """A field short of its configured channel count is skipped this cycle."""

    container_root = "/scan/roots/root_a"
    root_files = [
        f"{container_root}/Plate1/CARD-CelIns-CX7_260803130001_B02f00d0.TIF",
    ]

    ready, flat_files = import_scan.partition_hcs_candidates(
        root_files, container_root, hcs_channels=5
    )

    assert ready == []
    assert flat_files == []  # not flat-imported either; retried once complete


def test_cleanup_old_flat_field_images_deletes_and_untracks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Old flat images for a merged field are deleted and their tracking dropped."""

    deleted_ids: list[int] = []
    monkeypatch.setattr(
        import_scan,
        "load_dataset_image_ids_by_name",
        lambda *args: [42],
    )
    monkeypatch.setattr(
        import_scan,
        "delete_image",
        lambda *args: deleted_ids.append(args[-1]) or True,
    )
    monkeypatch.setattr(
        import_scan,
        "delete_now_empty_datasets",
        lambda *args: None,
    )

    channel_files = {
        0: "/scan/roots/root_a/Plate1/CARD-CelIns-CX7_260803130001_B02f00d0.TIF",
        1: "/scan/roots/root_a/Plate1/CARD-CelIns-CX7_260803130001_B02f00d1.TIF",
    }
    imported = {
        import_scan.imported_file_key("root_a", "habomero", "lab", channel_files[0]),
        import_scan.imported_file_key("root_a", "habomero", "lab", channel_files[1]),
        import_scan.imported_file_key("root_a", "habomero", "lab", "/keep.TIF"),
    }
    dataset_state = {"root_a|owner=habomero|group=lab|Plate1": 99}

    import_scan.cleanup_old_flat_field_images(
        "habomero",
        "pw",
        "lab",
        "root_a",
        "Plate1",
        channel_files,
        imported,
        dataset_state,
    )

    assert deleted_ids == [42, 42]
    assert imported == {
        import_scan.imported_file_key("root_a", "habomero", "lab", "/keep.TIF")
    }


def test_process_hcs_candidates_orchestrates_plate_well_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ready field group creates a Plate/Well/Image, then old flat copies cleaned."""

    calls: list[str] = []
    monkeypatch.setattr(
        import_scan, "HCS_FIELD_STATE_PATH", tmp_path / "hcs_fields.txt"
    )
    monkeypatch.setattr(
        import_scan,
        "get_or_create_plate",
        lambda *args: (calls.append("plate"), 7)[1],
    )
    monkeypatch.setattr(
        import_scan,
        "import_hcs_field",
        lambda *args: (calls.append("import"), 200)[1],
    )
    monkeypatch.setattr(
        import_scan,
        "get_or_create_well_and_add_sample",
        lambda *args: (calls.append("well"), (8, 80))[1],
    )
    monkeypatch.setattr(
        import_scan,
        "cleanup_old_flat_field_images",
        lambda *args: calls.append("cleanup"),
    )

    container_root = "/scan/roots/root_a"
    root_files = [
        f"{container_root}/Plate1/CARD-CelIns-CX7_260803130001_B02f00d0.TIF",
        f"{container_root}/Plate1/CARD-CelIns-CX7_260803130001_B02f00d1.TIF",
        f"{container_root}/other/plain_image.tif",
    ]
    imported: set[str] = set()
    dataset_state: dict[str, int] = {}
    hcs_imported: set[str] = set()
    hcs_plate_state: dict[str, int] = {}
    hcs_well_state: dict[str, int] = {}

    remaining = import_scan.process_hcs_candidates(
        "habomero",
        "pw",
        "lab",
        "root_a",
        container_root,
        2,
        root_files,
        imported,
        dataset_state,
        hcs_imported,
        hcs_plate_state,
        hcs_well_state,
    )

    assert remaining == [f"{container_root}/other/plain_image.tif"]
    assert calls == ["plate", "import", "well", "cleanup"]
    assert hcs_imported == {
        import_scan.hcs_field_state_key(
            "root_a", "habomero", "lab", "Plate1", "B02", "f00"
        )
    }

    # A second pass is a no-op for the already-processed field (idempotent).
    calls.clear()
    remaining_again = import_scan.process_hcs_candidates(
        "habomero",
        "pw",
        "lab",
        "root_a",
        container_root,
        2,
        root_files,
        imported,
        dataset_state,
        hcs_imported,
        hcs_plate_state,
        hcs_well_state,
    )
    assert calls == []
    assert remaining_again == [f"{container_root}/other/plain_image.tif"]


def test_process_hcs_candidates_disabled_returns_all_files_unchanged() -> None:
    """hcs_channels <= 0 (HCS disabled for this root) is a complete pass-through."""

    root_files = ["/scan/roots/root_a/anything.tif"]

    remaining = import_scan.process_hcs_candidates(
        "habomero",
        "pw",
        "lab",
        "root_a",
        "/scan/roots/root_a",
        0,
        root_files,
        set(),
        {},
        set(),
        {},
        {},
    )

    assert remaining == root_files
