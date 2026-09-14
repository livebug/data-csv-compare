"""生成 SQL 片段的小工具。

DuckDB 的字符串字面量会处理反斜杠转义（``\\n`` / ``\\t`` / ``\\uXXXX`` 等），
所以这里统一提供转义函数，避免把用户数据里的引号或反斜杠拼进 SQL 时报错。
"""

from __future__ import annotations

from typing import Iterable


def quote_ident(name: str) -> str:
    """把列名/表名转成安全的双引号标识符。"""
    return '"' + str(name).replace('"', '""') + '"'


def sql_str(value: str) -> str:
    """把 Python 字符串转成 SQL 字符串字面量。

    反斜杠统一写成 ``\\\\``，避免 DuckDB 把 ``\\s``、``\\d`` 之类
    误当成转义序列处理（``\\u`` 会直接报 invalid escape sequence）。
    """
    text = str(value)
    text = text.replace("\\", "\\\\").replace("'", "''")
    return "'" + text + "'"


def sql_list(values: Iterable[str]) -> str:
    """把一组字符串转成 SQL 的 ``('a', 'b')`` 列表字面量。"""
    return "(" + ", ".join(sql_str(v) for v in values) + ")"


def sql_regex(pattern: str) -> str:
    """把正则表达式转成 SQL 字面量。

    与 :func:`sql_str` 不同，正则里的反斜杠必须原样保留给正则引擎，
    因此这里不做反斜杠加倍，只处理单引号。
    注意：DuckDB 会把 ``\\s`` 当作未知转义原样传递，但 ``\\u`` 会报错，
    所以构造正则时不要使用 ``\\uXXXX`` 形式，直接用真实字符。
    """
    return "'" + pattern.replace("'", "''") + "'"


def fmt_float(value: float) -> str:
    """把浮点数渲染成不会丢精度的 SQL 字面量。"""
    if value == int(value) and abs(value) < 1e15:
        return f"{value:.1f}"
    return repr(float(value))
