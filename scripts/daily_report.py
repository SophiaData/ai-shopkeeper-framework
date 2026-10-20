#!/usr/bin/env python3
"""本地日报的命令行入口。逻辑在 shopkeeper.workflow.daily_metrics。"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from shopkeeper.workflow.daily_metrics import main

if __name__ == "__main__":
    raise SystemExit(main())
