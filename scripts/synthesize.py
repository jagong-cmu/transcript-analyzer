#!/usr/bin/env python3
"""Run the synthesis engine on demand (the sync loop also runs it daily).

Usage:
    python scripts/synthesize.py                 # all steps, respects change detection
    python scripts/synthesize.py --only digest   # one step (repeatable)
    python scripts/synthesize.py --force         # regenerate dossiers/studies even if unchanged
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from transcript_analyzer.pipeline import synthesize  # noqa: E402
from transcript_analyzer.pipeline.llm import LLMError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Write digests, dossiers, and rollups into the vault.")
    parser.add_argument("--only", action="append", choices=synthesize.ALL_STEPS,
                        help="Run only this step (repeatable). Default: all.")
    parser.add_argument("--force", action="store_true",
                        help="Regenerate dossiers/studies even when their inputs are unchanged.")
    args = parser.parse_args()

    try:
        summary = synthesize.run(
            only=set(args.only) if args.only else None,
            force=args.force,
        )
    except LLMError as e:
        print(f"synthesize: {e}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
