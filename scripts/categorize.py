#!/usr/bin/env python3
"""Ask Claude to sort your date-organized notes into categories you provide.

Notes stay where they are (organized by date); this creates non-destructive
category index notes in the vault under "Transcript Insights/Categories/" and
populates the dashboard's category views.

Usage:
    python scripts/categorize.py Fundraising Hiring Product
    python scripts/categorize.py "Fundraising, Hiring, Product, Personal"
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from transcript_analyzer.pipeline.organize import categorize, reset_categories  # noqa: E402


def _parse_categories(argv: list[str]) -> list[str]:
    joined = " ".join(argv)
    if "," in joined:
        return [c.strip() for c in joined.split(",") if c.strip()]
    return [a.strip() for a in argv if a.strip()]


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] in ("--reset", "-r"):
        reset_categories()
        return 0
    cats = _parse_categories(argv)
    if not cats:
        print(__doc__)
        print("Provide categories, or --reset to clear all categories.")
        return 2
    print(f"[categorize] categories: {cats}")
    categorize(categories=cats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
