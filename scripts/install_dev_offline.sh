#!/usr/bin/env bash
# datacompare 内网二次开发环境安装（Linux / macOS）
#
# 与 install_offline.sh 的区别：
#   install_offline.sh      只把 wheel 装上，装完就能跑，改不了代码
#   install_dev_offline.sh  以**可编辑模式**装 source/，改代码立刻生效，还能跑测试
#
# 在**完全离线**的机器上执行：
#     bash install_dev_offline.sh
#
# 可选环境变量：
#     PYTHON=python3.12            指定解释器（默认自动挑 3.9+ 的 python3）
#     VENV_DIR=./venv-dev          虚拟环境目录
#     SOURCE_DIR=./source          源码目录（可换成你自己 checkout 的仓库路径）
#     WHEELS_DIR=./wheels          wheel 目录
#     EXT_DIR=~/.duckdb/extensions DuckDB 扩展安装位置
#     SKIP_TESTS=1                 跳过最后的 pytest 自检

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-$HERE/venv-dev}"
SOURCE="${SOURCE_DIR:-$HERE/source}"
WHEELS="${WHEELS_DIR:-$HERE/wheels}"

echo "==> datacompare 内网二次开发环境"
echo "    安装包：$HERE"
echo "    虚拟环境：$VENV_DIR"

# ---------- 1. 找一个可用的 Python ----------
PY="${PYTHON:-}"
if [[ -z "$PY" ]]; then
    for cand in python3.13 python3.12 python3.11 python3.10 python3.9 python3 python; do
        if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
    done
fi
if [[ -z "$PY" ]]; then
    echo "错误：找不到 Python 解释器，本方案要求目标机器已装 Python 3.9+。" >&2
    exit 1
fi
PYVER="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo "==> 使用解释器：$PY (Python $PYVER)"

"$PY" - <<'EOF' || { echo "错误：需要 Python 3.9 或更高版本" >&2; exit 1; }
import sys
raise SystemExit(0 if sys.version_info >= (3, 9) else 1)
EOF

# ---------- 2. 目录检查 ----------
[[ -d "$WHEELS" ]] || { echo "错误：找不到 wheels 目录（$WHEELS）" >&2; exit 1; }
[[ -d "$SOURCE" ]] || {
    echo "错误：找不到源码目录（$SOURCE）。" >&2
    echo "      用 --no-source 生成的包不含源码，无法二次开发；请重新制作。" >&2
    exit 1
}
[[ -f "$SOURCE/pyproject.toml" ]] || {
    echo "错误：$SOURCE 看起来不是本项目源码（缺 pyproject.toml）。" >&2
    exit 1
}

# 开发依赖（setuptools/wheel/pip/pytest）少了就没法离线做可编辑安装
[[ -f "$SOURCE/requirements-dev.txt" ]] || {
    echo "警告：源码里没有 requirements-dev.txt，将只装运行依赖。" >&2
}

# ---------- 3. 建虚拟环境 ----------
echo "==> 创建虚拟环境：$VENV_DIR"
"$PY" -m venv "$VENV_DIR"
PIP="$VENV_DIR/bin/pip"
PYBIN="$VENV_DIR/bin/python"

# ---------- 4. 离线安装（全程 --no-index，绝不联网） ----------
echo "==> 升级 pip（离线，用包里的 pip wheel）"
"$PIP" install --quiet --no-index --find-links "$WHEELS" \
    --disable-pip-version-check --upgrade pip || \
    echo "    跳过：包内没有 pip wheel，沿用虚拟环境自带的 pip"

if [[ -f "$SOURCE/requirements-dev.txt" ]]; then
    echo "==> 离线安装运行 + 开发依赖"
    "$PIP" install --no-index --find-links "$WHEELS" \
        --disable-pip-version-check -r "$SOURCE/requirements-dev.txt"
else
    echo "==> 离线安装运行依赖"
    "$PIP" install --no-index --find-links "$WHEELS" \
        --disable-pip-version-check -r "$SOURCE/requirements.txt"
fi

echo "==> 可编辑安装源码（$SOURCE）"
# 可编辑安装要走 build isolation，pip 会从 --find-links 里拿 setuptools/wheel，
# 所以这两个 wheel 必须在包内（requirements-dev.txt 里已经列了）。
"$PIP" install --no-index --find-links "$WHEELS" \
    --disable-pip-version-check -e "$SOURCE"

# ---------- 5. 可选：安装 DuckDB 扩展 ----------
if [[ -d "$HERE/duckdb_extensions" ]]; then
    EXT_DEST="${EXT_DIR:-$HOME/.duckdb/extensions}"
    echo "==> 安装 DuckDB 扩展到 $EXT_DEST"
    mkdir -p "$EXT_DEST"
    cp -rf "$HERE/duckdb_extensions/." "$EXT_DEST/"
fi

# ---------- 6. 自检 ----------
echo "==> 自检"
"$VENV_DIR/bin/datacompare" --version
"$PYBIN" - <<'EOF'
import duckdb, openpyxl, yaml
print(f"    duckdb   {duckdb.__version__}")
print(f"    openpyxl {openpyxl.__version__}")
print(f"    PyYAML   {yaml.__version__}")
EOF

if [[ "${SKIP_TESTS:-}" == "1" ]]; then
    echo "==> 跳过测试（SKIP_TESTS=1）"
else
    echo "==> 跑测试（应该是全绿）"
    ( cd "$SOURCE" && "$PYBIN" -m pytest -q tests )
fi

# ---------- 7. 验证「可编辑」确实生效 ----------
"$PYBIN" - <<EOF
import datacompare, os
print("    datacompare 来自：", os.path.dirname(datacompare.__file__))
EOF

cat <<EOF

二次开发环境就绪。

跑对比：
    $VENV_DIR/bin/datacompare compare -b 旧数据.csv -a 新数据.csv -k 单据号 -o out

跑测试（源码目录下）：
    cd $SOURCE && $PYBIN -m pytest tests -q

改代码不需要重装：venv 已经指向 $SOURCE/src/datacompare，
改完存盘直接再跑命令即可。加了新的第三方依赖时，
要重做离线包（联网机器上重新跑 build_offline_bundle.py）。

重新打成 wheel（离线也能打，构建依赖已在 venv 里）：
    cd $SOURCE && $PYBIN -m pip wheel --no-deps --no-build-isolation --no-index -w dist .
EOF
