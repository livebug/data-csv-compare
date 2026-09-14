"""PyInstaller 的入口脚本。

不能直接把 ``src/datacompare/__main__.py`` 交给 PyInstaller——
那会被当成顶层脚本，里面的相对导入 ``from .cli import main`` 会失败。
这里用绝对导入做一个干净的入口。
"""

import sys

from datacompare.cli import main

if __name__ == "__main__":
    sys.exit(main())
