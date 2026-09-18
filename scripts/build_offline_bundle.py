#!/usr/bin/env python3
"""在**联网机器**上制作离线交付包，整包拷进内网即可用。

默认**连源码一起带**（内网可能要二次开发），产出的目录结构::

    offline-bundle/
    ├── README.txt
    ├── MANIFEST.txt              # 版本 / 生成时间 / 目标环境 / 文件清单
    ├── install_offline.sh        # Linux：装上就能跑（不改代码）
    ├── install_offline.ps1       # Windows：同上
    ├── install_dev_offline.sh    # Linux：二次开发（可编辑安装 + 跑测试）
    ├── install_dev_offline.ps1   # Windows：同上
    ├── wheels/                   # 运行 + 构建 + 测试依赖（多平台混放，pip 自选）
    ├── source/                   # 完整源码树（src/ tests/ scripts/ docs/ ...）
    └── duckdb_extensions/        # 可选：DuckDB 扩展（excel / json 等）

用法::

    # 内网要二次开发：wheel + 源码一起带（默认行为）
    python scripts/build_offline_bundle.py --out dist/offline \\
        --python 3.12 --platforms linux-x64,win-x64 --with-extensions --zip

    # 只给内网跑、不改代码：不带源码也不带开发依赖，包更小
    python scripts/build_offline_bundle.py --out dist/offline --no-source --no-dev

    # 想把 git 历史也带走（内网继续提交 / 查历史）
    python scripts/build_offline_bundle.py --out dist/offline --with-git

    # 同时准备 Windows 和 Linux
    python scripts/build_offline_bundle.py --out dist/offline \
        --python 3.12,3.13 --platforms win_amd64,manylinux_2_28_x86_64
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from typing import List, Optional, Tuple

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

#: 拷贝源码时**不**带走的目录名（全在 .gitignore 里，都是可再生成的产物）
SOURCE_SKIP_DIRS = {
    ".venv", "venv", "env", ".env", ".venv-build",
    "build", "dist", "release", "offline-bundle",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    ".idea", ".vscode", "node_modules", "compare_out", "htmlcov",
}

#: 拷贝源码时**不**带走的文件/目录通配
SOURCE_SKIP_GLOBS = (
    "*.egg-info", "*.py[cod]", "*.spec", "*.duckdb", "*.duckdb.wal", ".DS_Store",
)

#: 离线包里开发/测试工具链的**固定版本**（不是读 requirements-dev.txt）。
#:
#: 为什么钉死：
#:   1. ``pip download --python-version`` / ``--platform`` 只影响「选哪个 wheel」，
#:      **不按目标解释器评估环境标记** → pytest 的 `colorama; sys_platform=="win32"`
#:      这类依赖会被漏掉，Windows 内网就装不上；
#:   2. 同一个包在包里留多个版本时，内网 pip 要 backtracking，实测会直接报
#:      `Package 'setuptools' requires a different Python` 而失败。
#: 所以每个包只留一个版本，且都满足 requirements-dev.txt 里的松散约束。
DEV_TOOLCHAIN = [
    "pip==25.0.1",
    "setuptools==75.3.4",
    "wheel==0.45.1",
    "pytest==8.3.5",
    "pluggy==1.5.0",
    "iniconfig==2.1.0",
    "packaging==26.2",
    "colorama==0.4.6",            # Windows 上 pytest 要（sys_platform == "win32"）
    # 下面三个是 3.9/3.10 才用得上的 marker 依赖，留着以防 --python 填了老版本
    "exceptiongroup==1.3.1",      # python_version < "3.11"
    "tomli==2.4.1",               # python_version < "3.11"
    "typing-extensions==4.12.2",  # exceptiongroup 在 python_version < "3.13" 时要
]


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


def download_wheels(out_dir: str, step: str,
                    python_versions: List[str], platforms: List[str],
                    req_rel: str = "",
                    packages: Optional[List[str]] = None,
                    loop_platforms: bool = True) -> List[Tuple[str, Optional[str]]]:
    """按「Python 版本（× 平台）」逐个下载，返回失败的组合列表。

    注意：不能把多个 ``--platform`` 塞进同一次 ``pip download``——
    pip 对每个包只会挑一个匹配的 wheel，结果就是只拿到第一个平台。
    必须一个平台一次调用，wheel 累积到同一个目录（同名会覆盖）。

    ``loop_platforms=False`` 用于纯 Python 的依赖（pytest/setuptools/wheel/pip）：
    它们都是 ``py3-none-any``，跟平台无关，只按 Python 版本下一遍就够。
    此时应传 ``packages`` 而不是 ``req_rel``，见 :func:`read_dev_packages`。

    **有些组合为空是正常的**：老 glibc 上只剩老包带 `manylinux2014` 标签，
    新 glibc 标签下可能根本没有该包（例如 Python 3.9 在 `manylinux_2_28_x86_64`
    下已无 duckdb wheel）。这种情况只记警告并跳过（老 glibc 的 wheel 在新机器上
    照样能装），真正的错误由 :func:`check_version_coverage` 兜底。
    """
    wheels_dir = os.path.join(out_dir, "wheels")
    os.makedirs(wheels_dir, exist_ok=True)

    if packages:
        # 显式包名优先：requirements-dev.txt 里含 `-r requirements.txt`，
        # 交给 pip 会按构建机平台把运行依赖再解析一遍（多带用不上的 wheel），
        # 而且 marker 依赖（colorama/exceptiongroup/tomli）需要显式补进来。
        spec = list(packages)
        label = req_rel or " ".join(spec)
    elif req_rel:
        req = os.path.join(ROOT, req_rel)
        if not os.path.exists(req):
            raise SystemExit(f"找不到依赖清单：{req}")
        spec = ["-r", req]
        label = req_rel
    else:
        spec = []
        label = ""
    if not spec:
        raise SystemExit("没有要下载的依赖（req_rel 与 packages 都为空）")

    targets = [(pyver, plat)
               for pyver in python_versions
               for plat in (platforms if loop_platforms else [None])]
    failed: List[Tuple[str, Optional[str]]] = []
    for pyver, platform in targets:
        cmd = [
            sys.executable, "-m", "pip", "download",
            *spec,
            "--only-binary=:all:",
            "--python-version", pyver,
            "--implementation", "cp",
            "--dest", wheels_dir,
        ]
        if platform:
            cmd += ["--platform", platform]
        suffix = f"Python {pyver}" + (f" / {platform}" if platform else "")
        print(f"{step} 下载 {label}（{suffix}）")
        try:
            run(cmd)
        except subprocess.CalledProcessError as exc:
            failed.append((pyver, platform))
            print(f"      警告：pip download 失败（退出码 {exc.returncode}），"
                  "跳过这个组合；上面 pip 的输出里会写是哪个包没有匹配的 wheel")
            print("            常见原因：该 Python 版本在那个平台标签下已经没有 wheel，"
                  "例如 3.9+manylinux_2_28（duckdb 从 1.5 起不再发 cp39）")
    return failed


def check_version_coverage(failed: List[Tuple[str, Optional[str]]],
                           python_versions: List[str],
                           platforms: List[str]) -> None:
    """允许个别组合为空，但**不接受某个 Python 版本整个下不到东西**。

    组合为空是正常的（见 :func:`download_wheels` 的说明）；一个 Python 版本在
    **所有**目标平台下都空，基本是版本号写错或依赖真不支持，必须拦住。
    """
    if not failed:
        return
    print("\n以下组合没有可用 wheel，已跳过：")
    for pyver, platform in failed:
        print(f"  - Python {pyver}" + (f" / {platform}" if platform else ""))

    dead = [p for p in python_versions
            if all((p, pl) in failed for pl in platforms)]
    if dead:
        raise SystemExit(
            f"错误：Python {', '.join(dead)} 在所有目标平台下都没下到 wheel。\n"
            "  检查一下 --python / --platforms（不存在的版本号、写错的平台标签、"
            "或架构对不上都会这样）。"
        )


def build_project_wheel(out_dir: str, step: str) -> None:
    """把本项目自己打成 wheel，一起放进离线包。"""
    wheels_dir = os.path.join(out_dir, "wheels")
    os.makedirs(wheels_dir, exist_ok=True)
    print(f"{step} 打包本项目 wheel")
    run([
        sys.executable, "-m", "pip", "wheel",
        "--no-deps", "--wheel-dir", wheels_dir, ROOT,
    ])


def read_project_version() -> str:
    """版本号在 ``src/datacompare/__init__.py`` 与 ``pyproject.toml`` 两处，优先前者。"""
    init = os.path.join(ROOT, "src", "datacompare", "__init__.py")
    try:
        with open(init, encoding="utf-8") as fh:
            m = re.search(r"""^__version__\s*=\s*["']([^"']+)""", fh.read(), re.M)
        if m:
            return m.group(1)
    except OSError:
        pass
    try:
        with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as fh:
            m = re.search(r'''(?m)^\s*version\s*=\s*"([^"]+)"''', fh.read())
        if m:
            return m.group(1)
    except OSError:
        pass
    return "0.0.0"


def git_revision() -> str:
    """尽力取当前提交号；取不到就返回 unknown（不联网，纯本地 git）。"""
    try:
        rev = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT,
            capture_output=True, text=True, check=True,
        ).stdout
    except Exception:
        return "unknown"
    return f"{rev}{'（含未提交改动）' if status.strip() else ''}"


def copy_source(out_dir: str, step: str, with_git: bool = False) -> str:
    """把源码树原样拷进 ``source/``。

    **不是** ``git archive``：内网二次开发要的就是「你手上这份」，
    包括还没提交的改动。所以按忽略清单直接拷目录，
    ``.venv`` / ``dist`` / ``__pycache__`` / ``*.egg-info`` 这些产物不带。
    """
    dest = os.path.join(out_dir, "source")
    if os.path.exists(dest):
        shutil.rmtree(dest)
    print(f"{step} 拷贝源码到 source/（{'含 .git 历史' if with_git else '不含 .git'}）")

    out_abs = os.path.abspath(out_dir)

    def ignore(dirpath: str, names: List[str]) -> List[str]:
        skip: List[str] = []
        for name in names:
            path = os.path.join(dirpath, name)
            abs_path = os.path.abspath(path)
            # 输出目录本身（比如 --out 就设在仓库里）绝不能递归拷进去
            if abs_path == out_abs or abs_path.startswith(out_abs + os.sep):
                skip.append(name)
                continue
            if os.path.isdir(path):
                if name in SOURCE_SKIP_DIRS:
                    skip.append(name)
                    continue
                if name == ".git" and not with_git:
                    skip.append(name)
                    continue
            if any(fnmatch.fnmatch(name, pat) for pat in SOURCE_SKIP_GLOBS):
                skip.append(name)
        return skip

    shutil.copytree(ROOT, dest, ignore=ignore, symlinks=True)

    files = sum(len(fs) for _, _, fs in os.walk(dest))
    size = sum(os.path.getsize(os.path.join(dp, f))
               for dp, _, fs in os.walk(dest) for f in fs)
    print(f"      source/ 共 {files} 个文件、{size / 1024 / 1024:.1f} MB")
    return dest


def fetch_extensions(out_dir: str, step: str) -> None:
    """下载 DuckDB 扩展文件，离线机器可直接 LOAD。

    产出目录**镜像** DuckDB 的 extension_directory 结构::

        duckdb_extensions/<duckdb版本>/<平台>/xxx.duckdb_extension

    这样离线机器只要把 ``duckdb_extensions/*`` 拷进 ``~/.duckdb/extensions/``
    就能直接用，不需要任何网络。
    """
    print(f"{step} 下载 DuckDB 扩展")
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


def write_readme(out_dir: str, python_versions: List[str], platforms: List[str],
                 version: str, with_source: bool, with_dev: bool,
                 with_git: bool) -> None:
    if with_source:
        source_section = (
            "\n源码目录\n"
            "--------\n"
            "source/                     本项目完整源码（改这里 → 立即生效）\n"
            "    这批源码来自哪个提交、有没有未提交改动，见 MANIFEST.txt。"
        )
    else:
        source_section = (
            "\n本包**未**包含源码（生成时用了 --no-source）。\n"
            "内网若要二次开发，请重新生成并去掉 --no-source。"
        )

    if with_source and with_dev:
        dev_section = """
Linux 二次开发（可编辑安装 + 跑测试）
------------------------------------
    bash install_dev_offline.sh
    cd source
    ../venv-dev/bin/python -m pytest tests -q      # 应全部通过
    ../venv-dev/bin/datacompare compare -b 旧.csv -a 新.csv -k 单据号 -o out

Windows 二次开发
----------------
    powershell -ExecutionPolicy Bypass -File install_dev_offline.ps1
    cd source
    ..\\venv-dev\\Scripts\\python.exe -m pytest tests -q

改完代码不用重装——可编辑安装已经把 venv 指向 source/ 了。
要重新打成 wheel 带走（离线也能打，构建依赖已在 venv 里）：
    python -m pip wheel --no-deps --no-build-isolation --no-index -w dist .
"""
    else:
        dev_section = ""

    # 「目录结构」按实际带了什么来列，不带的东西不写上去
    layout = [
        "wheels/                     依赖 wheel  + 本项目自身的 wheel",
        "duckdb_extensions/          DuckDB 扩展（可选，按平台/版本分目录）",
        "install_offline.sh          Linux：装上就能跑（不可改代码）",
        "install_offline.ps1         Windows：同上",
    ]
    if with_source and with_dev:
        layout += [
            "install_dev_offline.sh      Linux：二次开发环境（可编辑安装 + 跑测试）",
            "install_dev_offline.ps1     Windows：同上",
        ]
    if with_source:
        layout.append("source/                     本项目完整源码（改这里 → 立即生效）")
    layout.append("MANIFEST.txt                版本 / 生成时间 / 文件清单")
    layout_text = "\n".join(layout)

    if with_source:
        intro = "本包在联网机器上生成，包含安装所需的**全部 wheel**与**完整源码**，"
    else:
        intro = "本包在联网机器上生成，包含安装所需的**全部 wheel**，"

    text = f"""datacompare 离线交付包（v{version}）
==============================

{intro}
可在完全离线的内网使用。

目标环境
--------
Python: {', '.join(python_versions)}
平台  : {', '.join(platforms)}

目录结构
--------
{layout_text}
{source_section}

Linux 安装（只部署）
-------------------
    tar -xzf offline-bundle.tar.gz
    cd offline-bundle
    bash install_offline.sh
    ./venv/bin/datacompare --version

Windows 安装（只部署）
----------------------
    Expand-Archive offline-bundle.zip -DestinationPath .
    cd offline-bundle
    powershell -ExecutionPolicy Bypass -File install_offline.ps1
    .\\venv\\Scripts\\datacompare.exe --version
{dev_section}
注意事项
--------
1. 目标机器需要已安装对应版本的 Python（脚本会检查）。
   如果目标机器完全没有 Python，请改用 PyInstaller 方案，
   见 docs/DEPLOY.md「方案 B」——注意 PyInstaller **不能交叉编译**。
2. wheel 与「Python 版本 + 操作系统 + CPU 架构」严格绑定，
   拿错版本会报 "not a supported wheel on this platform"。
   本包覆盖：Python {', '.join(python_versions)} / {', '.join(platforms)}。
3. 离线装包时用 `--no-index --find-links wheels`，安装脚本里已经写好了；
   千万不要让 pip 去联网（内网也连不上），也不要漏掉 --no-index。
4. DuckDB 扩展（可选）：把 duckdb_extensions/ 下的内容拷到目标机器的
   ~/.duckdb/extensions/ 即可。本工具**不依赖**扩展也能完整跑通。
"""
    if with_source:
        text += (
            "5. 源码拷贝的是「生成那一刻的工作区」，**含未提交的改动**；\n"
            "   需要 git 历史就在生成时加 --with-git。\n"
        )
    with open(os.path.join(out_dir, "README.txt"), "w", encoding="utf-8") as fh:
        fh.write(text)


def write_manifest(out_dir: str, version: str, python_versions: List[str],
                   platforms: List[str], revision: str, with_source: bool,
                   with_dev: bool, with_git: bool, extensions: bool,
                   skipped: Optional[List[Tuple[str, Optional[str]]]] = None) -> None:
    """写一份交付清单，方便内网那边核对「拿到的东西到底是哪一版」。"""
    wheels = []
    wheels_dir = os.path.join(out_dir, "wheels")
    if os.path.isdir(wheels_dir):
        for name in sorted(os.listdir(wheels_dir)):
            path = os.path.join(wheels_dir, name)
            if os.path.isfile(path):
                wheels.append(f"    {name}  ({os.path.getsize(path) / 1024:.0f} KB)")

    files = []
    for dp, dirnames, filenames in os.walk(out_dir):
        dirnames[:] = [d for d in dirnames if d not in ("wheels", "source")]
        for fn in filenames:
            rel = os.path.relpath(os.path.join(dp, fn), out_dir)
            files.append(f"    {rel}")

    lines = [
        "datacompare 离线交付包 清单",
        "=" * 30,
        f"项目版本    : {version}",
        f"源码提交    : {revision}",
        f"生成时间    : {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %z')}",
        f"生成机器    : {sys.platform} / Python {sys.version.split()[0]}",
        "",
        f"目标 Python : {', '.join(python_versions)}",
        f"目标平台    : {', '.join(platforms)}",
        "",
        f"含源码      : {'是' + ('（含 .git 历史）' if with_git else '') if with_source else '否'}",
        f"含开发依赖  : {'是（setuptools/wheel/pip/pytest）' if with_dev else '否'}",
        f"含 DuckDB 扩展: {'是' if extensions else '否'}",
        "",
    ]
    if skipped:
        lines.append("以下「Python × 平台」组合没有可用 wheel（正常现象，不是出错）：")
        for pyver, platform in skipped:
            lines.append(f"    Python {pyver}" + (f" / {platform}" if platform else ""))
        lines.append("    （例如某些老 Python 在新 glibc 标签下已无 duckdb wheel；"
                     "安装时 pip 会自己挑能用的那个）")
        lines.append("")
    lines += [
        f"wheel 清单（{len(wheels)} 个）",
        "-" * 30,
    ]
    lines += wheels or ["    （无）"]
    lines += ["", "包内其它文件", "-" * 30]
    lines += files or ["    （无）"]
    lines += [
        "",
        "校验（可选）：",
        "    sha256sum $(find wheels -name '*.whl' | sort) > wheels.sha256",
        "",
    ]
    with open(os.path.join(out_dir, "MANIFEST.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def main() -> int:
    ap = argparse.ArgumentParser(description="制作 datacompare 离线交付包（内网部署 + 二次开发）")
    ap.add_argument("--out", default="dist/offline-bundle", help="输出目录")
    ap.add_argument("--python", default="3.13",
                    help="目标 Python 版本，逗号分隔，如 3.12,3.13（默认 3.13＝当前开发环境）")
    ap.add_argument("--platforms", default="linux-x64,win-x64",
                    help="目标平台：win-x64 / win-arm64 / linux-x64 / linux-arm64，"
                         "也可直接写 pip 平台标签（逗号分隔）")
    # 不用 argparse.BooleanOptionalAction（3.9+），两个开关更直白
    ap.add_argument("--source", dest="source", action="store_true", default=True,
                    help="带上完整源码（默认带）")
    ap.add_argument("--no-source", dest="source", action="store_false",
                    help="不带源码，只带 wheel")
    ap.add_argument("--dev", dest="dev", action="store_true", default=True,
                    help="带上构建/测试依赖 setuptools/wheel/pip/pytest（默认带）")
    ap.add_argument("--no-dev", dest="dev", action="store_false",
                    help="不带构建/测试依赖")
    ap.add_argument("--with-git", action="store_true",
                    help="源码里连 .git 一起带（保留提交历史，包会大一些）")
    ap.add_argument("--with-extensions", action="store_true",
                    help="同时下载 DuckDB 扩展（excel/json）")
    ap.add_argument("--skip-project", action="store_true", help="不打包本项目自身")
    ap.add_argument("--zip", action="store_true", help="打包完成后压成 tar.gz / zip")
    ap.add_argument("--print-version", action="store_true",
                    help="只打印项目版本号后退出（CI 取版本号用）")
    args = ap.parse_args()

    if args.print_version:
        print(read_project_version())
        return 0

    # 重定向/管道下 Python 会缓冲 stdout，而 pip 子进程是直接写 fd 的，
    # 两边顺序会错位（日志看起来像跑反了）。行缓冲一下就正常了。
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, OSError):
        pass

    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    pyvers = [v.strip() for v in args.python.split(",") if v.strip()]
    platforms = expand_platforms(args.platforms)
    if not platforms:
        raise SystemExit("没有解析出任何目标平台")

    with_dev = args.dev
    total_steps = 3 + (1 if with_dev else 0) + (1 if args.source else 0) + \
        (1 if args.with_extensions else 0)
    step_no = 0

    def next_step(label: str) -> str:
        nonlocal step_no
        step_no += 1
        return f"[{step_no}/{total_steps}] {label}"

    version = read_project_version()
    revision = git_revision()
    print(f"datacompare {version}（源码 {revision}）→ {out_dir}\n")

    os.chdir(ROOT)
    failed = download_wheels(out_dir, next_step("下载运行依赖 wheel"),
                             pyvers, platforms, req_rel="requirements.txt")
    if with_dev:
        # 钉死版本 + 按平台各下一遍：pytest/setuptools 是 py3-none-any 无所谓，
        # 但 tomli 2.4 起带 cpXXX 原生 wheel，只下构建机平台会让 Windows 缺 wheel。
        failed += download_wheels(out_dir, next_step("下载开发/构建依赖 wheel"),
                                  pyvers, platforms, packages=DEV_TOOLCHAIN)
    elif args.dev:
        print("跳过开发/构建依赖（--no-source 时不需要）")
    check_version_coverage(failed, pyvers, platforms)
    if not args.skip_project:
        build_project_wheel(out_dir, next_step("打包本项目 wheel"))
    if args.source:
        copy_source(out_dir, next_step("拷贝源码"), with_git=args.with_git)
    if args.with_extensions:
        fetch_extensions(out_dir, next_step("下载 DuckDB 扩展"))
    else:
        print("跳过 DuckDB 扩展（加 --with-extensions 可下载）")

    scripts = ["install_offline.sh", "install_offline.ps1"]
    if args.source and with_dev:
        scripts += ["install_dev_offline.sh", "install_dev_offline.ps1"]
    for name in scripts:
        src = os.path.join(HERE, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(out_dir, name))
        else:
            print(f"警告：找不到脚本 {src}")

    write_readme(out_dir, pyvers, platforms, version, args.source, with_dev,
                 args.with_git)
    write_manifest(out_dir, version, pyvers, platforms, revision, args.source,
                   with_dev, args.with_git, args.with_extensions, failed)

    total = sum(
        os.path.getsize(os.path.join(dp, f))
        for dp, _, fs in os.walk(out_dir) for f in fs
    )
    print(f"\n完成：{out_dir}（{total / 1024 / 1024:.0f} MB）")
    if args.source:
        print("  内网二次开发：bash install_dev_offline.sh")
    print("  内网只部署  ：bash install_offline.sh")

    if args.zip:
        base = os.path.join(os.path.dirname(out_dir), os.path.basename(out_dir))
        shutil.make_archive(base, "gztar", root_dir=os.path.dirname(out_dir),
                            base_dir=os.path.basename(out_dir))
        print(f"已打包：{base}.tar.gz")
    return 0


if __name__ == "__main__":
    sys.exit(main())
