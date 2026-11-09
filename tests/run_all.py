"""Run every check the project ships with, offline.

    python tests/run_all.py
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKS = [
    ("unit tests (links, engine, and the three reported-bug regressions)",
     [sys.executable, "-m", "unittest", "discover", "-s", "tests",
      "-p", "test_*.py"]),
    ("interface smoke test (join controls, per-account overrides, bot bridge)",
     [sys.executable, "tests/smoke_ui.py"]),
    ("page layout (dropdown, Settings page, Start feedback)",
     [sys.executable, "tests/layout_checks.py"]),
    ("app launch (offscreen, stubbed Roblox)", [sys.executable,
                                                "tests/launch_offscreen.py"]),
    ("all modules compile", [sys.executable, "-m", "compileall", "-q",
                             "src", "tests"]),
]


def main():
    failures = 0
    for label, cmd in CHECKS:
        print(f"\n=== {label} ===")
        result = subprocess.run(cmd, cwd=ROOT)
        if result.returncode != 0:
            failures += 1
            print(f"!!! FAILED: {label}")
    print()
    if failures:
        print(f"{failures} check(s) failed")
        return 1
    print(f"All {len(CHECKS)} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
