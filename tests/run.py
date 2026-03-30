#!/usr/bin/env python3
"""Integrated test runner for quark/triskelion.

Tier 1: Pure unit tests (no Wine, no processes) — always safe
Tier 2: Triskelion integration (launches Wine) — scoped kill only
Tier 3: Stock comparison (diffs vs reference artifacts) — no stock launch

Usage:
    python3 tests/run.py              # Tier 1 + 2
    python3 tests/run.py --fast       # Tier 1 only (seconds)
    python3 tests/run.py --compare    # Tier 1 + 2 + 3
    python3 tests/run.py --tier 1     # Specific tier
    python3 tests/run.py --tier 3     # Stock comparison only
    python3 tests/run.py --continue   # Don't stop on first failure
"""

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from util import kill_quark_processes, REFERENCE_DIR, _USER_HOME

TESTS_DIR = Path(__file__).parent
REPO_ROOT = TESTS_DIR.parent

# ── Tier definitions ──

TIER_1 = {
    "name": "Unit Tests (no Wine)",
    "tests": [
        ("test_ntsync", [sys.executable, str(TESTS_DIR / "test_ntsync.py")]),
        ("test_eventloop_ntsync", [sys.executable, str(TESTS_DIR / "test_eventloop_ntsync.py")]),
        ("test_window_shm", [sys.executable, str(TESTS_DIR / "test_window_shm.py")]),
        ("test_package", [sys.executable, str(TESTS_DIR / "test_package.py")]),
    ],
    "prereqs": ["/dev/ntsync"],
}

TIER_2 = {
    "name": "Triskelion Integration",
    "tests": [
        ("iterate", [sys.executable, str(TESTS_DIR / "iterate.py"),
                      "--skip-build", "--timeout", "20",
                      "--appid", "2379780"]),
    ],
    "prereqs": [str(_USER_HOME / ".local/share/Steam/compatibilitytools.d/quark/proton")],
}

TIER_3 = {
    "name": "Stock Comparison (vs reference)",
    "tests": [
        ("protocol_conformance", [sys.executable, str(TESTS_DIR / "protocol_conformance.py")]),
        ("diff_display_registry", [sys.executable, str(TESTS_DIR / "diff_display_registry.py")]),
        ("trace_display_init", [sys.executable, str(TESTS_DIR / "trace_display_init.py")]),
    ],
    "prereqs": [str(REFERENCE_DIR / "metadata.json")],
}


def check_prereqs(tier):
    """Check prerequisites for a tier. Returns list of missing items."""
    missing = []
    for path in tier.get("prereqs", []):
        if not Path(path).exists():
            missing.append(path)
    return missing


def run_tier(tier, continue_on_error=False):
    """Run all tests in a tier. Returns (passed, failed, skipped)."""
    passed, failed, skipped = 0, 0, 0
    name = tier["name"]

    missing = check_prereqs(tier)
    if missing:
        print(f"\n  SKIP {name}: missing prerequisites:")
        for m in missing:
            print(f"    - {m}")
        return 0, 0, len(tier["tests"])

    print(f"\n{'─' * 60}")
    print(f"  {name}")
    print(f"{'─' * 60}")

    for test_name, cmd in tier["tests"]:
        # Safety barrier between tests
        kill_quark_processes()

        print(f"\n  [{test_name}] ", end="", flush=True)
        t0 = time.time()
        try:
            result = subprocess.run(
                cmd, cwd=str(REPO_ROOT),
                capture_output=True, text=True,
                timeout=300,
            )
            elapsed = time.time() - t0
            if result.returncode == 0:
                print(f"PASS ({elapsed:.1f}s)")
                passed += 1
            else:
                print(f"FAIL ({elapsed:.1f}s)")
                # Show last 10 lines of output
                output = (result.stdout + result.stderr).strip().splitlines()
                for line in output[-10:]:
                    print(f"    {line}")
                failed += 1
                if not continue_on_error:
                    print(f"\n  Stopping tier on first failure. Use --continue to keep going.")
                    return passed, failed, skipped
        except subprocess.TimeoutExpired:
            print(f"TIMEOUT (300s)")
            failed += 1
            if not continue_on_error:
                return passed, failed, skipped

    return passed, failed, skipped


def main():
    args = set(sys.argv[1:])
    continue_on_error = "--continue" in args
    fast = "--fast" in args
    compare = "--compare" in args

    # Specific tier selection
    specific_tier = None
    if "--tier" in args:
        idx = sys.argv.index("--tier")
        if idx + 1 < len(sys.argv):
            specific_tier = int(sys.argv[idx + 1])

    print("=" * 60)
    print("  quark test runner")
    print("=" * 60)

    total_passed, total_failed, total_skipped = 0, 0, 0

    tiers_to_run = []
    if specific_tier:
        tiers_to_run = [specific_tier]
    elif fast:
        tiers_to_run = [1]
    elif compare:
        tiers_to_run = [1, 2, 3]
    else:
        tiers_to_run = [1, 2]

    tier_map = {1: TIER_1, 2: TIER_2, 3: TIER_3}

    for tier_num in tiers_to_run:
        tier = tier_map.get(tier_num)
        if not tier:
            print(f"\n  Unknown tier: {tier_num}")
            continue
        p, f, s = run_tier(tier, continue_on_error)
        total_passed += p
        total_failed += f
        total_skipped += s

    # Final cleanup
    kill_quark_processes()

    # Summary
    print(f"\n{'=' * 60}")
    total = total_passed + total_failed + total_skipped
    status = "PASS" if total_failed == 0 else "FAIL"
    print(f"  {status}: {total_passed} passed, {total_failed} failed, {total_skipped} skipped ({total} total)")
    print(f"{'=' * 60}")

    return 1 if total_failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
