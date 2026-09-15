"""本机 Web 界面（`datacompare gui`）。

为什么是 Web 而不是桌面 GUI
---------------------------
1. **零新增依赖**：只用 Python 标准库 ``http.server``。桌面 GUI（tkinter / PyQt）
   会把离线包体积撑大好几倍，Linux 上还得额外装 ``python3-tk``，离线更麻烦。
2. **报告本来就是 HTML**，呈现层天然是 Web，缺的只是「配置 + 触发」这一段。
3. **离线内网友好**：进程跑在数据所在的机器上，浏览器一个 URL 就能用；
   要远程访问就 ``--host 0.0.0.0``，不需要部署任何 Web 服务。

设计上的一个关键取舍：**输入用「文件路径」而不是「上传文件」**。
目标场景是内网机器上已经躺着几十 GB 的数据文件，让浏览器上传 10GB 的 CSV
是荒唐的。所以界面上填路径，另外提供一个受限的目录浏览方便点选。

安全说明
--------
* 默认只监听 ``127.0.0.1``（本机可访问）
* 静态文件只从本次任务的输出目录里取，且用 ``basename`` 过滤，防目录穿越
* 没有登录态 —— 它是本机工具，不是多租户服务
"""

from __future__ import annotations

import json
import os
import socket
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlparse

from . import __version__
from .config import Config, dump_config, load_config
from .engine import CompareEngine

# --------------------------------------------------------------------------
# 进度：把引擎的日志映射成阶段百分比
# --------------------------------------------------------------------------
_PHASES = [
    ("读取前数据集", 8),
    ("读取后数据集", 18),
    ("字段画像", 32),
    ("按主键匹配行", 48),
    ("逐字段比对", 62),
    ("汇总行级结论", 76),
    ("检测异常数据", 86),
    ("生成报告", 95),
]


class Job:
    """一次对比任务的状态。同一时刻只跑一个（DuckDB 工作库不便并发）。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with getattr(self, "lock", threading.Lock()):
            self.state = "idle"        # idle | running | done | error
            self.percent = 0
            self.phase = ""
            self.logs: List[str] = []
            self.error = ""
            self.out_dir = ""
            self.reports: List[Dict[str, Any]] = []
            self.stats: Dict[str, Any] = {}
            self.title = ""

    # -- 线程安全的写入口 -------------------------------------------------
    def log(self, message: str) -> None:
        with self.lock:
            self.logs.append(message)
            del self.logs[:-300]           # 只留最近 300 行
            for keyword, percent in _PHASES:
                if keyword in message:
                    self.phase = keyword.rstrip("…：")
                    self.percent = max(self.percent, percent)
                    break

    def set(self, **kwargs) -> None:
        with self.lock:
            for key, value in kwargs.items():
                setattr(self, key, value)

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "state": self.state,
                "percent": self.percent,
                "phase": self.phase,
                "logs": list(self.logs),
                "error": self.error,
                "reports": list(self.reports),
                "stats": dict(self.stats),
                "outDir": self.out_dir,
            }


JOB = Job()


# --------------------------------------------------------------------------
# 配置装配
# --------------------------------------------------------------------------
def _source_from_payload(payload: Dict[str, Any], prefix: str):
    from .config import SourceSpec

    raw = payload.get(prefix) or {}
    spec = SourceSpec(path=str(raw.get("path") or "").strip())
    if not spec.path:
        raise ValueError(f"请填写{'前' if prefix == 'before' else '后'}数据集的文件路径")

    encoding = str(raw.get("encoding") or "").strip()
    if encoding:
        spec.encoding = encoding

    delimiter = raw.get("delimiter")
    if delimiter:
        spec.delimiter = (
            str(delimiter).replace("\\t", "\t").replace("\\n", "\n")
        )

    if raw.get("header") is not None:
        spec.header = bool(raw["header"])

    sheet = raw.get("sheet")
    if sheet not in (None, ""):
        try:
            spec.sheet = int(sheet)
        except (TypeError, ValueError):
            spec.sheet = str(sheet)
    return spec


def _config_from_payload(payload: Dict[str, Any]) -> Config:
    base = payload.get("configPath")
    cfg = load_config(base) if base else Config()
    cfg.before = _source_from_payload(payload, "before")
    cfg.after = _source_from_payload(payload, "after")

    keys = payload.get("keys")
    if isinstance(keys, list):
        cfg.keys = [str(k).strip() for k in keys if str(k).strip()]
    elif isinstance(keys, str):
        cfg.keys = [k.strip() for k in keys.replace("，", ",").split(",") if k.strip()]

    if payload.get("ignoreCase"):
        cfg.compare.string.case_insensitive = True
    if payload.get("outDir"):
        cfg.output_dir = str(payload["outDir"])
    if payload.get("title"):
        cfg.report.title = str(payload["title"])
    if payload.get("treatFormatOnlyAsDiff"):
        cfg.report.treat_format_only_as_diff = True
    return cfg


# --------------------------------------------------------------------------
# 目录浏览（方便在内网点选文件，不依赖系统文件对话框）
# --------------------------------------------------------------------------
def browse(path: str) -> Dict[str, Any]:
    path = os.path.abspath(os.path.expanduser(path or os.getcwd()))
    if os.path.isfile(path):
        path = os.path.dirname(path)
    if not os.path.isdir(path):
        raise ValueError(f"目录不存在：{path}")

    entries = []
    try:
        names = sorted(os.listdir(path), key=lambda s: (not os.path.isdir(os.path.join(path, s)), s.lower()))
    except PermissionError as exc:
        raise ValueError(f"没有权限读取目录：{path}") from exc

    data_ext = {".csv", ".tsv", ".txt", ".dat", ".psv", ".xlsx", ".xlsm",
                ".parquet", ".pq", ".json", ".jsonl", ".ndjson"}
    for name in names[:2000]:
        if name.startswith("."):
            continue
        full = os.path.join(path, name)
        try:
            is_dir = os.path.isdir(full)
            size = 0 if is_dir else os.path.getsize(full)
        except OSError:
            continue
        ext = os.path.splitext(name)[1].lower()
        entries.append({
            "name": name,
            "isDir": is_dir,
            "size": size,
            "selectable": is_dir or ext in data_ext,
        })
    parent = os.path.dirname(path)
    return {
        "path": path,
        "parent": parent if parent != path else "",
        "entries": entries,
    }


# --------------------------------------------------------------------------
# 执行对比
# --------------------------------------------------------------------------
def _run_job(cfg: Config) -> None:
    from .report import write_reports

    engine = CompareEngine(cfg, log=JOB.log)
    try:
        JOB.log("开始：读取数据并对比")
        result = engine.run()
        JOB.log("生成报告…")
        written = write_reports(result, cfg, log=JOB.log)

        stats = result.stats
        reports = []
        for path in written:
            reports.append({
                "name": os.path.basename(path),
                "url": "/files/" + os.path.basename(path),
                "size": os.path.getsize(path) if os.path.exists(path) else 0,
            })
        # HTML 放最前面，方便一键打开
        reports.sort(key=lambda r: (not r["name"].endswith(".html"), r["name"]))

        JOB.set(
            state="done",
            percent=100,
            phase="完成",
            reports=reports,
            out_dir=os.path.abspath(cfg.output_dir),
            stats={
                "beforeRows": stats.before_rows,
                "afterRows": stats.after_rows,
                "matchedPairs": stats.matched_pairs,
                "sameRows": stats.same_rows,
                "changedRows": stats.changed_rows,
                "onlyInBefore": stats.only_in_before,
                "onlyInAfter": stats.only_in_after,
                "cellDiffs": stats.cell_diffs,
                "severeCellDiffs": stats.severe_cell_diffs,
                "formatOnlyCells": stats.format_only_cells,
                "comparedColumns": stats.compared_columns,
                "dupKeyBefore": stats.dup_key_before,
                "dupKeyAfter": stats.dup_key_after,
                "consistent": stats.consistent,
                "keyColumns": list(stats.key_columns),
            },
        )
        JOB.log("完成")
    except Exception as exc:  # noqa: BLE001 - 要把错误显示到界面上
        JOB.log(f"失败：{exc}")
        JOB.set(state="error", error=f"{type(exc).__name__}: {exc}",
                detail=traceback.format_exc()[-4000:])
    finally:
        engine.close(cleanup=not bool(cfg.work_db))


# --------------------------------------------------------------------------
# HTTP 处理
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = f"datacompare/{__version__}"

    # -- 工具 -------------------------------------------------------------
    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _error(self, message: str, code: int = 400) -> None:
        self._json({"ok": False, "error": str(message)}, code)

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"请求体不是合法 JSON：{exc}") from exc

    def log_message(self, fmt, *args):  # 安静点，别把访问日志刷到控制台
        pass

    # -- 路由 -------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, _PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/status":
            self._json({"ok": True, **JOB.snapshot()})
        elif path.startswith("/files/"):
            self._serve_file(unquote(path[len("/files/"):]))
        else:
            self._error("未知路径", 404)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            if path == "/api/browse":
                self._json({"ok": True, **browse(str(payload.get("path") or ""))})
            elif path == "/api/inspect":
                self._handle_inspect(payload)
            elif path == "/api/run":
                self._handle_run(payload)
            elif path == "/api/export-config":
                self._handle_export(payload)
            else:
                self._error("未知路径", 404)
        except (ValueError, FileNotFoundError, RuntimeError) as exc:
            # 文件找不到 / 参数填错 / 文件解析不了 —— 都是用户侧问题，
            # 用 400 让前端能和「服务端炸了」区分开
            self._error(exc, 400)
        except Exception as exc:  # noqa: BLE001
            self._error(f"{type(exc).__name__}: {exc}", 500)

    # -- 各接口 -----------------------------------------------------------
    def _handle_inspect(self, payload: Dict[str, Any]) -> None:
        cfg = _config_from_payload(payload)
        preview = int(payload.get("previewRows") or 20000)
        info = CompareEngine(cfg).inspect(preview_rows=max(1000, min(preview, 200000)))
        info["title"] = cfg.report.title
        info["outDir"] = os.path.abspath(cfg.output_dir)
        self._json(info)

    def _handle_run(self, payload: Dict[str, Any]) -> None:
        with JOB.lock:
            if JOB.state == "running":
                raise ValueError("已有任务在执行，请等它结束")
        cfg = _config_from_payload(payload)
        JOB.reset()
        JOB.set(state="running", percent=2, phase="启动中")
        thread = threading.Thread(target=_run_job, args=(cfg,), daemon=True)
        thread.start()
        self._json({"ok": True})

    def _handle_export(self, payload: Dict[str, Any]) -> None:
        cfg = _config_from_payload(payload)
        self._json({"ok": True, "yaml": dump_config(cfg)})

    def _serve_file(self, name: str) -> None:
        safe = os.path.basename(name)
        if not safe or safe != name:
            self._error("非法文件名", 400)
            return
        base = JOB.out_dir
        if not base:
            self._error("还没有生成报告", 404)
            return
        full = os.path.join(base, safe)
        if not os.path.isfile(full):
            self._error("文件不存在", 404)
            return
        ext = os.path.splitext(safe)[1].lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".md": "text/markdown; charset=utf-8",
            ".csv": "text/csv; charset=utf-8",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".duckdb": "application/octet-stream",
        }.get(ext, "application/octet-stream")
        with open(full, "rb") as fh:
            self._send(200, fh.read(), ctype)


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------
def _pick_port(host: str, port: int) -> int:
    if port:
        return port
    with socket.socket() as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def run_gui(
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    work_dir: Optional[str] = None,
    print_fn=print,
) -> int:
    """启动本机 Web 界面，阻塞直到 Ctrl+C。"""
    if work_dir:
        os.chdir(work_dir)
    port = _pick_port(host, port)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True

    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{shown}:{port}/"
    print_fn("")
    print_fn("  datacompare 图形界面已启动")
    print_fn(f"    → {url}")
    print_fn(f"    工作目录：{os.getcwd()}")
    if host in ("0.0.0.0", "::"):
        print_fn("    ⚠ 已监听所有网卡，同网段的人都能访问，请确认网络环境可信")
    print_fn("    按 Ctrl+C 退出")
    print_fn("")

    if open_browser:
        threading.Timer(0.5, lambda: _safe_open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print_fn("\n  已停止")
    finally:
        httpd.server_close()
    return 0


def _safe_open(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:  # pragma: no cover - 无桌面环境时静默失败
        pass


_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>datacompare · 数据集对比</title>
<style>
  :root{
    --bg:#f6f7fb; --panel:#fff; --line:#e5e7eb; --text:#111827; --muted:#6b7280;
    --brand:#2563eb; --ok:#16a34a; --warn:#d97706; --bad:#dc2626;
    --shadow:0 1px 2px rgba(16,24,40,.06), 0 4px 16px rgba(16,24,40,.06);
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
    font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
  header{background:linear-gradient(135deg,#1e293b,#0f172a);color:#fff;padding:20px 28px}
  header h1{margin:0;font-size:19px;font-weight:650}
  header .sub{color:#94a3b8;font-size:12.5px;margin-top:3px}
  .wrap{padding:20px 28px 60px;max-width:1180px;margin:0 auto}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;
    box-shadow:var(--shadow);margin-bottom:16px;overflow:hidden}
  .card > h2{margin:0;padding:13px 20px;font-size:14.5px;font-weight:600;border-bottom:1px solid var(--line);
    display:flex;align-items:center;gap:9px}
  .step{width:20px;height:20px;border-radius:50%;background:var(--brand);color:#fff;font-size:12px;
    display:inline-flex;align-items:center;justify-content:center;flex:none;font-weight:700}
  .card > h2.done .step{background:var(--ok)}
  .card > h2 .hint{font-weight:400;color:var(--muted);font-size:12.5px;margin-left:auto}
  .body{padding:18px 20px}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  @media(max-width:820px){.grid{grid-template-columns:1fr}}
  label{display:block;font-size:12.5px;color:var(--muted);margin-bottom:5px;font-weight:500}
  input[type=text],input[type=number],select{width:100%;padding:8px 11px;border:1px solid var(--line);
    border-radius:8px;font-size:13.5px;background:#fff;color:var(--text);outline:none;font-family:inherit}
  input:focus,select:focus{border-color:var(--brand);box-shadow:0 0 0 3px rgba(37,99,235,.12)}
  .row{display:flex;gap:8px;align-items:flex-end}
  .row input{flex:1}
  .btn{padding:8px 15px;border-radius:8px;border:1px solid var(--line);background:#fff;cursor:pointer;
    font-size:13.5px;font-family:inherit;white-space:nowrap}
  .btn:hover{background:#f3f4f6}
  .btn.primary{background:var(--brand);border-color:var(--brand);color:#fff;font-weight:600}
  .btn.primary:hover{background:#1d4ed8}
  .btn:disabled{opacity:.5;cursor:not-allowed}
  .btn.danger{color:var(--bad);border-color:#fecaca}
  details{margin-top:12px}
  summary{cursor:pointer;color:var(--brand);font-size:13px;user-select:none}
  .adv{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:12px}
  @media(max-width:820px){.adv{grid-template-columns:1fr 1fr}}
  table{border-collapse:separate;border-spacing:0;width:100%;font-size:13px}
  th{text-align:left;padding:8px 10px;background:#f9fafb;border-bottom:1px solid var(--line);
    font-size:12.5px;font-weight:600;color:#374151;white-space:nowrap}
  td{padding:6px 10px;border-bottom:1px solid #f1f3f5;vertical-align:top}
  tbody tr:hover td{background:#f8fafc}
  .pill{display:inline-block;padding:1px 9px;border-radius:999px;font-size:11.5px;font-weight:600}
  .pill.num{background:#eff6ff;color:#1d4ed8}
  .pill.date{background:#f0fdf4;color:#15803d}
  .pill.string{background:#f5f3ff;color:#6d28d9}
  .pill.key{background:#fef3c7;color:#92400e}
  .pill.only{background:#fee2e2;color:#991b1b}
  .muted{color:var(--muted);font-size:12.5px}
  .bar{height:7px;background:#e5e7eb;border-radius:99px;overflow:hidden}
  .bar>i{display:block;height:100%;background:var(--brand);width:0;transition:width .4s ease}
  .log{background:#0f172a;color:#cbd5e1;border-radius:8px;padding:11px 14px;font-size:12.5px;
    font-family:ui-monospace,Menlo,Consolas,monospace;max-height:190px;overflow:auto;white-space:pre-wrap}
  .kpis{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin-bottom:14px}
  .kpi{border:1px solid var(--line);border-radius:10px;padding:11px 13px}
  .kpi .k{color:var(--muted);font-size:12px}
  .kpi .v{font-size:19px;font-weight:650;font-variant-numeric:tabular-nums}
  .kpi.bad .v{color:var(--bad)} .kpi.ok .v{color:var(--ok)} .kpi.warn .v{color:var(--warn)}
  .files{display:flex;flex-wrap:wrap;gap:9px}
  .file{display:flex;align-items:center;gap:8px;border:1px solid var(--line);border-radius:9px;
    padding:9px 13px;text-decoration:none;color:var(--text);font-size:13.5px;background:#fff}
  .file:hover{border-color:var(--brand);background:#f8faff}
  .file .sz{color:var(--muted);font-size:12px}
  .alert{padding:11px 15px;border-radius:9px;font-size:13px;margin-bottom:14px;white-space:pre-wrap;
    word-break:break-word}
  .alert.err{background:#fef2f2;border:1px solid #fecaca;color:#991b1b}
  .alert.warn{background:#fffbeb;border:1px solid #fde68a;color:#92400e}
  .alert.ok{background:#ecfdf5;border:1px solid #a7d3bf;color:#065f46}
  .modal{position:fixed;inset:0;background:rgba(15,23,42,.45);display:none;align-items:center;
    justify-content:center;padding:24px;z-index:50}
  .modal.on{display:flex}
  .modal .box{background:#fff;border-radius:12px;max-width:760px;width:100%;max-height:80vh;
    display:flex;flex-direction:column;overflow:hidden}
  .modal .box h3{margin:0;padding:14px 18px;font-size:14.5px;border-bottom:1px solid var(--line)}
  .modal .box .lst{overflow:auto;padding:6px 0}
  .modal .box .lst div{padding:6px 18px;cursor:pointer;font-size:13.5px;display:flex;gap:8px}
  .modal .box .lst div:hover{background:#f3f4f6}
  .modal .box .ft{padding:12px 18px;border-top:1px solid var(--line);display:flex;gap:8px}
  .hide{display:none!important}
  pre.yaml{background:#0f172a;color:#e2e8f0;padding:14px;border-radius:9px;overflow:auto;
    max-height:340px;font-size:12.5px;margin:0}
</style>
</head>
<body>
<header>
  <h1>数据集对比</h1>
  <div class="sub">前后两份数据逐字段比对 · 出报告 · 找异常 <span id="ver"></span></div>
</header>

<div class="wrap">
  <div id="msg"></div>

  <!-- 第一步 -->
  <div class="card">
    <h2><span class="step">1</span>选择数据<span class="hint">填文件路径，或点「浏览」</span></h2>
    <div class="body">
      <div class="grid">
        <div>
          <label>前数据集（旧）</label>
          <div class="row">
            <input type="text" id="beforePath" placeholder="D:\data\before.csv 或 /data/before.csv">
            <button class="btn" onclick="pick('before')">浏览</button>
          </div>
          <details>
            <summary>高级选项</summary>
            <div class="adv">
              <div><label>编码</label><input type="text" id="beforeEncoding" placeholder="utf-8 / gbk"></div>
              <div><label>分隔符</label><input type="text" id="beforeDelimiter" placeholder="自动嗅探"></div>
              <div><label>首行是表头</label>
                <select id="beforeHeader"><option value="">默认（是）</option><option value="1">是</option><option value="0">否</option></select>
              </div>
              <div><label>Excel 工作表</label><input type="text" id="beforeSheet" placeholder="名称或序号"></div>
            </div>
          </details>
        </div>
        <div>
          <label>后数据集（新）</label>
          <div class="row">
            <input type="text" id="afterPath" placeholder="D:\data\after.csv 或 /data/after.csv">
            <button class="btn" onclick="pick('after')">浏览</button>
          </div>
          <details>
            <summary>高级选项</summary>
            <div class="adv">
              <div><label>编码</label><input type="text" id="afterEncoding" placeholder="utf-8 / gbk"></div>
              <div><label>分隔符</label><input type="text" id="afterDelimiter" placeholder="自动嗅探"></div>
              <div><label>首行是表头</label>
                <select id="afterHeader"><option value="">默认（是）</option><option value="1">是</option><option value="0">否</option></select>
              </div>
              <div><label>Excel 工作表</label><input type="text" id="afterSheet" placeholder="名称或序号"></div>
            </div>
          </details>
        </div>
      </div>
      <div style="margin-top:18px">
        <label>报告输出目录</label>
        <div class="row">
          <input type="text" id="outDirInput" placeholder="默认 compare_out（相对当前工作目录）">
          <button class="btn" onclick="pick('outDirInput', true)">浏览</button>
        </div>
      </div>
      <div style="margin-top:16px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <button class="btn primary" id="btnInspect" onclick="doInspect()">分析字段</button>
        <span class="muted">先分析：看看字段怎么比、主键对不对，再跑完整对比</span>
      </div>
    </div>
  </div>

  <!-- 第二步 -->
  <div class="card hide" id="cardFields">
    <h2 class="done"><span class="step">2</span>确认字段与主键<span class="hint" id="previewHint"></span></h2>
    <div class="body">
      <div id="inspectWarn"></div>
      <div style="margin-bottom:14px">
        <label>行匹配主键（逗号分隔，留空则按行号对齐）</label>
        <div class="row">
          <input type="text" id="keys" placeholder="例如：单据号">
          <span class="muted" id="keyStrategy" style="white-space:nowrap"></span>
        </div>
      </div>
      <div style="max-height:340px;overflow:auto;border:1px solid var(--line);border-radius:9px">
        <table><thead><tr>
          <th>字段</th><th>比较方式</th><th>角色</th><th style="text-align:right">空值率</th>
          <th style="text-align:right">取值数</th><th>推断依据</th>
        </tr></thead><tbody id="fieldRows"></tbody></table>
      </div>
      <div style="margin-top:16px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <button class="btn primary" id="btnRun" onclick="doRun()">开始完整对比</button>
        <label style="display:flex;align-items:center;gap:6px;margin:0;font-size:13px;color:var(--text)">
          <input type="checkbox" id="ignoreCase"> 文本比较忽略大小写
        </label>
        <button class="btn" onclick="showConfig()">导出配置（YAML）</button>
      </div>
    </div>
  </div>

  <!-- 第三步 -->
  <div class="card hide" id="cardRun">
    <h2 id="runHead"><span class="step">3</span>执行对比</h2>
    <div class="body">
      <div id="runError"></div>
      <div id="progressBox">
        <div style="display:flex;justify-content:space-between;margin-bottom:6px">
          <span id="phase" class="muted">准备中…</span>
          <span id="pct" class="muted">0%</span>
        </div>
        <div class="bar"><i id="barFill"></i></div>
        <div style="margin-top:14px" class="log" id="logBox"></div>
      </div>
      <div id="resultBox" class="hide">
        <div class="kpis" id="kpis"></div>
        <div class="files" id="files"></div>
        <div class="muted" id="outDir" style="margin-top:12px"></div>
      </div>
    </div>
  </div>
</div>

<!-- 目录浏览 -->
<div class="modal" id="modal">
  <div class="box">
    <h3 id="modalTitle">选择文件</h3>
    <div style="padding:10px 18px;border-bottom:1px solid var(--line)">
      <div class="row">
        <input type="text" id="modalPath">
        <button class="btn" onclick="modalGo()">前往</button>
        <button class="btn" onclick="modalUp()">上级</button>
      </div>
    </div>
    <div class="lst" id="modalList"></div>
    <div class="ft">
      <button class="btn primary" onclick="modalChoose()">选择当前目录</button>
      <button class="btn" onclick="closeModal()">取消</button>
    </div>
  </div>
</div>

<script>
const $ = (id) => document.getElementById(id);
const esc = (v) => v === null || v === undefined ? '' :
  String(v).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const nf = (n) => (n === null || n === undefined) ? '-' : Number(n).toLocaleString('zh-CN');
const pf = (n) => (n === null || n === undefined) ? '-' : (n * 100).toFixed(2) + '%';
const TYPES = {numeric: '数值', date: '日期', string: '文本', boolean: '布尔'};
const ROLES = {compared: '参与对比', key: '主键', only_before: '仅前有', only_after: '仅有后', ignored: '已忽略'};

let lastInspect = null;

function toast(text, kind) {
  $('msg').innerHTML = text ? `<div class="alert ${kind || 'warn'}">${esc(text)}</div>` : '';
}
function clearToast() { $('msg').innerHTML = ''; }

async function api(path, payload) {
  const res = await fetch(path, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload || {}),
  });
  const data = await res.json().catch(() => ({ok: false, error: '服务端返回了非 JSON 内容'}));
  if (!res.ok || data.ok === false) throw new Error(data.error || ('HTTP ' + res.status));
  return data;
}

function headerVal(id) {
  const v = $(id).value;
  return v === '' ? null : v === '1';
}

function sourcePayload(prefix) {
  return {
    path: $(prefix + 'Path').value.trim(),
    encoding: $(prefix + 'Encoding').value.trim(),
    delimiter: $(prefix + 'Delimiter').value.trim(),
    header: headerVal(prefix + 'Header'),
    sheet: $(prefix + 'Sheet').value.trim(),
  };
}

function buildPayload() {
  return {
    before: sourcePayload('before'),
    after: sourcePayload('after'),
    keys: $('keys').value,
    ignoreCase: $('ignoreCase').checked,
    outDir: $('outDirInput').value.trim(),
    previewRows: 20000,
  };
}

/* ---------- 目录浏览 ---------- */
let pickTarget = null;
let pickDirOnly = false;
async function pick(which, dirOnly) {
  pickTarget = which;
  pickDirOnly = !!dirOnly;
  const cur = $(which).value.trim();
  const start = cur ? (dirOnly ? cur : cur.replace(/[\\/][^\\/]*$/, '')) : '';
  await openDir(start);
  $('modal').classList.add('on');
}
async function openDir(path) {
  try {
    const data = await api('/api/browse', {path});
    $('modalPath').value = data.path;
    $('modal').dataset.cur = data.path;
    $('modal').dataset.parent = data.parent || '';
    $('modalList').innerHTML = data.entries.map(e =>
      `<div onclick="enterEntry('${esc(e.name).replace(/'/g, "\\'")}', ${e.isDir}, ${e.selectable})">
         <span>${e.isDir ? '📁' : '📄'}</span>
         <span style="${e.selectable ? '' : 'opacity:.45'}">${esc(e.name)}</span>
         <span class="muted" style="margin-left:auto">${e.isDir ? '' : (e.size/1024).toFixed(0) + ' KB'}</span>
       </div>`).join('') || '<div class="muted" style="padding:10px 18px">（空目录）</div>';
  } catch (e) { toast(e.message, 'err'); }
}
function enterEntry(name, isDir, selectable) {
  const cur = $('modal').dataset.cur;
  const sep = cur.includes('\\') ? '\\' : '/';
  const full = cur.replace(/[\\/]$/, '') + sep + name;
  if (isDir) { openDir(full); return; }
  if (pickDirOnly) { toast('这里要选目录，请进入目录后点「选择当前目录」'); return; }
  if (!selectable) { toast('这个文件类型不支持，可选 CSV / Excel / Parquet / JSON'); return; }
  chooseFile(full);
}
function chooseFile(full) {
  $(pickTarget).value = full;
  closeModal();
}
function modalGo() { openDir($('modalPath').value.trim()); }
function modalUp() {
  const p = $('modal').dataset.parent;
  if (p) openDir(p);
}
function modalChoose() { chooseFile($('modal').dataset.cur); }
function closeModal() { $('modal').classList.remove('on'); }

/* ---------- 第一步：分析 ---------- */
async function doInspect() {
  clearToast();
  $('btnInspect').disabled = true;
  $('btnInspect').textContent = '分析中…';
  try {
    const data = await api('/api/inspect', buildPayload());
    lastInspect = data;
    renderInspect(data);
    $('cardFields').classList.remove('hide');
    $('cardFields').scrollIntoView({behavior: 'smooth', block: 'start'});
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    $('btnInspect').disabled = false;
    $('btnInspect').textContent = '分析字段';
  }
}

function renderInspect(d) {
  $('previewHint').textContent =
    `前 ${nf(d.before.rows)} 行 / 后 ${nf(d.after.rows)} 行（抽样预览，正式对比按全量）`;
  $('keys').value = (d.keys || []).join(',');
  $('keyStrategy').textContent = d.keyStrategy ? '· ' + d.keyStrategy : '';

  const warns = [];
  if (d.warnings && d.warnings.length) warns.push(...d.warnings);
  if (d.onlyBefore.length) warns.push('后数据集缺少字段：' + d.onlyBefore.join('、'));
  if (d.onlyAfter.length) warns.push('后数据集新增字段：' + d.onlyAfter.join('、'));
  $('inspectWarn').innerHTML = warns.length
    ? `<div class="alert warn">${esc(warns.join('\n'))}</div>` : '';

  $('fieldRows').innerHTML = d.columns.map(c => {
    const cls = c.mode === 'key' ? 'key' : (c.mode.startsWith('only') ? 'only' : (c.type || 'string'));
    return `<tr>
      <td><b>${esc(c.name)}</b>${c.afterName && c.afterName !== c.name ? `<br><span class="muted">→ ${esc(c.afterName)}</span>` : ''}</td>
      <td><span class="pill ${cls}">${esc(TYPES[c.type] || (c.mode.startsWith('only') ? '—' : c.type))}</span></td>
      <td>${esc(ROLES[c.mode] || c.mode)}</td>
      <td style="text-align:right">${c.nullRate === null ? '-' : pf(c.nullRate)}</td>
      <td style="text-align:right">${nf(c.distinct)}</td>
      <td class="muted">${esc(c.suggest)}</td>
    </tr>`;
  }).join('');
}

/* ---------- 第二步：执行 ---------- */
let timer = null;
async function doRun() {
  clearToast();
  $('btnRun').disabled = true;
  $('cardRun').classList.remove('hide');
  $('resultBox').classList.add('hide');
  $('runError').innerHTML = '';
  $('progressBox').classList.remove('hide');
  $('runHead').classList.remove('done');
  $('cardRun').scrollIntoView({behavior: 'smooth', block: 'start'});
  try {
    await api('/api/run', buildPayload());
    startPolling();
  } catch (e) {
    toast(e.message, 'err');
    $('btnRun').disabled = false;
  }
}

function startPolling() {
  if (timer) clearInterval(timer);
  timer = setInterval(poll, 700);
  poll();
}

async function poll() {
  let s;
  try {
    const res = await fetch('/api/status');
    s = await res.json();
  } catch (e) { return; }

  $('barFill').style.width = s.percent + '%';
  $('pct').textContent = s.percent + '%';
  $('phase').textContent = s.phase || '执行中…';
  $('logBox').textContent = (s.logs || []).join('\n');
  $('logBox').scrollTop = $('logBox').scrollHeight;

  if (s.state === 'done') {
    clearInterval(timer); timer = null;
    $('btnRun').disabled = false;
    $('runHead').classList.add('done');
    $('progressBox').classList.add('hide');
    renderResult(s);
  } else if (s.state === 'error') {
    clearInterval(timer); timer = null;
    $('btnRun').disabled = false;
    $('runError').innerHTML = `<div class="alert err">执行失败：${esc(s.error)}</div>`;
  }
}

function renderResult(s) {
  const t = s.stats || {};
  const kpis = [
    ['匹配成功', nf(t.matchedPairs), ''],
    ['完全一致', nf(t.sameRows), 'ok'],
    ['存在差异', nf(t.changedRows), t.changedRows ? 'warn' : 'ok'],
    ['仅前数据集', nf(t.onlyInBefore), t.onlyInBefore ? 'warn' : 'ok'],
    ['仅后数据集', nf(t.onlyInAfter), t.onlyInAfter ? 'warn' : 'ok'],
    ['实质差异', nf(t.severeCellDiffs), t.severeCellDiffs ? 'bad' : 'ok'],
    ['仅格式差异', nf(t.formatOnlyCells), ''],
    ['主键重复组数', `${nf(t.dupKeyBefore)} / ${nf(t.dupKeyAfter)}`,
      (t.dupKeyBefore + t.dupKeyAfter) ? 'bad' : 'ok'],
  ];
  $('kpis').innerHTML = kpis.map(([k, v, c]) =>
    `<div class="kpi ${c}"><div class="k">${esc(k)}</div><div class="v">${v}</div></div>`).join('');

  $('files').innerHTML = (s.reports || []).map(r =>
    `<a class="file" href="${esc(r.url)}" target="_blank">
       <span>${r.name.endsWith('.html') ? '📊' : (r.name.endsWith('.xlsx') ? '📗' : r.name.endsWith('.csv') ? '📄' : '📝')}</span>
       <span>${esc(r.name)}</span>
       <span class="sz">${(r.size/1024).toFixed(0)} KB</span>
     </a>`).join('');

  const verdict = t.consistent
    ? '<div class="alert ok">未发现实质性问题</div>'
    : `<div class="alert warn">发现 ${nf(t.severeCellDiffs)} 处实质差异（「仅格式差异」如补零、千分位、日期写法不算）</div>`;
  $('runError').innerHTML = verdict;
  $('outDir').textContent = '输出目录：' + (s.outDir || '');
  $('resultBox').classList.remove('hide');
}

/* ---------- 导出配置 ---------- */
async function showConfig() {
  try {
    const data = await api('/api/export-config', buildPayload());
    const w = window.open('', '_blank');
    w.document.write('<title>datacompare 配置</title>' +
      '<style>body{margin:0;background:#f6f7fb;font:14px system-ui;padding:20px}' +
      'pre{background:#0f172a;color:#e2e8f0;padding:16px;border-radius:10px;overflow:auto;font-size:12.5px}</style>' +
      '<p>把下面内容存成 <code>my.yaml</code>，之后就能用命令行跑定时任务：</p>' +
      '<pre>' + esc(data.yaml) + '</pre>' +
      '<p><code>datacompare compare -c my.yaml --fail-on-diff</code></p>');
  } catch (e) { toast(e.message, 'err'); }
}

/* 回车即分析 */
['beforePath', 'afterPath'].forEach(id => $(id).addEventListener('keydown', e => {
  if (e.key === 'Enter') doInspect();
}));
</script>
</body>
</html>
"""
