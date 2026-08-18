"""Standalone sandbox test harness for Part 3 — not wired to the pipeline yet.

Usage:
    python scripts/sandbox_test.py --dir generated_output/chai-wala
    python scripts/sandbox_test.py --broken
"""

import argparse
import asyncio
import sys
from pathlib import Path

from worker.tasks.sandbox import BROKEN_FIXTURE, sandbox_test

OUTPUT_DIR = Path(__file__).parent.parent / "generated_output"


def _print_report(report: dict) -> None:
    status = "PASSED" if report["passed"] else "FAILED"
    print(f"\n{status}\n")
    for check in report["checks"]:
        mark = "OK" if check["passed"] else "FAIL"
        print(f"  [{mark}] {check['name']}: {check['detail']}")
    if not report["checks"]:
        print("  (no checks ran)")


async def run(files: dict[str, str]) -> None:
    report = await sandbox_test(files)
    _print_report(report)
    if not report["passed"]:
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the sandbox smoke-test harness standalone.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dir", help="Path to a generated_output/<name> folder from Part 2")
    group.add_argument(
        "--broken", action="store_true", help="Run against the hardcoded broken fixture instead"
    )
    args = parser.parse_args()

    if args.broken:
        asyncio.run(run(BROKEN_FIXTURE))
        return

    site_dir = Path(args.dir)
    index_path = site_dir / "index.html"
    css_path = site_dir / "style.css"
    if not index_path.exists() or not css_path.exists():
        print(f"Expected index.html and style.css in {site_dir}", file=sys.stderr)
        sys.exit(1)

    files = {
        "index.html": index_path.read_text(encoding="utf-8"),
        "style.css": css_path.read_text(encoding="utf-8"),
    }
    asyncio.run(run(files))


if __name__ == "__main__":
    main()
