"""Run import_scan in safe rounds for high-latency remote sources."""

from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCAN_CONFIG_PATH = PROJECT_ROOT / "config/omero/scan_dirs.yml"
IMPORTED_STATE_PATH = PROJECT_ROOT / "data/state/imported_files.txt"
SCAN_ROOTS_STATE_PATH = PROJECT_ROOT / "data/state/scan_roots.yml"

DEFAULT_ROUNDS = 30
DEFAULT_PAUSE_SECONDS = 20
DEFAULT_STAGNANT_ROUNDS = 3
DEFAULT_CYCLE_PAUSE_SECONDS = 300


class SafeImportConfigError(ValueError):
    """Raised for invalid safe import configuration."""


def now_utc() -> str:
    """Return a compact UTC timestamp for operational logs."""

    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def line_count(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for _ in path.open("r", encoding="utf-8"))


def parse_positive_int_env(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise SafeImportConfigError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise SafeImportConfigError(f"{name} must be a positive integer")
    return parsed


def parse_nonnegative_int_env(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise SafeImportConfigError(f"{name} must be a non-negative integer") from exc
    if parsed < 0:
        raise SafeImportConfigError(f"{name} must be a non-negative integer")
    return parsed


def parse_bool_env(name: str) -> bool | None:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return None
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise SafeImportConfigError(f"{name} must be one of: 1,true,yes,on,0,false,no,off")


def load_safe_round_config() -> tuple[int, int, int, bool, int]:  # noqa: C901
    rounds = DEFAULT_ROUNDS
    pause_seconds = DEFAULT_PAUSE_SECONDS
    stagnant_rounds = DEFAULT_STAGNANT_ROUNDS
    continuous = False
    cycle_pause_seconds = DEFAULT_CYCLE_PAUSE_SECONDS
    if SCAN_CONFIG_PATH.exists():
        payload = yaml.safe_load(SCAN_CONFIG_PATH.read_text(encoding="utf-8")) or {}
        round_val = payload.get("safe_import_rounds")
        if isinstance(round_val, int) and round_val > 0:
            rounds = round_val
        pause_val = payload.get("safe_import_pause_seconds")
        if isinstance(pause_val, int) and pause_val >= 0:
            pause_seconds = pause_val
        stagnant_val = payload.get("safe_import_stagnant_rounds")
        if isinstance(stagnant_val, int) and stagnant_val > 0:
            stagnant_rounds = stagnant_val
        continuous_val = payload.get("safe_import_continuous")
        if isinstance(continuous_val, bool):
            continuous = continuous_val
        cycle_pause_val = payload.get("safe_import_cycle_pause_seconds")
        if isinstance(cycle_pause_val, int) and cycle_pause_val >= 0:
            cycle_pause_seconds = cycle_pause_val

    env_rounds = parse_positive_int_env("SAFE_IMPORT_ROUNDS")
    if env_rounds is not None:
        rounds = env_rounds
    env_pause = parse_nonnegative_int_env("SAFE_IMPORT_PAUSE_SECONDS")
    if env_pause is not None:
        pause_seconds = env_pause
    env_stagnant = parse_positive_int_env("SAFE_IMPORT_STAGNANT_ROUNDS")
    if env_stagnant is not None:
        stagnant_rounds = env_stagnant
    env_continuous = parse_bool_env("SAFE_IMPORT_CONTINUOUS")
    if env_continuous is not None:
        continuous = env_continuous
    env_cycle_pause = parse_nonnegative_int_env("SAFE_IMPORT_CYCLE_PAUSE_SECONDS")
    if env_cycle_pause is not None:
        cycle_pause_seconds = env_cycle_pause

    return rounds, pause_seconds, stagnant_rounds, continuous, cycle_pause_seconds


def run_healthcheck() -> None:
    print("[safe-import] running healthcheck")
    result = subprocess.run(
        ["python", "scripts/healthcheck.py"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "healthcheck failed")
    print(result.stdout.strip())


def run_scan_dirs() -> None:
    started = time.perf_counter()
    print(f"[safe-import] {now_utc()} rescan-start refreshing scan roots")
    result = subprocess.run(
        ["python", "scripts/scan_dirs.py"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "scan_dirs failed")
    if result.stdout.strip():
        print(result.stdout.strip())
    duration = time.perf_counter() - started
    print(f"[safe-import] {now_utc()} rescan-done duration={duration:.1f}s")


def run_import_once(import_env: dict[str, str] | None = None) -> tuple[bool, bool]:  # noqa: C901
    """Run import scan once and return (saw_import_summary, had_new_imports)."""

    roots = "unknown"
    if SCAN_ROOTS_STATE_PATH.exists():
        payload = (
            yaml.safe_load(SCAN_ROOTS_STATE_PATH.read_text(encoding="utf-8")) or {}
        )
        if isinstance(payload, dict) and payload:
            srcs = []
            for value in payload.values():
                if isinstance(value, dict):
                    source = value.get("source")
                    if isinstance(source, str) and source.strip():
                        srcs.append(source.strip())
            if srcs:
                roots = ", ".join(srcs)
    print(f"[safe-import] running import-scan for roots: {roots}")
    env = os.environ.copy()
    if import_env:
        env.update(import_env)
    process = subprocess.Popen(
        ["python", "scripts/import_scan.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    saw_summary = False
    had_new_imports = False
    assert process.stdout is not None
    for line in process.stdout:
        text = line.rstrip()
        print(text)
        if text == "No new files to import":
            saw_summary = True
            had_new_imports = False
        if text.startswith("Imported ") and " new file(s) total" in text:
            saw_summary = True
            had_new_imports = True
    return_code = process.wait()
    if return_code != 0:
        raise RuntimeError(f"import-scan failed with exit code {return_code}")
    return saw_summary, had_new_imports


def build_import_env() -> dict[str, str]:
    """Build optional import_scan overrides from SAFE_IMPORT_* env vars."""

    mapping = {
        "SAFE_IMPORT_MAX_FILES_PER_RUN": "IMPORT_MAX_FILES_PER_RUN",
        "SAFE_IMPORT_SLEEP_BETWEEN_IMPORTS_SECONDS": (
            "IMPORT_SLEEP_BETWEEN_IMPORTS_SECONDS"
        ),
        "SAFE_IMPORT_SCAN_PROGRESS_EVERY_PATHS": "IMPORT_SCAN_PROGRESS_EVERY_PATHS",
        "SAFE_IMPORT_PROGRESS_EVERY_FILES": "IMPORT_PROGRESS_EVERY_FILES",
        "SAFE_IMPORT_WORKERS": "IMPORT_WORKERS",
    }
    out: dict[str, str] = {}
    for src, dest in mapping.items():
        value = os.environ.get(src, "").strip()
        if value:
            out[dest] = value
    return out


def run_rounds(
    rounds: int,
    pause_seconds: int,
    stagnant_rounds: int,
    import_env: dict[str, str],
) -> bool:
    """Run one bounded cycle of safe rounds.

    Returns True when new imports were detected in this cycle.
    """

    cycle_started = time.perf_counter()
    stagnant = 0
    had_new_data_in_cycle = False
    for round_index in range(1, rounds + 1):
        round_started = time.perf_counter()
        before = line_count(IMPORTED_STATE_PATH)
        print(f"[safe-import] round {round_index}/{rounds} (before={before})")
        run_scan_dirs()
        run_healthcheck()
        saw_summary, had_new_imports = run_import_once(import_env)
        after = line_count(IMPORTED_STATE_PATH)
        delta = after - before
        round_duration = time.perf_counter() - round_started
        print(f"[safe-import] round {round_index} complete (after={after})")
        print(
            "[safe-import] round-progress "
            f"round={round_index}/{rounds} delta={delta} "
            f"stagnant={stagnant}/{stagnant_rounds} "
            f"duration={round_duration:.1f}s"
        )
        if had_new_imports or after > before:
            had_new_data_in_cycle = True
        if saw_summary and not had_new_imports:
            print("[safe-import] stopping cycle: import reported no new files")
            cycle_duration = time.perf_counter() - cycle_started
            print(
                "[safe-import] cycle-summary "
                f"status=no-new-files duration={cycle_duration:.1f}s"
            )
            return had_new_data_in_cycle
        if after > before:
            stagnant = 0
        else:
            stagnant += 1
            if stagnant >= stagnant_rounds:
                print(
                    "[safe-import] stopping cycle due to stagnant progress "
                    f"for {stagnant_rounds} round(s)"
                )
                cycle_duration = time.perf_counter() - cycle_started
                print(
                    "[safe-import] cycle-summary "
                    f"status=stagnant duration={cycle_duration:.1f}s"
                )
                return had_new_data_in_cycle
        if round_index < rounds:
            print(f"[safe-import] pausing {pause_seconds}s before next round")
            time.sleep(pause_seconds)
    cycle_duration = time.perf_counter() - cycle_started
    print(
        f"[safe-import] cycle-summary status=round-limit duration={cycle_duration:.1f}s"
    )
    return had_new_data_in_cycle


def main() -> None:
    rounds, pause_seconds, stagnant_rounds, continuous, cycle_pause_seconds = (
        load_safe_round_config()
    )
    import_env = build_import_env()
    print(
        "[safe-import] config "
        f"rounds={rounds} pause={pause_seconds}s "
        f"stagnant_limit={stagnant_rounds} continuous={continuous} "
        f"cycle_pause={cycle_pause_seconds}s"
    )
    if import_env:
        print(f"[safe-import] import overrides={import_env}")

    cycle = 1
    while True:
        print(f"[safe-import] cycle {cycle} starting")
        had_new_data = run_rounds(
            rounds,
            pause_seconds,
            stagnant_rounds,
            import_env,
        )
        if not continuous:
            return
        if not had_new_data:
            print(
                "[safe-import] cycle ended without new data; "
                f"sleeping {cycle_pause_seconds}s before next rescan"
            )
        else:
            print(
                "[safe-import] cycle ended with new imports; "
                f"sleeping {cycle_pause_seconds}s before next rescan"
            )
        time.sleep(cycle_pause_seconds)
        cycle += 1


if __name__ == "__main__":
    main()
