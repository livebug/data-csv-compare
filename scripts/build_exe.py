#!/usr/bin/env python3
"""打包成免安装的单文件可执行程序（目标机器可以完全没有 Python）。

**必须在目标操作系统上执行**——PyInstaller 不支持交叉编译：
Linux 上只能出 Linux 可执行文件，Windows 上只能出 .exe。

用法::

    pip install pyinstaller
    python scripts/build_exe.py                 # 当前平台
    python scripts/build_exe.py --onedir        # 目录模式（启动更快、更稳）

产物在 ``dist/``：
* ``--onefile``：``dist/datacompare``（Linux）/ ``dist/datacompare.exe``（Windows）
* ``--onedir`` ：``dist/datacompare/`` 整个目录一起拷走

注意
----
* duckdb 带 C 扩展，用 ``--onedir`` 更保险；``--onefile`` 每次启动要解压到临时目录，
  首次启动会慢 1-3 秒。
* DuckDB 的 ``excel`` / ``json`` 扩展**不会**被打进包里（它们是运行时下载的）。
  离线环境请用 ``--with-extensions`` 参数把扩展一并放到 exe 旁边的
  ``duckdb_extensions/`` 目录下，详见 docs/DEPLOY.md。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def main() -> int:
    ap = argparse.ArgumentParser(description="打包 datacompare 为单文件可执行程序")
    ap.add_argument("--onedir", action="store_true",
                    help="目录模式（更稳、启动更快），默认 onefile")
    ap.add_argument("--name", default="datacompare", help="可执行文件名")
    ap.add_argument("--clean", action="store_true", help="先清掉 build/dist")
    ap.add_argument("--with-extensions", action="store_true",
                    help="把 DuckDB 扩展也拷到产物旁边")
    args = ap.parse_args()

    from importlib.util import find_spec

    if find_spec("PyInstaller") is None:
        print("需要先安装 PyInstaller：pip install pyinstaller", file=sys.stderr)
        return 1

    if args.clean:
        for d in ("build", "dist"):
            shutil.rmtree(os.path.join(ROOT, d), ignore_errors=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--name", args.name,
        "--paths", os.path.join(ROOT, "src"),
        "--collect-all", "duckdb",          # duckdb 有二进制共享库，必须整包收集
        "--collect-all", "openpyxl",
        "--hidden-import", "yaml",
        "--console",
        # 用专门的入口脚本：直接把包内 __main__.py 交给 PyInstaller 会因为
        # 相对导入而报 "attempted relative import with no known parent package"
        os.path.join(HERE, "entrypoint.py"),
    ]
    cmd.append("--onedir" if args.onedir else "--onefile")

    print("$ " + " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=ROOT)

    dist = os.path.join(ROOT, "dist")
    # onedir 模式下产物是 dist/<name>/<name>[.exe]
    exe_dir = os.path.join(dist, args.name) if args.onedir else dist
    exe = os.path.join(exe_dir, args.name + (".exe" if os.name == "nt" else ""))

    if args.with_extensions:
        _copy_extensions(exe_dir)

    print(f"\n完成：{exe}")
    if args.onedir:
        print(f"      整个目录 {exe_dir} 一起拷到目标机器即可")
    print("\n自检：")
    subprocess.run([exe, "--version"], check=False)
    return 0


def _copy_extensions(base: str) -> None:
    """把本机已下载的 DuckDB 扩展拷到 exe 旁边，供离线使用。"""
    import glob

    ext_root = os.path.join(os.path.expanduser("~"), ".duckdb", "extensions")
    found = glob.glob(os.path.join(ext_root, "**", "*.duckdb_extension"), recursive=True)
    if not found:
        print("提示：本机 ~/.duckdb/extensions 下没有扩展文件，先用 "
              "scripts/build_offline_bundle.py --with-extensions 下载")
        return
    for src in found:
        rel = os.path.relpath(src, ext_root)
        dest = os.path.join(base, "duckdb_extensions", rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(src, dest)
        print(f"  扩展 → duckdb_extensions/{rel}")


if __name__ == "__main__":
    sys.exit(main())
