"""从 CHANGELOG.md 中抽出指定版本的正文，供 GitHub Release 使用。

用法::

    python scripts/extract_release_notes.py 0.2.0 notes.md

设计上刻意把逻辑放在 Python 里而不是 CI 的 PowerShell 片段里 ——
PowerShell 在开发机上不一定有，没法本地验证；Python 可以直接跑测试。

约定：**无论如何都会写出 notes.md**（实在找不到该版本就写一句兜底说明），
这样调用方不需要再判断空文件，也不用担心 BOM / 编码问题。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

FALLBACK = "详细变更见仓库根目录的 CHANGELOG.md。"


def extract(version: str, text: str) -> str:
    """返回 ``## [version]`` 到下一个 ``## `` 之间的正文；找不到返回空串。"""
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.startswith("## ") and f"[{version}]" in line:
            start = index
            break
    if start is None:
        return ""

    body: list[str] = []
    for line in lines[start + 1:]:
        if line.startswith("## "):  # 下一个版本标题，到此为止
            break
        body.append(line)
    return "\n".join(body).strip()


def build_notes(version: str, changelog: Path = CHANGELOG) -> str:
    """读 CHANGELOG 并返回该版本的发布说明（带兜底）。"""
    if not changelog.exists():
        return FALLBACK
    body = extract(version, changelog.read_text(encoding="utf-8"))
    if not body:
        return FALLBACK
    return body


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2

    version, out_path = argv[1], argv[2]
    notes = build_notes(version)
    # newline="\n" 保证在 Windows runner 上也只写 LF，避免 gh 把 CRLF 带进 Release
    Path(out_path).write_text(notes + "\n", encoding="utf-8", newline="\n")
    print(f"已写出 {out_path}（{len(notes)} 字符，版本 {version}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
