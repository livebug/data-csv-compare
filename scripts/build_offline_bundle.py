#!/usr/bin/env python3
"""在**联网机器**上制作离线安装包。

产出的目录结构（整包拷进内网即可用）::

    offline-bundle/
    ├── README.txt
    ├── install_offline.sh        # Linux 安装
    ├── install_offline.ps1       # Windows 安装
    ├── wheels/                   # 所有 .whl（多平台混放，pip 会按当前解释器自动挑选）
    └── duckdb_extensions/        # 可选：DuckDB 扩展（excel / json 等）

用法::

    # 只给 Linux x86_64 + Python 3.12
    python scripts/build_offline_bundle.py --out dist/offline \\
        --python 3.12 --platforms manylinux_2_28_x86_64

    # 同时准备 Windows 和 Linux
    python scripts/build_offline_bundle.py --out dist/offline \\
        --python 3.12,3.13 --platforms win_amd64,manylinux_2_28_x86_64

    # 顺手把 DuckDB 扩展也下载下来（离线机器就能用 excel 导出等能力）
    python scripts/build_offline_bundle.py --out dist/offline --with-extensions
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from typing import List

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

#: 常用平台标签（pip 接受多个，取并集）
PLATFORM_PRESETS = {
    "win-x64": ["win_amd64"],
    "win-arm64": ["win_arm64"],
    "linux-x64": [
        "manylinux_2_28_x86_64",
        "manylinux_2_17_x86_64",
        "manylinux2014_x86_64",
    ],
    "linux-arm64": [
        "manylinux_2_28_aarch64",
        "manylinux_2_17_aarch64",
        "manylinux2014_aarch64",
    ],
}

DEFAULT_EXTENSIONS = ["excel", "json"]


def run(cmd: List[str], **kwargs) -> None:
    print("  $ " + " ".join(cmd))
    subprocess.run(cmd, check=True, **kwargs)


def expand_platforms(raw: str) -> List[str]:
    out: List[str] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if item in PLATFORM_PRESETS:
            out.extend(PLATFORM_PRESETS[item])
        else:
            out.append(item)
    # 去重且保序
    seen = set()
    return [p for p in out if not (p in seen or seen.add(p))]


def download_wheels(out_dir: str, python_versions: List[str], platforms: List[str]) -> None:
    """按「Python 版本 × 平台」逐个下载。

    注意：不能把多个 ``--platform`` 塞进同一次 ``pip download``——
    pip 对每个包只会挑一个匹配的 wheel，结果就是只拿到第一个平台。
    必须一个平台一次调用，wheel 累积到同一个目录（同名会覆盖）。
    """
    wheels_dir = os.path.join(out_dir, "wheels")
    os.makedirs(wheels_dir, exist_ok=True)
    req = os.path.join(ROOT, "requirements.txt")

    for pyver in python_versions:
        for platform in platforms:
            cmd = [
                sys.executable, "-m", "pip", "download",
                "-r", req,
                "--only-binary=:all:",
                "--python-version", pyver,
                "--implementation", "cp",
                "--platform", platform,
                "--dest", wheels_dir,
            ]
            print(f"[1/3] 下载依赖 wheel（Python {pyver} / {platform}）")
            try:
                run(cmd)
            except subprocess.CalledProcessError as exc:
                raise SystemExit(
                    f"下载失败（Python {pyver} / {platform}）。\n"
                    "  该组合可能没有对应 wheel，请去掉该组合。\n"
                    f"  原始错误：{exc}"
                ) from exc


def build_project_wheel(out_dir: str) -> None:
    """把本项目自己打成 wheel，一起放进离线包。"""
    wheels_dir = os.path.join(out_dir, "wheels")
    os.makedirs(wheels_dir, exist_ok=True)
    print("[2/3] 打包本项目 wheel")
    run([
        sys.executable, "-m", "pip", "wheel",
        "--no-deps", "--wheel-dir", wheels_dir, ROOT,
    ])


def fetch_extensions(out_dir: str) -> None:
    """下载 DuckDB 扩展文件，离线机器可直接 LOAD。

    产出目录**镜像** DuckDB 的 extension_directory 结构::

        duckdb_extensions/<duckdb版本>/<平台>/xxx.duckdb_extension

    这样离线机器只要把 ``duckdb_extensions/*`` 拷进 ``~/.duckdb/extensions/``
    就能直接用，不需要任何网络。
    """
    print("[3/3] 下载 DuckDB 扩展")
    try:
        import duckdb
    except ImportError:
        print("      跳过：当前环境没有 duckdb")
        return

    import glob

    try:
        con = duckdb.connect()
        ext_root = con.sql("SELECT current_setting('extension_directory')").fetchone()[0]
    except Exception as exc:
        print(f"      跳过：{exc}")
        return
    if not ext_root:
        ext_root = os.path.join(os.path.expanduser("~"), ".duckdb", "extensions")

    for name in DEFAULT_EXTENSIONS:
        try:
            con.execute(f"INSTALL {name}")
        except Exception as exc:
            print(f"      跳过 {name}：{exc}")
            continue
        found = sorted(
            glob.glob(os.path.join(ext_root, "**", f"{name}.duckdb_extension"),
                      recursive=True)
        )
        if not found:
            print(f"      跳过 {name}：在 {ext_root} 下找不到扩展文件")
            continue
        # 取版本号最高的那个（目录名形如 v1.5.5）
        src = max(found, key=_version_key)
        rel = os.path.relpath(src, ext_root)
        dest = os.path.join(out_dir, "duckdb_extensions", rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(src, dest)
        print(f"      {name} → duckdb_extensions/{rel}")


def _version_key(path: str) -> tuple:
    parts = []
    for chunk in os.path.normpath(path).split(os.sep):
        if chunk.startswith("v") and chunk[1:2].isdigit():
            try:
                parts.append(tuple(int(x) for x in chunk[1:].split(".")))
            except ValueError:
                parts.append((0,))
    return tuple(parts) or ((0,),)


def write_readme(out_dir: str, python_versions: List[str], platforms: List[str]) -> None:
    text = f"""datacompare 离线安装包
=====================

本包在联网机器上生成，包含安装所需的全部 wheel，可在完全离线的内网使用。

目标环境
--------
Python: {', '.join(python_versions)}
平台  : {', '.join(platforms)}

目录结构
--------
wheels/                     所有依赖与本项目 wheel
duckdb_extensions/          DuckDB 扩展（可选，按平台/版本分目录）
install_offline.sh          Linux 安装脚本
install_offline.ps1         Windows 安装脚本

Linux 安装
----------
    tar -xzf offline-bundle.tar.gz
    cd offline-bundle
    bash install_offline.sh
    ./venv/bin/datacompare --version

Windows 安装
------------
    Expand-Archive offline-bundle.zip -DestinationPath .
    cd offline-bundle
    powershell -ExecutionPolicy Bypass -File install_offline.ps1
    .\\venv\\Scripts\\datacompare.exe --version

注意事项
--------
1. 目标机器需要已安装对应版本的 Python（脚本会检查）。
   如果目标机器完全没有 Python，请改用 PyInstaller 单文件方案，
   见 docs/DEPLOY.md「方案 C」。
2. wheel 与「Python 版本 + 操作系统 + CPU 架构」严格绑定，
   拿错版本会报 "not a supported wheel on this platform"。
3. 若要用 DuckDB 的 Excel/JSON 扩展（离线），把
   duckdb_extensions/<platform>/<version>/ 下的文件拷贝到目标机器
   的 ~/.duckdb/extensions/<platform>/<version>/ 目录即可。
"""
    with open(os.path.join(out_dir, "README.txt"), "w", encoding="utf-8") as fh:
        fh.write(text)


def main() -> int:
    ap = argparse.ArgumentParser(description="制作 datacompare 离线安装包")
    ap.add_argument("--out", default="dist/offline-bundle", help="输出目录")
    ap.add_argument("--python", default="3.12",
                    help="目标 Python 版本，逗号分隔，如 3.11,3.12")
    ap.add_argument("--platforms", default="linux-x64,win-x64",
                    help="目标平台：win-x64 / win-arm64 / linux-x64 / linux-arm64，"
                         "也可直接写 pip 平台标签（逗号分隔）")
    ap.add_argument("--with-extensions", action="store_true",
                    help="同时下载 DuckDB 扩展（excel/json）")
    ap.add_argument("--skip-project", action="store_true", help="不打包本项目自身")
    ap.add_argument("--zip", action="store_true", help="打包完成后压成 tar.gz / zip")
    args = ap.parse_args()

    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    pyvers = [v.strip() for v in args.python.split(",") if v.strip()]
    platforms = expand_platforms(args.platforms)
    if not platforms:
        raise SystemExit("没有解析出任何目标平台")

    os.chdir(ROOT)
    download_wheels(out_dir, pyvers, platforms)
    if not args.skip_project:
        build_project_wheel(out_dir)
    if args.with_extensions:
        fetch_extensions(out_dir)
    else:
        print("[3/3] 跳过 DuckDB 扩展（加 --with-extensions 可下载）")

    for name in ("install_offline.sh", "install_offline.ps1"):
        src = os.path.join(HERE, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(out_dir, name))
    write_readme(out_dir, pyvers, platforms)

    total = sum(
        os.path.getsize(os.path.join(dp, f))
        for dp, _, fs in os.walk(out_dir) for f in fs
    )
    print(f"\n完成：{out_dir}（{total / 1024 / 1024:.0f} MB）")

    if args.zip:
        base = os.path.join(os.path.dirname(out_dir), os.path.basename(out_dir))
        shutil.make_archive(base, "gztar", root_dir=os.path.dirname(out_dir),
                            base_dir=os.path.basename(out_dir))
        print(f"已打包：{base}.tar.gz")
    return 0


if __name__ == "__main__":
    sys.exit(main())
