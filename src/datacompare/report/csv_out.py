"""把差异明细导成 CSV。

直接用 DuckDB 的 ``COPY ... TO ... (FORMAT csv)`` 落盘——C++ 实现，
比在 Python 里逐行 ``writerow`` 快一个数量级，也不会把几百万行拉进内存。
输出带 UTF-8 BOM，方便 Excel 直接双击打开不乱码。
"""

from __future__ import annotations

from ..config import Config
from ..sqlutil import sql_str
from . import data as D


def write_diff_csv(result, cfg: Config, path: str, limit: int = 0) -> str:
    inner = D.diff_rows_sql(
        result.con,
        cfg,
        result.stats,
        limit=limit or None,
        order_by_value=False,
        label_status=True,
    )
    result.con.execute(
        f"COPY ({inner}) TO {sql_str(path)} "
        "(FORMAT csv, HEADER true, DELIMITER ',', QUOTE '\"')"
    )
    _prepend_bom(path)
    return path


def _prepend_bom(path: str) -> None:
    """给文件加 UTF-8 BOM，这样 Excel 双击打开中文不乱码。

    用「临时文件 + 分块拷贝」而不是整体读进内存，大文件（几百 MB）也不会炸。
    """
    import os
    import shutil
    import tempfile

    mode = os.stat(path).st_mode & 0o777
    with open(path, "rb") as src, tempfile.NamedTemporaryFile("wb", delete=False) as tmp:
        tmp.write(b"\xef\xbb\xbf")
        shutil.copyfileobj(src, tmp, length=1 << 20)
        tmp_path = tmp.name
    os.chmod(tmp_path, mode)          # NamedTemporaryFile 默认 0600，要还原成原权限
    shutil.move(tmp_path, path)
