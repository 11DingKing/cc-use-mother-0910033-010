"""启动主诊医师授权排班 HTTP 服务。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scheduling.api import serve


if __name__ == "__main__":
    serve()
