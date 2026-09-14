"""生成示例数据：故意埋入各类差异，用来演示和验证工具能力。

运行： python examples/make_sample.py
"""

from __future__ import annotations

import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))

HEADER = ["门店编码", "单据号", "业务日期", "商品", "数量", "单价", "金额", "折扣率", "状态", "备注"]

STORES = ["001", "002", "003", "004", "005"]
GOODS = ["苹果", "香蕉", "牛奶", "面包", "咖啡豆", "洗衣液"]
STATUS = ["已完成", "已发货", "待付款", "已取消"]


def build_rows(n: int, seed: int):
    rnd = random.Random(seed)
    rows = []
    for i in range(n):
        store = rnd.choice(STORES)
        doc = f"SO{202400000 + i:09d}"
        day = rnd.randint(1, 28)
        date = f"2024-01-{day:02d}"
        goods = rnd.choice(GOODS)
        qty = rnd.randint(1, 20)
        price = round(rnd.uniform(3, 60), 2)
        amount = round(qty * price, 2)
        discount = rnd.choice(["1.0", "0.95", "0.9", "0.85"])
        status = rnd.choice(STATUS)
        remark = rnd.choice(["", "加急", "客户指定", "赠品", ""])
        rows.append([store, doc, date, goods, str(qty), f"{price:.2f}",
                     f"{amount:.2f}", discount, status, remark])
    return rows


def mutate(rows):
    """把「前」数据集改造成「后」数据集，埋入各类差异。"""
    rnd = random.Random(42)
    out = [list(r) for r in rows]

    def idx(col):
        return HEADER.index(col)

    # 1) 金额/单价：只改写法（补零、千分位）—— 应被识别为「仅格式差异」
    for i in range(0, 40, 3):
        value = float(out[i][idx("金额")])
        out[i][idx("金额")] = f"{value:,.1f}" if value >= 1000 else f"{value:g}"
        p = float(out[i][idx("单价")])
        out[i][idx("单价")] = f"{p:g}"

    # 2) 日期：只改写法
    for i in range(1, 40, 4):
        out[i][idx("业务日期")] = out[i][idx("业务日期")].replace("-", "/")

    # 3) 状态：加尾随空格 / 改大小写（中文没有大小写，用空格演示）
    for i in range(2, 40, 5):
        out[i][idx("状态")] = out[i][idx("状态")] + "  "

    # 4) 备注：前为空串，后为 NULL（或反之）—— 按默认口径视为一致
    for i in range(3, 40, 6):
        out[i][idx("备注")] = ""

    # 5) 数量：真实数值变化
    for i in (5, 11, 17, 23, 29, 35):
        out[i][idx("数量")] = str(int(out[i][idx("数量")]) + rnd.randint(1, 5))

    # 6) 折扣率：真实数值变化
    out[7][idx("折扣率")] = "0.8"
    out[13][idx("折扣率")] = "0.75"

    # 7) 数值型脏数据：数值列里混进文本 —— 应报「类型/格式异常」
    out[9][idx("金额")] = "待确认"
    out[15][idx("数量")] = "N/A"

    # 8) 空值注入：某字段空值率明显上升
    for i in range(0, 12):
        out[i][idx("备注")] = ""

    # 9) 离群点：金额异常大
    out[21][idx("金额")] = "999999.99"

    # 10) 新增取值
    out[25][idx("商品")] = "蓝莓"
    out[26][idx("商品")] = "蓝莓"

    # 11) 删掉几行（只在前数据集存在）
    for i in sorted([30, 31, 32], reverse=True):
        out.pop(i)

    # 12) 追加几行（只在后数据集存在）
    extra = build_rows(5, seed=999)
    for k, row in enumerate(extra):
        row[1] = f"SO{209900000 + k:09d}"
    out.extend(extra)

    # 13) 制造重复主键（单据号重复）
    out.append(list(out[0]))
    out.append(list(out[1]))

    return out


def write_csv(path: str, rows, encoding: str = "utf-8"):
    import csv

    with open(path, "w", encoding=encoding, newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        writer.writerows(rows)
    print(f"写入 {path}（{len(rows)} 行）")


def main():
    before = build_rows(200, seed=7)
    after = mutate(before)

    write_csv(os.path.join(HERE, "before.csv"), before)
    write_csv(os.path.join(HERE, "after.csv"), after)
    # GBK 版本，用来验证编码自动转换
    write_csv(os.path.join(HERE, "before_gbk.csv"), before, encoding="gbk")

    # 无表头 + 分号分隔的文本文件，验证自定义分隔符
    with open(os.path.join(HERE, "after_semicolon.txt"), "w", encoding="utf-8", newline="") as fh:
        for row in after:
            fh.write(";".join(row) + "\n")
    print(f"写入 {os.path.join(HERE, 'after_semicolon.txt')}（无表头，分号分隔）")


if __name__ == "__main__":
    main()
