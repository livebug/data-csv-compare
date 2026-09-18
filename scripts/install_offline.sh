#!/usr/bin/env bash
# datacompare 离线安装脚本（Linux / macOS）
#
# 在**完全离线**的机器上执行：
#     bash install_offline.sh
#
# 可选环境变量：
#     PYTHON=python3.13      指定解释器（默认自动挑 3.9+ 的 python3）
#     VENV_DIR=./venv        虚拟环境目录
#     EXT_DIR=~/.duckdb/extensions    DuckDB 扩展的安装位置

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-$HERE/venv}"
WHEELS="$HERE/wheels"

echo "==> datacompare 离线安装"
echo "    安装包：$HERE"

# ---------- 1. 找一个可用的 Python ----------
PY="${PYTHON:-}"
if [[ -z "$PY" ]]; then
    for cand in python3.13 python3.12 python3.11 python3.10 python3.9 python3 python; do
        if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
    done
fi
if [[ -z "$PY" ]]; then
    echo "错误：找不到 Python 解释器。" >&2
    echo "      本方案要求目标机器已装 Python 3.9+。" >&2
    echo "      完全没有 Python 请用免安装整体包（见 docs/DEPLOY.md 方案 B）。" >&2
    echo "      完全没有 Python 请改用 PyInstaller 单文件方案（见 docs/DEPLOY.md 方案 C）。" >&2
    exit 1
fi
PYVER="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo "==> 使用解释器：$PY (Python $PYVER)"

# 版本下限检查
"$PY" - <<'EOF' || { echo "错误：需要 Python 3.9 或更高版本" >&2; exit 1; }
import sys
raise SystemExit(0 if sys.version_info >= (3, 9) else 1)
EOF

if [[ ! -d "$WHEELS" ]]; then
    echo "错误：找不到 wheels 目录（$WHEELS）" >&2
    exit 1
fi

# ---------- 2. 建虚拟环境并离线安装 ----------
echo "==> 创建虚拟环境：$VENV_DIR"
"$PY" -m venv "$VENV_DIR"

PIP="$VENV_DIR/bin/pip"
echo "==> 离线安装依赖（不访问任何网络）"
"$PIP" install --quiet --upgrade pip 2>/dev/null || true
"$PIP" install \
    --no-index \
    --find-links "$WHEELS" \
    --disable-pip-version-check \
    datacompare

# ---------- 3. 可选：安装 DuckDB 扩展 ----------
if [[ -d "$HERE/duckdb_extensions" ]]; then
    EXT_DEST="${EXT_DIR:-$HOME/.duckdb/extensions}"
    echo "==> 安装 DuckDB 扩展到 $EXT_DEST"
    # duckdb_extensions 内部结构镜像 extension_directory：<版本>/<平台>/*.duckdb_extension
    mkdir -p "$EXT_DEST"
    cp -rf "$HERE/duckdb_extensions/." "$EXT_DEST/"
    find "$HERE/duckdb_extensions" -name "*.duckdb_extension" | while read -r f; do
        echo "    ${f#"$HERE/duckdb_extensions/"}"
    done
fi

# ---------- 4. 自检 ----------
echo "==> 自检"
"$VENV_DIR/bin/datacompare" --version
"$VENV_DIR/bin/python" - <<'EOF'
import duckdb, openpyxl, yaml
print(f"    duckdb  {duckdb.__version__}")
print(f"    openpyxl {openpyxl.__version__}")
print(f"    PyYAML  {yaml.__version__}")
EOF

cat <<EOF

安装完成。

用法：
    $VENV_DIR/bin/datacompare compare -b 旧数据.csv -a 新数据.csv -k 单据号 -o 输出目录

长期使用可以加到 PATH：
    echo 'export PATH="$VENV_DIR/bin:\$PATH"' >> ~/.bashrc && source ~/.bashrc
EOF
