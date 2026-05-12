"""Run import_scan in safe rounds for high-latency remote sources."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCAN_CONFIG_PATH = PROJECT_ROOT / "config/omero/scan_dirs.yml"
IMPORTED_STATE_PATH = PROJECT_ROOT / "data/state/imported_files.txt"
SCAN_ROOTS_STATE_PATH = PROJECT_ROOT / "data/state/scan_roots.yml"

DEFAULT_ROUNDS = 30
DEFAULT_PAUSE_SECONDS = 20
DEFAULT_STAGNANT_ROUNDS = 3


def line_count(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for _ in path.open("r", encoding="utf-8"))


def load_safe_round_config() -> tuple[int, int, int]:
    rounds = DEFAULT_ROUNDS
    pause_seconds = DEFAULT_PAUSE_SECONDS
    stagnant_rounds = DEFAULT_STAGNANT_ROUNDS
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
    return rounds, pause_seconds, stagnant_rounds


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


def run_import_once() -> tuple[bool, bool]:
    """Run import scan once and return (saw_import_summary, had_new_imports)."""

    roots = "unknown"
    if SCAN_ROOTS_STATE_PATH.exists():
        payload = yaml.safe_load(SCAN_ROOTS_STATE_PATH.read_text(encoding="utf-8")) or {}
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
    process = subprocess.Popen(
        ["python", "scripts/import_scan.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
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


def main() -> None:
    rounds, pause_seconds, stagnant_rounds = load_safe_round_config()
    stagnant = 0
    print(
        "[safe-import] config "
        f"rounds={rounds} pause={pause_seconds}s stagnant_limit={stagnant_rounds}"
    )

    for round_index in range(1, rounds + 1):
        before = line_count(IMPORTED_STATE_PATH)
        print(f"[safe-import] round {round_index}/{rounds} (before={before})")
        run_healthcheck()
        saw_summary, had_new_imports = run_import_once()
        after = line_count(IMPORTED_STATE_PATH)
        print(f"[safe-import] round {round_index} complete (after={after})")
        if saw_summary and not had_new_imports:
            print("[safe-import] stopping: import reported no new files")
            return
        if after > before:
            stagnant = 0
        else:
            stagnant += 1
            if stagnant >= stagnant_rounds:
                print(
                    "[safe-import] stopping due to stagnant progress "
                    f"for {stagnant_rounds} round(s)"
                )
                return
        if round_index < rounds:
            print(f"[safe-import] pausing {pause_seconds}s before next round")
            time.sleep(pause_seconds)


if __name__ == "__main__":
    main()
