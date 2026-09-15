"""GUI（本机 Web 界面）的测试。

真的起一个 HTTP 服务在随机端口上跑，用 urllib 打请求——
比 mock 掉 handler 更能发现路由 / 序列化 / 目录穿越这类问题。
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from datacompare import gui


# --------------------------------------------------------------------------
# 纯函数部分
# --------------------------------------------------------------------------
def test_browse_lists_directory(tmp_path):
    (tmp_path / "a.csv").write_text("x\n1\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "note.docx").write_text("x", encoding="utf-8")
    (tmp_path / ".hidden").write_text("x", encoding="utf-8")

    data = gui.browse(str(tmp_path))
    names = [e["name"] for e in data["entries"]]

    assert "sub" in names and "a.csv" in names
    assert ".hidden" not in names, "隐藏文件不该出现"
    by_name = {e["name"]: e for e in data["entries"]}
    assert by_name["sub"]["isDir"] is True
    assert by_name["a.csv"]["selectable"] is True
    assert by_name["note.docx"]["selectable"] is False, "非数据文件不可选"
    assert data["path"] == str(tmp_path)


def test_browse_accepts_a_file_path(tmp_path):
    target = tmp_path / "a.csv"
    target.write_text("x\n", encoding="utf-8")
    # 传文件路径时应返回它所在目录，方便用户直接粘一个完整路径
    assert gui.browse(str(target))["path"] == str(tmp_path)


def test_browse_rejects_missing_dir():
    with pytest.raises(ValueError):
        gui.browse("/definitely/not/exists/xyz")


def test_config_from_payload(tmp_path):
    payload = {
        "before": {"path": str(tmp_path / "a.csv"), "encoding": "gbk",
                   "delimiter": "\\t", "header": False, "sheet": "2"},
        "after": {"path": str(tmp_path / "b.xlsx"), "sheet": "明细"},
        "keys": "门店, 日期 ,",
        "ignoreCase": True,
        "outDir": str(tmp_path / "out"),
    }
    cfg = gui._config_from_payload(payload)

    assert cfg.before.encoding == "gbk"
    assert cfg.before.delimiter == "\t", "\\t 应该被还原成真正的制表符"
    assert cfg.before.header is False
    assert cfg.before.sheet == 2, "纯数字的工作表应转成序号"
    assert cfg.after.sheet == "明细", "非数字的工作表名保持字符串"
    assert cfg.keys == ["门店", "日期"], "空白项要被丢掉"
    assert cfg.compare.string.case_insensitive is True
    assert cfg.output_dir == str(tmp_path / "out")


def test_config_requires_paths():
    with pytest.raises(ValueError, match="请填写"):
        gui._config_from_payload({"before": {}, "after": {"path": "x.csv"}})


def test_config_accepts_chinese_comma(tmp_path):
    cfg = gui._config_from_payload({
        "before": {"path": "a.csv"}, "after": {"path": "b.csv"},
        "keys": "单据号，门店",
    })
    assert cfg.keys == ["单据号", "门店"]


# --------------------------------------------------------------------------
# HTTP 接口
# --------------------------------------------------------------------------
@pytest.fixture()
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), gui.Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _post(base, path, payload):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # 4xx/5xx 也要把 body 读出来，接口的错误信息就在里面
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get(base, path):
    try:
        with urllib.request.urlopen(base + path, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_index_page(server):
    status, body = _get(server, "/")
    assert status == 200
    text = body.decode("utf-8")
    assert "数据集对比" in text
    # 三个步骤的骨架都在
    for marker in ("选择数据", "确认字段与主键", "执行对比"):
        assert marker in text


def test_status_api_idle(server):
    status, body = _get(server, "/api/status")
    assert status == 200
    data = json.loads(body)
    assert data["ok"] is True
    assert data["state"] in ("idle", "running", "done", "error")


def test_browse_api(server, tmp_path):
    (tmp_path / "x.csv").write_text("a\n", encoding="utf-8")
    status, data = _post(server, "/api/browse", {"path": str(tmp_path)})
    assert status == 200 and data["ok"] is True
    assert any(e["name"] == "x.csv" for e in data["entries"])


def test_bad_json_returns_400(server):
    req = urllib.request.Request(
        server + "/api/browse", data=b"{not json",
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=10)
    assert exc.value.code == 400


def test_unknown_route_404(server):
    assert _get(server, "/api/nope")[0] == 404


def test_inspect_api(server, tmp_path):
    before = tmp_path / "before.csv"
    after = tmp_path / "after.csv"
    before.write_text("id,金额\nA,1.50\nB,2.00\n", encoding="utf-8")
    after.write_text("id,金额\nA,1.5\nB,3.00\n", encoding="utf-8")

    status, data = _post(server, "/api/inspect", {
        "before": {"path": str(before)},
        "after": {"path": str(after)},
        "previewRows": 1000,
    })
    assert status == 200 and data["ok"] is True
    assert data["keys"] == ["id"]
    types = {c["name"]: c["type"] for c in data["columns"]}
    assert types["金额"] == "numeric"
    assert types["id"] == "key" or data["columns"][0]["mode"] == "key"


def test_inspect_reports_missing_file(server, tmp_path):
    status, data = _post(server, "/api/inspect", {
        "before": {"path": str(tmp_path / "nope.csv")},
        "after": {"path": str(tmp_path / "nope2.csv")},
    })
    assert status == 400
    assert data["ok"] is False
    assert "nope.csv" in data["error"] or "找不到" in data["error"]


def test_file_serving_blocks_traversal(server):
    for evil in ("/files/..%2f..%2fetc%2fpasswd", "/files/a/b"):
        status, _ = _get(server, evil)
        assert status in (400, 404), f"{evil} 不该被放行"


def test_export_config_api(server, tmp_path):
    status, data = _post(server, "/api/export-config", {
        "before": {"path": str(tmp_path / "a.csv")},
        "after": {"path": str(tmp_path / "b.csv")},
        "keys": ["id"],
    })
    assert status == 200 and data["ok"] is True
    assert "keys:" in data["yaml"]
    assert "id" in data["yaml"]


def test_run_end_to_end_and_serve_report(server, tmp_path):
    """完整跑一次：提交任务 → 轮询到完成 → 取报告文件。"""
    before = tmp_path / "before.csv"
    after = tmp_path / "after.csv"
    before.write_text("id,金额\nA,1.50\nB,2.00\n", encoding="utf-8")
    after.write_text("id,金额\nA,1.5\nB,3.00\n", encoding="utf-8")
    out = tmp_path / "out"

    status, data = _post(server, "/api/run", {
        "before": {"path": str(before)},
        "after": {"path": str(after)},
        "keys": "id",
        "outDir": str(out),
    })
    assert status == 200 and data["ok"] is True

    # 等任务结束
    import time

    deadline = time.time() + 60
    state = None
    while time.time() < deadline:
        _, body = _get(server, "/api/status")
        state = json.loads(body)
        if state["state"] in ("done", "error"):
            break
        time.sleep(0.2)

    assert state is not None, "拿不到任务状态"
    assert state["state"] == "done", f"任务失败：{state.get('error')}\n{state.get('logs')}"

    stats = state["stats"]
    # 1.50 vs 1.5 是仅格式差异；2.00 vs 3.00 是实质差异
    assert stats["severeCellDiffs"] == 1
    assert stats["formatOnlyCells"] == 1
    assert stats["matchedPairs"] == 2

    html = [r for r in state["reports"] if r["name"].endswith(".html")]
    assert html, "应该产出 HTML 报告"
    code, body = _get(server, html[0]["url"])
    assert code == 200
    assert "前后数据集对比报告" in body.decode("utf-8")
    assert os.path.isdir(out)


def test_concurrent_run_is_rejected(server, tmp_path):
    """同一时刻只允许一个任务。"""
    gui.JOB.reset()
    gui.JOB.set(state="running")
    try:
        status, data = _post(server, "/api/run", {
            "before": {"path": str(tmp_path / "a.csv")},
            "after": {"path": str(tmp_path / "b.csv")},
        })
        assert status == 400
        assert "已有任务" in data["error"]
    finally:
        gui.JOB.reset()
