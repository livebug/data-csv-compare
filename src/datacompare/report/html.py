"""HTML 报告：单文件、内嵌数据、可筛选搜索、逐字符高亮差异。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict

from ..config import Config
from ..models import (
    ANOMALY_CATEGORY_LABELS,
    LEVEL_LABELS,
    ROW_ONLY_IN_AFTER,
    ROW_ONLY_IN_BEFORE,
    STATUS_COLORS,
    STATUS_LABELS,
    TYPE_LABELS,
)
from . import data as D


def build_payload(result, cfg: Config) -> Dict[str, Any]:
    stats = result.stats
    keys = D.key_columns(stats)

    diffs = D.diff_rows(
        result.con, cfg, stats, limit=cfg.report.html_max_rows, order_by_value=False
    )
    diff_rows = []
    for d in diffs:
        row = {k: d.get(k) for k in keys}
        row.update(
            {
                "column": d["column_name"],
                "ctype": d["column_type"],
                "status": d["status"],
                "before": d["before_raw"],
                "after": d["after_raw"],
                "beforeNorm": d["before_norm"],
                "afterNorm": d["after_norm"],
                "absDiff": d["abs_diff"],
                "relDiff": d["rel_diff"],
                "bRow": d["__b_row"],
                "aRow": d["__a_row"],
            }
        )
        diff_rows.append(row)

    only_before = _only_payload(result, cfg, stats, ROW_ONLY_IN_BEFORE)
    only_after = _only_payload(result, cfg, stats, ROW_ONLY_IN_AFTER)

    total_diffs = stats.cell_diffs
    return {
        "tool": "datacompare",
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "title": cfg.report.title,
        "keys": keys,
        "keyStrategy": stats.key_strategy,
        "before": {
            "path": stats.before_path,
            "rows": stats.before_rows,
            "cols": len(stats.before_columns),
        },
        "after": {
            "path": stats.after_path,
            "rows": stats.after_rows,
            "cols": len(stats.after_columns),
        },
        "stats": {
            "matchedPairs": stats.matched_pairs,
            "sameRows": stats.same_rows,
            "changedRows": stats.changed_rows,
            "onlyInBefore": stats.only_in_before,
            "onlyInAfter": stats.only_in_after,
            "rowDelta": stats.row_delta,
            "rowDeltaRate": stats.row_delta_rate,
            "cellDiffs": stats.cell_diffs,
            "severeCellDiffs": stats.severe_cell_diffs,
            "formatOnlyCells": stats.format_only_cells,
            "comparedColumns": stats.compared_columns,
            "totalColumns": stats.total_columns,
            "dupKeyBefore": stats.dup_key_before,
            "dupKeyAfter": stats.dup_key_after,
            "consistent": stats.consistent,
        },
        "columns": [
            {
                "name": c.name,
                "afterName": c.after_name if c.after_name != c.name else "",
                "mode": c.mode,
                "ctype": c.ctype,
                "matchedRows": c.matched_rows,
                "equal": c.equal,
                "formatOnly": c.format_only,
                "valueDiff": c.value_diff,
                "nullMismatch": c.null_mismatch,
                "typeMismatch": c.type_mismatch,
                "diffTotal": c.diff_total,
                "severeTotal": c.severe_total,
                "severeRate": c.severe_rate,
                "formatRate": (c.format_only / c.matched_rows) if c.matched_rows else 0.0,
                "nullBefore": c.null_rate_before,
                "nullAfter": c.null_rate_after,
                "distinctBefore": c.distinct_before,
                "distinctAfter": c.distinct_after,
                "note": c.note,
            }
            for c in result.columns
        ],
        "anomalies": [
            {
                "level": a.level,
                "levelLabel": LEVEL_LABELS.get(a.level, a.level),
                "category": a.category,
                "categoryLabel": ANOMALY_CATEGORY_LABELS.get(a.category, a.category),
                "title": a.title,
                "scope": a.scope,
                "column": a.column,
                "detail": a.detail,
                "count": a.count,
            }
            for a in result.anomalies
        ],
        "schema": D.schema_rows(stats),
        "warnings": list(result.warnings),
        "diffs": diff_rows,
        "diffTruncated": max(0, total_diffs - len(diff_rows)),
        "onlyBefore": only_before,
        "onlyAfter": only_after,
        "statusLabels": STATUS_LABELS,
        "statusColors": STATUS_COLORS,
        "typeLabels": TYPE_LABELS,
        "modeLabels": {
            "compared": "参与对比",
            "key": "主键",
            "ignored": "已忽略",
            "only_before": "仅前数据集",
            "only_after": "仅后数据集",
        },
    }


def _only_payload(result, cfg: Config, stats, status: str) -> Dict[str, Any]:
    total = stats.only_in_before if status == ROW_ONLY_IN_BEFORE else stats.only_in_after
    stream = D.iter_only_in_rows(
        result.con, cfg, stats, status, cfg.report.only_in_max_rows
    )
    try:
        cols = next(stream)
    except StopIteration:
        return {"columns": [], "rows": [], "total": total, "truncated": 0}
    rows = [list(r) for r in stream]
    return {
        "columns": cols,
        "rows": rows,
        "total": total,
        "truncated": max(0, total - len(rows)),
    }


def write_html(result, cfg: Config, path: str) -> str:
    payload = build_payload(result, cfg)
    text = json.dumps(payload, ensure_ascii=False, default=str)
    text = text.replace("</", "<\\/")
    html = _TEMPLATE.replace("/*__DATA__*/", text)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return path


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>数据对比报告</title>
<style>
  :root{
    --bg:#f6f7fb; --panel:#ffffff; --line:#e5e7eb; --text:#111827; --muted:#6b7280;
    --brand:#2563eb; --ok:#16a34a; --warn:#d97706; --bad:#dc2626; --purple:#9333ea; --cyan:#0891b2;
    --shadow:0 1px 2px rgba(16,24,40,.06), 0 4px 16px rgba(16,24,40,.06);
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
    font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;}
  header{background:linear-gradient(135deg,#1e293b,#0f172a);color:#fff;padding:26px 32px}
  header h1{margin:0 0 6px;font-size:22px;font-weight:650;letter-spacing:.2px}
  header .sub{color:#94a3b8;font-size:13px}
  header code{background:rgba(255,255,255,.12);padding:1px 7px;border-radius:5px;font-size:12.5px}
  .wrap{padding:22px 32px 60px;max-width:1600px;margin:0 auto}
  .verdict{display:flex;align-items:center;gap:12px;padding:14px 18px;border-radius:12px;
    font-weight:600;font-size:15px;margin-bottom:18px;box-shadow:var(--shadow)}
  .verdict.ok{background:#ecfdf5;color:#065f46;border:1px solid #a7f3d0}
  .verdict.bad{background:#fef2f2;color:#991b1b;border:1px solid #fecaca}
  .verdict .dot{width:10px;height:10px;border-radius:50%;background:currentColor;flex:none}
  .cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px;margin-bottom:20px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;box-shadow:var(--shadow)}
  .card .k{color:var(--muted);font-size:12.5px;margin-bottom:4px}
  .card .v{font-size:22px;font-weight:650;font-variant-numeric:tabular-nums}
  .card .v small{font-size:12px;font-weight:500;color:var(--muted);margin-left:6px}
  .card.ok .v{color:var(--ok)} .card.warn .v{color:var(--warn)} .card.bad .v{color:var(--bad)}
  .tabs{display:flex;gap:6px;border-bottom:1px solid var(--line);margin-bottom:16px;flex-wrap:wrap}
  .tab{padding:9px 16px;border:none;background:none;cursor:pointer;font-size:14px;color:var(--muted);
    border-bottom:2px solid transparent;font-weight:500;border-radius:8px 8px 0 0}
  .tab:hover{color:var(--text);background:#f3f4f6}
  .tab.active{color:var(--brand);border-bottom-color:var(--brand);font-weight:600}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow);overflow:hidden}
  .panel + .panel{margin-top:16px}
  .panel h2{margin:0;padding:13px 18px;font-size:14.5px;border-bottom:1px solid var(--line);font-weight:600;
    display:flex;align-items:center;gap:8px}
  .panel h2 .hint{font-weight:400;color:var(--muted);font-size:12.5px}
  .toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:12px 18px;border-bottom:1px solid var(--line);background:#fbfbfd}
  input[type=search],select{padding:7px 11px;border:1px solid var(--line);border-radius:8px;font-size:13.5px;
    background:#fff;color:var(--text);outline:none;min-width:120px}
  input[type=search]:focus,select:focus{border-color:var(--brand);box-shadow:0 0 0 3px rgba(37,99,235,.12)}
  .chip{padding:5px 11px;border-radius:999px;border:1px solid var(--line);background:#fff;cursor:pointer;
    font-size:12.5px;user-select:none;display:inline-flex;align-items:center;gap:6px;font-weight:500}
  .chip .sw{width:8px;height:8px;border-radius:50%}
  .chip.off{opacity:.42}
  .chip.on{border-color:currentColor}
  .btn{padding:7px 13px;border-radius:8px;border:1px solid var(--line);background:#fff;cursor:pointer;font-size:13.5px}
  .btn:hover{background:#f3f4f6}
  .tw{overflow:auto;max-height:70vh}
  table{border-collapse:separate;border-spacing:0;width:100%;font-size:13px}
  thead th{position:sticky;top:0;background:#f9fafb;z-index:2;text-align:left;padding:9px 12px;
    border-bottom:1px solid var(--line);font-weight:600;color:#374151;white-space:nowrap;font-size:12.5px}
  thead th.sortable{cursor:pointer}
  thead th.sortable:hover{background:#f3f4f6}
  tbody td{padding:7px 12px;border-bottom:1px solid #f1f3f5;vertical-align:top}
  tbody tr:hover td{background:#f8fafc}
  td.num{text-align:right;font-variant-numeric:tabular-nums}
  td.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px;word-break:break-all;max-width:340px}
  .pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11.5px;font-weight:600;white-space:nowrap;color:#fff}
  .pill.soft{background:#eef2ff;color:#3730a3}
  .null{color:#9ca3af;font-style:italic}
  .del{background:#fee2e2;color:#991b1b;border-radius:3px;padding:0 1px}
  .ins{background:#dcfce7;color:#166534;border-radius:3px;padding:0 1px}
  .anom{display:flex;gap:12px;padding:12px 18px;border-bottom:1px solid #f1f3f5}
  .anom:last-child{border-bottom:none}
  .anom .lv{flex:none;font-size:11.5px;font-weight:700;padding:2px 9px;border-radius:999px;height:fit-content}
  .anom .lv.error{background:#fee2e2;color:#991b1b}
  .anom .lv.warn{background:#fef3c7;color:#92400e}
  .anom .lv.info{background:#e0f2fe;color:#075985}
  .anom .tt{font-weight:600;margin-bottom:2px}
  .anom .dd{color:var(--muted);font-size:12.5px;word-break:break-all}
  .empty{padding:26px;text-align:center;color:var(--muted)}
  .mini{padding:10px 18px;color:var(--muted);font-size:12.5px;border-top:1px solid var(--line);background:#fbfbfd}
  .warnbox{margin:0 0 16px;padding:12px 16px;border-radius:10px;background:#fffbeb;border:1px solid #fde68a;color:#92400e;font-size:13px}
  .warnbox ul{margin:6px 0 0;padding-left:20px}
  .hide{display:none !important}
  .lgd{display:flex;gap:14px;flex-wrap:wrap;padding:12px 18px;color:var(--muted);font-size:12.5px;border-bottom:1px solid var(--line)}
  .lgd i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px;vertical-align:middle}
</style>
</head>
<body>
<header>
  <h1 id="title"></h1>
  <div class="sub" id="subtitle"></div>
</header>
<div class="wrap">
  <div id="verdict" class="verdict"></div>
  <div id="warnings"></div>
  <div class="cards" id="cards"></div>

  <div class="tabs">
    <button class="tab active" data-tab="overview">概览</button>
    <button class="tab" data-tab="columns">字段对比</button>
    <button class="tab" data-tab="diffs">差异明细</button>
    <button class="tab" data-tab="anomalies">异常数据</button>
    <button class="tab" data-tab="only">单边数据</button>
  </div>

  <section id="tab-overview">
    <div class="panel">
      <h2>字段增减 <span class="hint">前有后无 / 后有前无的字段</span></h2>
      <div id="schema"></div>
    </div>
    <div class="panel">
      <h2>字段差异排行 <span class="hint">按实质差异从多到少</span></h2>
      <div class="tw"><table id="topcols"></table></div>
    </div>
    <div class="panel">
      <h2>仅格式差异字段 <span class="hint">补零 / 千分位 / 大小写 / 空白，语义一致</span></h2>
      <div id="fmtcols"></div>
    </div>
  </section>

  <section id="tab-columns" class="hide">
    <div class="panel">
      <h2>字段对比结果 <span class="hint">点击表头可排序</span></h2>
      <div class="toolbar">
        <input type="search" id="colSearch" placeholder="搜索字段名或说明…">
        <select id="colMode">
          <option value="">全部处理方式</option>
        </select>
      </div>
      <div class="tw"><table id="colTable"></table></div>
    </div>
  </section>

  <section id="tab-diffs" class="hide">
    <div class="panel">
      <h2>差异明细 <span class="hint" id="diffHint"></span></h2>
      <div class="toolbar">
        <input type="search" id="search" placeholder="搜索主键 / 前后值…">
        <select id="colFilter"><option value="">全部字段</option></select>
        <span id="chips"></span>
        <button class="btn" id="more">加载更多</button>
        <span class="hint" id="count"></span>
      </div>
      <div class="lgd">
        <span><i style="background:#fee2e2"></i>前数据集有、后数据集无/不同</span>
        <span><i style="background:#dcfce7"></i>后数据集有、前数据集无/不同</span>
      </div>
      <div class="tw"><table id="diffTable"></table></div>
      <div class="mini" id="diffFoot"></div>
    </div>
  </section>

  <section id="tab-anomalies" class="hide">
    <div class="panel">
      <h2>异常数据 <span class="hint">需要人工确认的问题点</span></h2>
      <div id="anomalies"></div>
    </div>
  </section>

  <section id="tab-only" class="hide">
    <div class="panel">
      <h2>仅前数据集存在 <span id="obHint" class="hint"></span></h2>
      <div class="tw"><table id="obTable"></table></div>
    </div>
    <div class="panel">
      <h2>仅后数据集存在 <span id="oaHint" class="hint"></span></h2>
      <div class="tw"><table id="oaTable"></table></div>
    </div>
  </section>
</div>

<script>
const DATA = /*__DATA__*/;

const $ = (s) => document.querySelector(s);
const esc = (v) => (v === null || v === undefined) ? '' :
  String(v).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmtInt = (n) => (n === null || n === undefined) ? '-' : Number(n).toLocaleString('zh-CN');
const fmtPct = (n) => (n === null || n === undefined) ? '-' : (Number(n) * 100).toFixed(2) + '%';
const fmtNum = (n) => (n === null || n === undefined) ? '' :
  (Math.abs(n) >= 1000 ? Number(n).toLocaleString('zh-CN', {maximumFractionDigits: 6}) : Number(n).toPrecision(8).replace(/\.?0+$/, ''));

function val(v){
  if (v === null || v === undefined || v === '') return '<span class="null">&lt;空&gt;</span>';
  if (v === '<NULL>') return '<span class="null">&lt;NULL&gt;</span>';
  return esc(v);
}

/* 字符串级差异高亮：公共前缀/后缀之外的中间部分标红/标绿 */
function highlight(a, b){
  a = a === null || a === undefined ? '' : String(a);
  b = b === null || b === undefined ? '' : String(b);
  if (!a || !b || a === '<NULL>' || b === '<NULL>') return [val(a), val(b)];
  let p = 0;
  const max = Math.min(a.length, b.length);
  while (p < max && a[p] === b[p]) p++;
  let s = 0;
  while (s < max - p && a[a.length-1-s] === b[b.length-1-s]) s++;
  const cut = (str) => [
    str.slice(0, p),
    str.slice(p, str.length - s),
    s ? str.slice(str.length - s) : ''
  ];
  const [a1,a2,a3] = cut(a), [b1,b2,b3] = cut(b);
  const mid = (x, cls) => x === '' ? '<span class="null">&lt;空&gt;</span>' : `<span class="${cls}">${esc(x)}</span>`;
  return [esc(a1) + mid(a2,'del') + esc(a3), esc(b1) + mid(b2,'ins') + esc(b3)];
}

/* ---------- 头部 ---------- */
$('#title').textContent = DATA.title;
$('#subtitle').innerHTML =
  `生成时间 ${esc(DATA.generatedAt)} ｜ 前：<code>${esc(DATA.before.path)}</code> ${fmtInt(DATA.before.rows)} 行 × ${DATA.before.cols} 列` +
  ` ｜ 后：<code>${esc(DATA.after.path)}</code> ${fmtInt(DATA.after.rows)} 行 × ${DATA.after.cols} 列` +
  ` ｜ 行匹配：<code>${esc(DATA.keys.length ? DATA.keys.join(', ') : DATA.keyStrategy)}</code>`;

const s = DATA.stats;
const vd = $('#verdict');
vd.className = 'verdict ' + (s.consistent ? 'ok' : 'bad');
vd.innerHTML = `<span class="dot"></span>` + (s.consistent
  ? '未发现实质性问题：所有匹配到的行字段值一致，且没有主键异常'
  : `发现 ${fmtInt(s.severeCellDiffs)} 处实质差异${(s.onlyInBefore+s.onlyInAfter)?`，${fmtInt(s.onlyInBefore+s.onlyInAfter)} 行无法匹配`:''}`);

if (DATA.warnings.length){
  $('#warnings').innerHTML = `<div class="warnbox"><b>提示</b><ul>` +
    DATA.warnings.map(w => `<li>${esc(w)}</li>`).join('') + `</ul></div>`;
}

const cards = [
  ['前数据集行数', fmtInt(s.matchedPairs + s.onlyInBefore), ''],
  ['后数据集行数', fmtInt(s.matchedPairs + s.onlyInAfter), ''],
  ['匹配成功', fmtInt(s.matchedPairs), ''],
  ['完全一致', fmtInt(s.sameRows), 'ok'],
  ['存在差异', fmtInt(s.changedRows), s.changedRows ? 'warn' : 'ok'],
  ['仅前数据集', fmtInt(s.onlyInBefore), s.onlyInBefore ? 'warn' : 'ok'],
  ['仅后数据集', fmtInt(s.onlyInAfter), s.onlyInAfter ? 'warn' : 'ok'],
  ['单元格差异', fmtInt(s.cellDiffs), ''],
  ['实质差异', fmtInt(s.severeCellDiffs), s.severeCellDiffs ? 'bad' : 'ok'],
  ['仅格式差异', fmtInt(s.formatOnlyCells), ''],
  ['参与对比字段', fmtInt(s.comparedColumns), ''],
  ['主键重复组数', `${fmtInt(s.dupKeyBefore)} / ${fmtInt(s.dupKeyAfter)}`, (s.dupKeyBefore+s.dupKeyAfter)?'bad':'ok'],
];
$('#cards').innerHTML = cards.map(([k,v,c]) =>
  `<div class="card ${c}"><div class="k">${esc(k)}</div><div class="v">${v}</div></div>`).join('');

/* ---------- Tab ---------- */
document.querySelectorAll('.tab').forEach(t => t.onclick = () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  t.classList.add('active');
  ['overview','columns','diffs','anomalies','only'].forEach(n =>
    $('#tab-'+n).classList.toggle('hide', n !== t.dataset.tab));
});

/* ---------- 字段增减 ---------- */
if (!DATA.schema.length){
  $('#schema').innerHTML = '<div class="empty">两侧字段完全一致</div>';
} else {
  $('#schema').innerHTML = '<div class="tw"><table><thead><tr><th>列名</th><th>状态</th></tr></thead><tbody>' +
    DATA.schema.map(r => `<tr><td class="mono">${esc(r['列名'])}</td><td>${esc(r['状态'])}</td></tr>`).join('') +
    '</tbody></table></div>';
}

/* ---------- 字段排行 ---------- */
const topCols = DATA.columns.filter(c => c.mode === 'compared' && c.severeTotal > 0)
  .sort((a,b) => b.severeTotal - a.severeTotal).slice(0, 20);
$('#topcols').innerHTML = '<thead><tr><th>字段</th><th>类型</th><th class="num">值不同</th>' +
  '<th class="num">空值不一致</th><th class="num">类型异常</th><th class="num">实质差异率</th><th class="num">空值率变化</th></tr></thead><tbody>' +
  (topCols.length ? topCols.map(c => `<tr>
    <td class="mono">${esc(c.name)}</td>
    <td>${esc(DATA.typeLabels[c.ctype]||c.ctype)}</td>
    <td class="num">${fmtInt(c.valueDiff)}</td>
    <td class="num">${fmtInt(c.nullMismatch)}</td>
    <td class="num">${fmtInt(c.typeMismatch)}</td>
    <td class="num">${fmtPct(c.severeRate)}</td>
    <td class="num">${fmtPct(c.nullBefore)} → ${fmtPct(c.nullAfter)}</td></tr>`).join('')
   : '<tr><td colspan="7" class="empty">没有实质差异</td></tr>') + '</tbody>';

const fmtCols = DATA.columns.filter(c => c.mode === 'compared' && c.formatOnly > 0)
  .sort((a,b) => b.formatOnly - a.formatOnly).slice(0, 20);
$('#fmtcols').innerHTML = fmtCols.length
  ? '<div class="tw"><table><thead><tr><th>字段</th><th>类型</th><th class="num">仅格式差异</th><th class="num">占比</th><th>说明</th></tr></thead><tbody>' +
    fmtCols.map(c => `<tr><td class="mono">${esc(c.name)}</td><td>${esc(DATA.typeLabels[c.ctype]||c.ctype)}</td>` +
      `<td class="num">${fmtInt(c.formatOnly)}</td><td class="num">${fmtPct(c.formatRate)}</td>` +
      `<td>${esc(c.note)}</td></tr>`).join('') + '</tbody></table></div>'
  : '<div class="empty">没有「仅格式差异」的单元格</div>';

/* ---------- 字段表 ---------- */
const colHeaders = [
  ['name','字段',0],['mode','处理方式',0],['ctype','类型',0],
  ['matchedRows','对比行数',1],['equal','一致',1],['formatOnly','仅格式差异',1],
  ['valueDiff','值不同',1],['nullMismatch','空值不一致',1],['typeMismatch','类型异常',1],
  ['severeRate','实质差异率',1],['nullBefore','前空值率',1],['nullAfter','后空值率',1],
  ['distinctBefore','前取值数',1],['distinctAfter','后取值数',1],['note','说明',0]
];
let colSort = {key:'severeTotal', desc:true};
const modes = [...new Set(DATA.columns.map(c => c.mode))];
modes.forEach(m => {
  const o = document.createElement('option'); o.value = m;
  o.textContent = DATA.modeLabels[m] || m; $('#colMode').appendChild(o);
});

function renderCols(){
  const q = $('#colSearch').value.trim().toLowerCase();
  const mode = $('#colMode').value;
  let rows = DATA.columns.filter(c =>
    (!mode || c.mode === mode) &&
    (!q || (c.name + ' ' + (c.afterName||'') + ' ' + c.note).toLowerCase().includes(q)));
  rows.sort((a,b) => {
    const k = colSort.key, d = colSort.desc ? -1 : 1;
    const x = a[k], y = b[k];
    if (typeof x === 'number' || typeof y === 'number') return (x - y) * d;
    return String(x).localeCompare(String(y), 'zh') * d;
  });
  $('#colTable').innerHTML = '<thead><tr>' + colHeaders.map(([k,label,num]) =>
    `<th class="sortable ${num?'num':''}" data-k="${k}">${esc(label)}${colSort.key===k?(colSort.desc?' ▾':' ▴'):''}</th>`).join('') +
    '</tr></thead><tbody>' + rows.map(c => `<tr>
      <td class="mono">${esc(c.name)}${c.afterName?`<br><span class="null">→ ${esc(c.afterName)}</span>`:''}</td>
      <td>${esc(DATA.modeLabels[c.mode]||c.mode)}</td>
      <td>${esc(c.ctype ? (DATA.typeLabels[c.ctype]||c.ctype) : '')}</td>
      <td class="num">${c.mode==='compared'?fmtInt(c.matchedRows):''}</td>
      <td class="num">${c.mode==='compared'?fmtInt(c.equal):''}</td>
      <td class="num">${c.formatOnly?fmtInt(c.formatOnly):''}</td>
      <td class="num" style="${c.valueDiff?'color:var(--bad);font-weight:600':''}">${c.valueDiff?fmtInt(c.valueDiff):''}</td>
      <td class="num" style="${c.nullMismatch?'color:var(--purple);font-weight:600':''}">${c.nullMismatch?fmtInt(c.nullMismatch):''}</td>
      <td class="num" style="${c.typeMismatch?'color:var(--cyan);font-weight:600':''}">${c.typeMismatch?fmtInt(c.typeMismatch):''}</td>
      <td class="num">${c.mode==='compared'?fmtPct(c.severeRate):''}</td>
      <td class="num">${c.mode==='compared'?fmtPct(c.nullBefore):''}</td>
      <td class="num">${c.mode==='compared'?fmtPct(c.nullAfter):''}</td>
      <td class="num">${fmtInt(c.distinctBefore)}</td>
      <td class="num">${fmtInt(c.distinctAfter)}</td>
      <td>${esc(c.note)}</td></tr>`).join('') + '</tbody>';
  document.querySelectorAll('#colTable th.sortable').forEach(th => th.onclick = () => {
    const k = th.dataset.k;
    if (colSort.key === k) colSort.desc = !colSort.desc; else colSort = {key:k, desc:true};
    renderCols();
  });
}
$('#colSearch').oninput = renderCols;
$('#colMode').onchange = renderCols;
renderCols();

/* ---------- 差异明细 ---------- */
const active = new Set(DATA.diffs.map(d => d.status));
const chipDefs = Object.keys(DATA.statusLabels).map(k => [k, DATA.statusLabels[k], DATA.statusColors[k]]);
$('#chips').innerHTML = chipDefs.map(([k,label,color]) =>
  `<span class="chip on" data-s="${k}" style="color:${color}"><span class="sw" style="background:${color}"></span>${esc(label)}</span>`).join(' ');
let colFilterInit = false;
function initDiffUI(){
  const cols = [...new Set(DATA.diffs.map(d => d.column))];
  cols.forEach(c => { const o = document.createElement('option'); o.value=c; o.textContent=c; $('#colFilter').appendChild(o); });
  document.querySelectorAll('#chips .chip').forEach(ch => ch.onclick = () => {
    const k = ch.dataset.s;
    if (active.has(k)) active.delete(k); else active.add(k);
    ch.classList.toggle('on', active.has(k)); ch.classList.toggle('off', !active.has(k));
    page = 1; renderDiffs();
  });
  colFilterInit = true;
}
const PAGE = 300;
let page = 1;
function diffKeys(){ return DATA.keys.length ? DATA.keys : ['行号']; }

function filtered(){
  const q = $('#search').value.trim().toLowerCase();
  const col = $('#colFilter').value;
  return DATA.diffs.filter(d => {
    if (!active.has(d.status)) return false;
    if (col && d.column !== col) return false;
    if (q){
      const hay = (DATA.keys.map(k => d[k]).join(' ') + ' ' + d.column + ' ' + (d.before||'') + ' ' + (d.after||'')).toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}
function renderDiffs(){
  const keys = diffKeys();
  const rows = filtered();
  const shown = rows.slice(0, page * PAGE);
  const head = '<thead><tr>' + keys.map(k => `<th>${esc(k)}</th>`).join('') +
    '<th>字段</th><th>状态</th><th>前数据集</th><th>后数据集</th><th class="num">差值</th></tr></thead>';
  $('#diffTable').innerHTML = head + '<tbody>' + (shown.length ? shown.map(d => {
    const [a, b] = d.status === 'FORMAT_ONLY' ? [val(d.before), val(d.after)] : highlight(d.before, d.after);
    const color = DATA.statusColors[d.status] || '#6b7280';
    let delta = '';
    if (d.absDiff !== null && d.absDiff !== undefined) delta = fmtNum(d.absDiff);
    else if (d.relDiff !== null && d.relDiff !== undefined) delta = fmtPct(d.relDiff);
    return `<tr>` + keys.map(k => `<td class="mono">${esc(d[k])}</td>`).join('') +
      `<td class="mono">${esc(d.column)}</td>` +
      `<td><span class="pill" style="background:${color}">${esc(DATA.statusLabels[d.status]||d.status)}</span></td>` +
      `<td class="mono">${a}</td><td class="mono">${b}</td>` +
      `<td class="num">${esc(delta)}</td></tr>`;
  }).join('') : `<tr><td colspan="${keys.length+5}" class="empty">没有符合条件的差异</td></tr>`) + '</tbody>';
  $('#count').textContent = `匹配 ${fmtInt(rows.length)} 条，已显示 ${fmtInt(shown.length)} 条`;
  $('#more').classList.toggle('hide', shown.length >= rows.length);
  $('#diffHint').textContent = `共 ${fmtInt(DATA.stats.cellDiffs)} 条，报告内嵌 ${fmtInt(DATA.diffs.length)} 条`;
  $('#diffFoot').innerHTML = DATA.diffTruncated > 0
    ? `另有 ${fmtInt(DATA.diffTruncated)} 条差异未内嵌到本报告，完整明细见 DuckDB 库中的 <code>v_diff</code> 视图。`
    : `完整明细见 DuckDB 库中的 <code>v_diff</code> 视图。`;
}
if (!DATA.diffs.length){
  $('#diffTable').innerHTML = '<tbody><tr><td class="empty">没有单元格差异</td></tr></tbody>';
} else {
  initDiffUI();
  $('#search').oninput = () => { page = 1; renderDiffs(); };
  $('#colFilter').onchange = () => { page = 1; renderDiffs(); };
  $('#more').onclick = () => { page += 1; renderDiffs(); };
  renderDiffs();
}

/* ---------- 异常 ---------- */
$('#anomalies').innerHTML = DATA.anomalies.length ? DATA.anomalies.map(a => `
  <div class="anom">
    <span class="lv ${esc(a.level)}">${esc(a.levelLabel)}</span>
    <div>
      <div class="tt">${esc(a.title)}</div>
      <div class="dd">${esc(a.categoryLabel)}${a.column?` ｜ 字段 ${esc(a.column)}`:''}${a.scope?` ｜ 范围 ${esc(a.scope)}`:''}</div>
      ${a.detail?`<div class="dd">${esc(a.detail)}</div>`:''}
    </div>
  </div>`).join('') : '<div class="empty">未检测到异常数据</div>';

/* ---------- 单边数据 ---------- */
function renderOnly(id, hintId, pack, label){
  if (!pack.rows.length){
    $('#'+id).innerHTML = '<tbody><tr><td class="empty">无</td></tr></tbody>';
    $('#'+hintId).textContent = '';
    return;
  }
  $('#'+hintId).textContent = pack.truncated > 0
    ? `共 ${fmtInt(pack.total)} 行，展示前 ${fmtInt(pack.rows.length)} 行`
    : `共 ${fmtInt(pack.total)} 行`;
  $('#'+id).innerHTML = '<thead><tr>' + pack.columns.map(c => `<th>${esc(c)}</th>`).join('') +
    '</tr></thead><tbody>' + pack.rows.map(r =>
      '<tr>' + r.map(v => `<td class="mono">${val(v)}</td>`).join('') + '</tr>').join('') + '</tbody>';
}
renderOnly('obTable','obHint', DATA.onlyBefore, '前');
renderOnly('oaTable','oaHint', DATA.onlyAfter, '后');
</script>
</body>
</html>
"""
