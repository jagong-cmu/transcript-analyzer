#!/usr/bin/env python3
"""Entry point for launchd / manual runs. Adds src/ to sys.path then runs sync."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from transcript_analyzer.sync import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
