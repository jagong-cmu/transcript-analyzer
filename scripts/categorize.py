#!/usr/bin/env python3
"""Ask Claude to sort your date-organized notes into categories you provide.

Notes stay where they are (organized by date); this creates non-destructive
category notes in the vault under "Transcript Insights/Categories/" and
populates the dashboard's category views. Optional descriptions steer matching.

Usage:
    python scripts/categorize.py Fundraising Hiring Product
    python scripts/categorize.py "Fundraising, Hiring, Product"
    python scripts/categorize.py "Fundraising: LP updates and term sheets" \\
                                 "Hiring: interviews and offer loops"
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from transcript_analyzer.pipeline.organize import (  # noqa: E402
    categorize,
    normalize_categories,
    reset_categories,
)


def _parse_argv(argv: list[str]) -> list[str]:
    """Split on commas when present; otherwise keep each arg (may be Name: desc)."""
    joined = " ".join(argv)
    if "," in joined and not any(":" in a for a in argv):
        return [c.strip() for c in joined.split(",") if c.strip()]
    return [a.strip() for a in argv if a.strip()]


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] in ("--reset", "-r"):
        reset_categories()
        return 0
    raw = _parse_argv(argv)
    defs = normalize_categories(raw)
    if not defs:
        print(__doc__)
        print("Provide categories (optionally Name: description), or --reset.")
        return 2
    for d in defs:
        print(f"[categorize] {d.name}" + (f" — {d.description}" if d.description else ""))
    categorize(categories=defs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
