"""pytest 根 conftest：确保测试 import 的是本仓库 src（而非历史 editable 安装指向的旧副本）."""

import sys
from pathlib import Path

_SRC = Path(__file__).parent / "src"
_repo = str(Path(__file__).parent)
for p in (str(_SRC), _repo):
    if p not in sys.path:
        sys.path.insert(0, p)
