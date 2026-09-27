/* 全局时序可行性抽屉 —— 复用 app.js 的 state / overrides / el / esc / toast /
   focusEvent / shortId / fmtNum 与 compare.js 的抽屉样式。 */
"use strict";

const feasState = {
  result: null,
  shown: 0,          // 已渲染的见证行数（分批渲染，5000 事件不卡页面）
  filter: "",
};
/* 约束撤销建议（预览，不写库）：从本抽屉进入，随 ⟳ 重新检查 联动重算 */
const revState = {
  result: null,
  searched: false,   // 本次抽屉会话内是否已搜索过（搜过则重算时自动刷新）
};
const FEAS_PAGE = 200;
const feas$ = (sel) => document.querySelector(sel);

/* --------------------------------------------------------------- 开关 */

function feasOpen() {
  feas$("#feas-overlay").classList.remove("hidden");
  feasRun();
}
function feasClose() {
  feas$("#feas-overlay").classList.add("hidden");
  // 关闭抽屉即结束本次会话：重开时不自动触发撤销搜索（可能较耗时）
  revState.searched = false;
  revState.result = null;
}

/* --------------------------------------------------------------- 检查 */

async function feasRun() {
  const box = feas$("#feas-result");
  box.innerHTML = "";
  box.appendChild(el("div", "cmp-empty", "联立求解中…"));
  const res = await fetch("/api/temporal-feasibility", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    // 页面当前（含未保存）的时钟假设；筛选不参与——始终全量联立
    body: JSON.stringify({ overrides: state.overrides || {} }),
  });
  if (!res.ok) {
    let msg = await res.text();
    try { msg = JSON.parse(msg).detail || msg; } catch { /* 保留原文 */ }
    feasState.result = null;
    box.innerHTML = "";
    box.appendChild(el("div", "cmp-empty", "检查失败: " + msg));
    feas$("#rev-result").innerHTML = "";
    revState.result = null;
    return;
  }
  feasState.result = await res.json();
  feasState.shown = 0;
  feasState.filter = "";
  feasRender();
  // 撤销建议随重算联动：假设已变化，旧建议失效；本会话搜索过则自动重搜
  feas$("#rev-result").innerHTML = "";
  revState.result = null;
  if (revState.searched && !feasState.result.feasible) revRun();
}

/* --------------------------------------------------------------- 渲染 */

function feasRender() {
  const d = feasState.result;
  const box = feas$("#feas-result");
  box.innerHTML = "";
  if (!d) return;
  box.appendChild(feasVerdict(d));
  if (d.feasible) {
    box.appendChild(feasWitnessBlock(d));
  } else {
    box.appendChild(feasConflictBlock(d.witness));
  }
}

function feasStatsHtml(d) {
  const s = d.stats;
  return `
    <span class="stat">事件 <b>${s.event_count}</b></span>
    <span class="stat">参与约束 <b>${s.constraint_count}</b></span>
    ${s.skipped_constraints ? `<span class="stat warn">悬空约束已忽略 <b>${s.skipped_constraints}</b></span>` : ""}
    <span class="stat ${s.cycle_count ? "warn" : ""}">因果环 <b>${s.cycle_count}</b></span>
    <span class="stat">环内事件 <b>${s.events_in_cycles}</b></span>`;
}

function feasVerdict(d) {
  const blk = el("div", "cmp-block feas-verdict " + (d.feasible ? "ok" : "bad"));
  const dirty = Object.keys(state.overrides || {}).length > 0;
  blk.innerHTML = `
    <div class="feas-verdict-head">
      <span class="feas-verdict-icon">${d.feasible ? "✓" : "✗"}</span>
      <div>
        <div class="feas-verdict-title">${d.feasible ? "全局时序可行" : "全局时序无解"}</div>
        <div class="feas-verdict-sub">${esc(d.summary)}
          ${dirty ? '<span class="tag amb">含未保存调整</span>' : ""}</div>
      </div>
    </div>
    <div class="sens-meta">${feasStatsHtml(d)}</div>`;
  return blk;
}

/* ---- 可行：每个事件的最早可行时刻（见证），分批渲染 + 关键字过滤 ---- */

function feasWitnessBlock(d) {
  const blk = el("div", "cmp-block");
  blk.innerHTML = `<h3>见证：每个事件的最早可行时刻
    <span class="cmp-count">落在各自闭区间内且满足全部约束的一种取值；点击行在主时间线定位</span></h3>`;
  const bar = el("div", "feas-filter-bar");
  bar.innerHTML = `<input type="search" id="feas-filter" placeholder="过滤摘要 / ID / 来源…"
      value="${esc(feasState.filter)}">
    <span class="cmp-hint2" id="feas-count"></span>`;
  blk.appendChild(bar);
  const list = el("div", "feas-list");
  list.id = "feas-list";
  blk.appendChild(list);
  const more = el("button", "btn tiny ghost feas-more");
  more.id = "feas-more";
  blk.appendChild(more);
  bar.querySelector("#feas-filter").addEventListener("input", (e) => {
    feasState.filter = e.target.value;
    feasState.shown = 0;
    feasRenderRows();
  });
  more.addEventListener("click", () => feasRenderRows(true));
  // 行渲染在节点挂到 DOM 后进行
  setTimeout(feasRenderRows, 0);
  return blk;
}

function feasFilteredEvents() {
  const d = feasState.result;
  const q = feasState.filter.trim().toLowerCase();
  if (!q) return d.events;
  return d.events.filter((e) =>
    (e.summary || "").toLowerCase().includes(q)
    || e.id.toLowerCase().includes(q)
    || (e.source_id || "").toLowerCase().includes(q));
}

/* 单条见证行（可行性见证与撤销建议见证共用）：最早可行时刻 + 区间余量 */
function feasRowEl(e) {
  const row = el("div", "feas-row" + (e.in_cycle ? " in-cycle" : ""));
  row.innerHTML = `
    <span class="feas-row-time" title="最早可行时刻">${esc(e.earliest_text.slice(11, 23))}</span>
    <span class="chain-node feas-row-id" data-eid="${esc(e.id)}">${esc(shortId(e.id))}</span>
    <span class="feas-row-src">${esc(e.source_id)}</span>
    <span class="feas-row-summary">${esc(e.summary || "")}</span>
    <span class="feas-row-range" title="真实时间闭区间 [lo, hi] 与余量（hi − 最早可行时刻）">
      [${esc(e.lo_text.slice(11, 23))} ~ ${esc(e.hi_text.slice(11, 23))}] · 余量 ${fmtNum(e.slack)}s
      ${e.in_cycle ? '<span class="tag amb" title="处于因果环内：环内事件取同一时刻">环</span>' : ""}
    </span>`;
  row.querySelector(".feas-row-id").addEventListener("click", () => {
    feasClose();
    focusEvent(e.id);
  });
  return row;
}

function feasRenderRows(append) {
  const d = feasState.result;
  if (!d || !d.feasible) return;
  const list = feas$("#feas-list");
  const more = feas$("#feas-more");
  if (!list || !more) return;
  const events = feasFilteredEvents();
  if (!append) {
    list.innerHTML = "";
    feasState.shown = 0;
  }
  const end = Math.min(feasState.shown + FEAS_PAGE, events.length);
  const frag = document.createDocumentFragment();
  for (let i = feasState.shown; i < end; i++) {
    frag.appendChild(feasRowEl(events[i]));
  }
  list.appendChild(frag);
  feasState.shown = end;
  const count = feas$("#feas-count");
  if (count) count.textContent = `${events.length} / ${d.events.length} 个事件`;
  if (feasState.shown >= events.length) {
    more.classList.add("hidden");
  } else {
    more.classList.remove("hidden");
    more.textContent = `显示更多（已 ${feasState.shown} / ${events.length}）`;
  }
}

/* ---- 无解：矛盾路径（事件 ID + 约束 ID + 两端冲突界限） ---- */

function feasConflictBlock(w) {
  const blk = el("div", "cmp-block");
  blk.innerHTML = `<h3>矛盾路径
    <span class="cmp-count">下界沿路径逐段传递，最终在终点越过其区间上界；点击事件在主时间线定位</span></h3>`;

  const bounds = el("div", "feas-bounds");
  bounds.innerHTML = `
    <div class="feas-bound">
      <span class="feas-bound-label">路径起点 · 区间下界</span>
      <span class="chain-node" data-eid="${esc(w.head.event)}">${esc(shortId(w.head.event))}</span>
      <span class="feas-bound-val">t ≥ ${esc(w.head.text)}</span>
    </div>
    <div class="feas-bound bad">
      <span class="feas-bound-label">路径终点 · 区间上界</span>
      <span class="chain-node" data-eid="${esc(w.tail.event)}">${esc(shortId(w.tail.event))}</span>
      <span class="feas-bound-val">t ≤ ${esc(w.tail.text)}</span>
    </div>
    <div class="feas-bound bad">
      <span class="feas-bound-label">冲突超出</span>
      <span class="feas-bound-val">${esc(w.overshoot_text)}</span>
    </div>`;
  blk.appendChild(bounds);

  const chain = el("div", "chain feas-chain");
  w.steps.forEach((s, i) => {
    if (i === 0) chain.appendChild(feasNode(s.from));
    const hop = el("span", "feas-hop" + (s.individually_feasible ? "" : " bad"));
    const cid = s.constraint_id === null || s.constraint_id === undefined
      ? "?" : `#${s.constraint_id}`;
    hop.title = `约束 ${cid}（${s.kind}）要求 t(${s.to}) ≥ t(${s.from})；`
      + `传递到此的下界 ${fmtNum(s.running_lo)}s；`
      + (s.individually_feasible ? "该边单独可行" : "该边单独即不可能");
    hop.innerHTML = `<span class="chain-arrow ${s.individually_feasible ? "" : "bad"}">→</span>
      <span class="feas-hop-cid">${cid}${s.individually_feasible ? "" : " ⚠"}</span>`;
    chain.appendChild(hop);
    chain.appendChild(feasNode(s.to, !s.individually_feasible));
  });
  blk.appendChild(chain);

  const noteCls = w.all_edges_individually_feasible ? "ok" : "warn";
  const note = el("div", "feas-note " + noteCls);
  note.textContent = w.explanation;
  blk.appendChild(note);

  // 入口：沿矛盾路径搜索约束撤销建议（预览，不写库）
  const revBar = el("div", "rev-bar");
  const revBtn = el("button", "btn tiny primary", "🧩 搜索约束撤销建议");
  revBtn.title = "沿矛盾路径搜索需撤销的最少人工/导入约束，逐个候选用原求解器复核；"
    + "source_order 系统时序边与事件区间不可撤销；预览不写数据库";
  revBtn.addEventListener("click", revRun);
  revBar.appendChild(revBtn);
  revBar.appendChild(el("span", "cmp-hint2",
    "最多撤销 3 条 · 最多 2000 次求解 · 至多展示 5 个同样最少的方案 · 计算含未保存的时钟假设"));
  revBar.querySelector(".cmp-hint2").title =
    "搜索使用页面当前（含未保存）的时钟假设；触限时明确标为未完成，不声称最优";
  blk.appendChild(revBar);

  blk.querySelectorAll(".chain-node").forEach((n) =>
    n.addEventListener("click", () => { feasClose(); focusEvent(n.dataset.eid); }));
  return blk;
}

function feasNode(eid, bad) {
  const n = el("span", "chain-node" + (bad ? " bad" : ""));
  n.dataset.eid = eid;
  n.textContent = shortId(eid);
  n.title = "点击在主时间线定位";
  return n;
}

/* ===================== 约束撤销建议（预览，不写库） ===================== */

async function revRun() {
  const box = feas$("#rev-result");
  box.innerHTML = "";
  box.appendChild(el("div", "cmp-empty", "正在沿矛盾路径搜索撤销方案，并逐个用原求解器复核…"));
  const res = await fetch("/api/temporal-feasibility/revocations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    // 与可行性检查一致：页面当前（含未保存）的时钟假设，全量数据，无筛选
    body: JSON.stringify({ overrides: state.overrides || {} }),
  });
  if (!res.ok) {
    let msg = await res.text();
    try { msg = JSON.parse(msg).detail || msg; } catch { /* 保留原文 */ }
    revState.result = null;
    box.innerHTML = "";
    box.appendChild(el("div", "cmp-empty", "撤销建议搜索失败: " + msg));
    return;
  }
  revState.result = await res.json();
  revState.searched = true;
  revRender();
}

function revRender() {
  const d = revState.result;
  const box = feas$("#rev-result");
  box.innerHTML = "";
  if (!d) return;
  box.appendChild(revVerdict(d));
  (d.solutions || []).forEach((s, i) => box.appendChild(revSolutionCard(s, i)));
}

function revVerdict(d) {
  const blk = el("div", "cmp-block rev-verdict");
  const dirty = Object.keys(state.overrides || {}).length > 0;
  const s = d.stats || {};
  const badges = [
    '<span class="tag amb">预览 · 不写入数据库</span>',
    dirty ? '<span class="tag amb">含未保存调整</span>' : "",
    d.complete === false ? '<span class="tag conf">⚠ 搜索触限 · 未完成</span>' : "",
    d.solutions_truncated ? '<span class="tag amb">方案列表有截断</span>' : "",
  ].filter(Boolean).join("");
  const noteCls = d.complete === false ? "warn"
    : (d.needed === false || (d.solutions && d.solutions.length) ? "ok" : "warn");
  blk.innerHTML = `
    <h3>🧩 约束撤销建议 ${badges}</h3>
    <div class="feas-note ${noteCls}">${esc(d.summary)}</div>
    <div class="sens-meta rev-meta">
      <span class="stat">求解复核 <b>${s.solve_count ?? "-"}</b> / ${d.limits.max_solves} 次</span>
      <span class="stat">候选复核 <b>${s.candidates_checked ?? 0}</b></span>
      <span class="stat">可撤销约束 <b>${s.revocable_constraints ?? 0}</b></span>
      <span class="stat" title="source_order 系统时序边与事件区间不可撤销">不可撤销 <b>${s.non_revocable_constraints ?? 0}</b></span>
      ${s.skipped_constraints ? `<span class="stat warn">悬空约束已忽略 <b>${s.skipped_constraints}</b></span>` : ""}
      <span class="stat">上限：撤销 ≤${d.limits.max_removals} 条 · 展示 ≤${d.limits.max_solutions} 个方案</span>
    </div>`;
  const rerun = el("button", "btn tiny", "⟳ 重新搜索");
  rerun.title = "以当前（含未保存）的时钟假设重新搜索撤销方案";
  rerun.addEventListener("click", revRun);
  blk.appendChild(rerun);
  return blk;
}

function revSolutionCard(sol, idx) {
  const blk = el("div", "cmp-block rev-sol");
  const k = sol.constraint_ids.length;
  blk.innerHTML = `<h3>方案 ${idx + 1} · 撤销 ${k} 条约束
    <span class="cmp-count">撤销后全量联立可行（已用原求解器复核）</span></h3>`;

  const list = el("div", "rev-constraints");
  sol.constraints.forEach((c) => {
    const row = el("div", "rev-constraint");
    row.innerHTML = `
      <span class="rev-cid" title="约束 ID">#${c.id}</span>
      <span class="chain-node" data-eid="${esc(c.from)}">${esc(shortId(c.from))}</span>
      <span class="chain-arrow">→</span>
      <span class="chain-node" data-eid="${esc(c.to)}">${esc(shortId(c.to))}</span>
      <span class="rev-kind">${esc(c.kind)}</span>
      ${c.note ? `<span class="rev-note">${esc(c.note)}</span>` : ""}`;
    row.querySelectorAll(".chain-node").forEach((n) =>
      n.addEventListener("click", () => { feasClose(); focusEvent(n.dataset.eid); }));
    list.appendChild(row);
  });
  blk.appendChild(list);

  // 撤销后全量可行见证：默认折叠，展开后分批渲染（5000 事件不卡页面）
  const toggle = el("button", "btn tiny ghost",
    `▸ 撤销后全量可行见证（${sol.witness.event_count} 个事件的最早可行时刻）`);
  const wrap = el("div", "rev-witness hidden");
  const listEl = el("div", "feas-list");
  const more = el("button", "btn tiny ghost feas-more");
  wrap.appendChild(listEl);
  wrap.appendChild(more);
  let shown = 0;
  let started = false;
  const renderMore = () => {
    const end = Math.min(shown + FEAS_PAGE, sol.witness.events.length);
    const frag = document.createDocumentFragment();
    for (let i = shown; i < end; i++) frag.appendChild(feasRowEl(sol.witness.events[i]));
    listEl.appendChild(frag);
    shown = end;
    if (shown >= sol.witness.events.length) {
      more.classList.add("hidden");
    } else {
      more.classList.remove("hidden");
      more.textContent = `显示更多（已 ${shown} / ${sol.witness.events.length}）`;
    }
  };
  more.addEventListener("click", renderMore);
  toggle.addEventListener("click", () => {
    wrap.classList.toggle("hidden");
    toggle.textContent = (wrap.classList.contains("hidden") ? "▸ " : "▾ ")
      + `撤销后全量可行见证（${sol.witness.event_count} 个事件的最早可行时刻）`;
    if (!started) { started = true; renderMore(); }
  });
  blk.appendChild(toggle);
  blk.appendChild(wrap);
  return blk;
}

/* --------------------------------------------------------------- 绑定 */

feas$("#btn-feasibility").addEventListener("click", feasOpen);
feas$("#btn-feasibility-2").addEventListener("click", feasOpen);
feas$("#feas-close").addEventListener("click", feasClose);
feas$("#feas-rerun").addEventListener("click", feasRun);
feas$("#feas-overlay").addEventListener("click", (e) => {
  if (e.target.id === "feas-overlay") feasClose();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !feas$("#feas-overlay").classList.contains("hidden")) feasClose();
});
