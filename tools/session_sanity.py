#!/usr/bin/env python3
"""Standalone wrapper for `python3 tools/session_sanity.py SESSION.jsonl`."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aetherward.session_sanity import main

if __name__ == '__main__':
    raise SystemExit(main())
