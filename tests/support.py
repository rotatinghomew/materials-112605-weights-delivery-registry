"""共享的交换资料装载与断言辅助。"""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from exchange import load  # noqa: E402

DATA = ROOT / "fixtures" / "exchange.json"

EARLY = "2027-01-18T10:00:00+08:00"       # 撤销之前
BEFORE_RL1 = "2027-01-18T10:59:00+08:00"
AFTER_RL1 = "2027-01-18T11:01:00+08:00"
AFTER_S4 = "2027-01-18T11:50:00+08:00"


def snapshot():
    return load(DATA)
