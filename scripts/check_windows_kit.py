#!/usr/bin/env python3
"""校验 Windows 离线套件里「构建 Python / 自带安装器 / wheel」三者版本一致。

为什么需要这个检查：这个坑踩过两次 —— CI 的 `PYTHON_VERSION`、
`-PythonInstaller`、`build_offline_bundle.py --python` 三处只要有一处不一致，
套件拿到内网就是 `not a supported wheel on this platform`，
而且本地（Linux）开发机根本发现不了。所以在 CI 出包后立刻校验。

用法::

    python scripts/check_windows_kit.py <zip 路径> <期望的 Python 版本，如 3.13>

检查项：
1. 包里有且只有一个 ``python-<x.y.z>-amd64.exe``，其 ``x.y`` 与期望一致
2. ``wheels/`` 里带平台标签的 wheel，其 ``cpXY`` 与期望一致
3. ``MANIFEST.txt`` 里「目标 Python」与期望一致
"""

from __future__ import annotations

import os
import re
import sys
import zipfile
from typing import List

WHEEL_PY_TAG = re.compile(r"-(cp\d+)(?:-cp\d+)?-")   # 如 duckdb-1.5.5-cp313-cp313-win_amd64
INSTALLER = re.compile(r"python-(\d+\.\d+\.\d+)-amd64\.exe$")


def check(zip_path: str, expected: str) -> List[str]:
    """返回问题列表；空列表表示通过。"""
    want = "cp" + expected.replace(".", "")
    problems: List[str] = []

    with zipfile.ZipFile(zip_path) as zf:
        # Windows 打的 zip 用反斜杠做分隔符，统一成正斜杠再判断
        names = [n.replace("\\", "/") for n in zf.namelist()]

        installers = [n for n in names
                      if INSTALLER.search(n) and not n.startswith("source/")]
        if not installers:
            problems.append("包里没有 python-<版本>-amd64.exe（离线套件应当自带安装器）")
        for n in installers:
            ver = INSTALLER.search(n).group(1)
            if ver.rsplit(".", 1)[0] != expected:
                problems.append(
                    f"自带安装器是 {ver}，但期望 {expected}："
                    "目标机装出来的解释器与包里的 wheel 不匹配"
                )

        wheels = [n for n in names if n.startswith("wheels/") and n.endswith(".whl")]
        if not wheels:
            problems.append("wheels/ 是空的")
        tags = set()
        for n in wheels:
            m = WHEEL_PY_TAG.search(os.path.basename(n))
            if m:                      # 纯 Python 的 py3-none-any 没有 cp 标签
                tags.add(m.group(1))
        for tag in sorted(tags - {want}):
            samples = [os.path.basename(n) for n in wheels if f"-{tag}-" in os.path.basename(n)][:2]
            problems.append(
                f"wheel 的 Python 标签是 {tag}，但期望 {want}：{', '.join(samples)}"
            )

        manifest = [n for n in names if n.endswith("MANIFEST.txt") and "/" not in n]
        if not manifest:
            problems.append("缺少 MANIFEST.txt")
        else:
            text = zf.read(manifest[0]).decode("utf-8")
            m = re.search(r"目标 Python\s*:\s*(.+)", text)
            if not m:
                problems.append("MANIFEST.txt 里没有「目标 Python」一行")
            elif expected not in m.group(1):
                problems.append(
                    f"MANIFEST 里写的目标是 {m.group(1).strip()}，与期望 {expected} 不一致"
                )

    return problems


def main(argv: List[str]) -> int:
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[-6].strip(), file=sys.stderr)
        print("用法：python scripts/check_windows_kit.py <zip> <3.13>", file=sys.stderr)
        return 2
    zip_path, expected = argv[1], argv[2]
    if not os.path.exists(zip_path):
        print(f"找不到文件：{zip_path}", file=sys.stderr)
        return 2

    problems = check(zip_path, expected)
    target = f"Python {expected}"
    if problems:
        print(f"✗ {os.path.basename(zip_path)} 与 {target} 不匹配：", file=sys.stderr)
        for p in problems:
            print(f"    - {p}", file=sys.stderr)
        print(
            "\n检查这三处是否一致：CI 的 PYTHON_VERSION、-PythonInstaller 的版本、"
            "build_windows_release.ps1 传给 build_offline_bundle.py 的 --python。",
            file=sys.stderr,
        )
        return 1
    print(f"✓ {os.path.basename(zip_path)}：安装器 / wheel / MANIFEST 都是 {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
