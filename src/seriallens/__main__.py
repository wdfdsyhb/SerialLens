"""`python -m seriallens` 入口，等价于 seriallens 控制台脚本."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
