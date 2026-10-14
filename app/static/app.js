const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const yen = (v) => (v == null ? "—" : Math.round(v).toLocaleString("ja-JP"));
const pct = (v, d = 2) => (v == null ? "—" : `${Number(v).toFixed(d)}%`);
const md = (t) => (window.marked && window.DOMPurify ? DOMPurify.sanitize(marked.parse(t || "")) : `<pre style="white-space:pre-wrap">${esc(t)}</pre>`);
const TOOL_JA = {
  list_tracked_investors: "追跡ファンド一覧を取得",
  get_investor_holdings: "ファンド別の保有を取得",
  rank_swf_conviction_stocks: "Conviction Scoreで採点",
  get_stock_dossier: "銘柄ドシエを作成",
  get_nbim_live_japan_holdings: "NBIM公開APIを照会",
  scan_new_swf_filings: "EDINETの新規報告を走査",
  build_investment_strategy: "配分を計算（ガードレール適用）",
  rationale_researcher: "投資理由をGoogle検索で調査",
  plan_reviewer: "独立エージェントが審査",
};
let universe = [];
let lastPlan = null;
let scoreGuide = [];
let timingGuide = "";
let catFilter = "sovereign";
const CAT_JA = { sovereign: "政府系", activist: "物言う株主", financing: "警告" };
const timingBadge = (t) => {
  if (!t || t.score == null) return `<span class="tbadge na">—</span>`;
  const cls = t.score >= 70 ? "buy" : t.score >= 55 ? "split" : t.score >= 40 ? "wait" : "avoid";
  return `<span class="tbadge ${cls}" title="${esc((t.reasons || []).join(" / "))}">${esc(t.label)} ${t.score}</span>`;
};
const PURPOSE_CLS = { "重要提案行為等": "act", "経営関与": "act", "純投資": "inv", "政策投資・提携": "pol", "資金調達の引受け": "fin" };
const purposeChip = (p) => (p ? `<span class="purpose ${PURPOSE_CLS[p] || ""}">${esc(p)}</span>` : "");
const trendCell = (t, heavy) => (t ? `<small class="${heavy ? "down" : "muted"}">${heavy ? "⚠ " : ""}${esc(t)}</small>` : "");
const catChips = (cats) => (cats || []).map((c) => `<span class="cat ${c}">${CAT_JA[c] || c}</span>`).join("");

/* ---------- tabs ---------- */
document.querySelectorAll(".tabs button").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("on", x === b));
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("on", t.id === `tab-${b.dataset.tab}`));
  })
);
const showTab = (name) => document.querySelector(`.tabs button[data-tab="${name}"]`).click();

/* ---------- form ---------- */
$("#horizon").addEventListener("input", (e) => ($("#hzOut").textContent = e.target.value));
$("#presets").addEventListener("click", (e) => {
  if (e.target.dataset.v) $("#amount").value = e.target.dataset.v;
});
function profile() {
  return {
    investable_jpy: Number($("#amount").value),
    risk: document.querySelector("input[name=risk]:checked").value,
    horizon_years: Number($("#horizon").value),
    use_nisa: $("#nisa").checked,
    include_activists: $("#activists").checked,
    exclude_codes: $("#exclude").value.split(/[\s,、]+/).map((s) => s.trim().toUpperCase()).filter(Boolean),
    note: $("#note").value.trim(),
  };
}

/* ---------- SSE over fetch ---------- */
async function streamAgent(body, handlers) {
  const res = await fetch("/api/agent/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch {}
    handlers.error?.({ message: msg });
    return;
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, i);
      buf = buf.slice(i + 2);
      const ev = /event: (.*)/.exec(chunk)?.[1];
      const data = /data: (.*)/s.exec(chunk)?.[1];
      if (ev && data) handlers[ev]?.(JSON.parse(data));
    }
  }
}

function traceItem(list, cls, html) {
  const li = document.createElement("li");
  li.className = cls;
  li.innerHTML = html;
  list.appendChild(li);
  list.scrollTop = list.scrollHeight;
}
function traceHandlers(list, onText) {
  return {
    start: (d) => traceItem(list, "txt", `<em>0.0s</em> 開始（model: ${esc(d.model)}）`),
    tool_call: (d) => traceItem(list, "call", `<em>${d.t}s</em> → <b>${esc(TOOL_JA[d.tool] || d.tool)}</b><br><em>${esc(JSON.stringify(d.args)).slice(0, 160)}</em>`),
    tool_result: (d) => traceItem(list, d.error ? "err" : "res", `<em>${d.t}s</em> ← ${esc(TOOL_JA[d.tool] || d.tool)} ${d.error ? `<b class="down">${esc(d.error)}</b>` : `(${d.chars.toLocaleString()}文字)`}`),
    text: (d) => onText(d),
    error: (d) => traceItem(list, "err", `エラー: ${esc(d.message)}`),
  };
}

/* ---------- plan rendering ---------- */
function renderPlan(plan, source) {
  lastPlan = plan;
  $("#planEmpty").hidden = true;
  $("#planView").hidden = false;
  const a = plan.allocation;
  $("#planMeta").textContent = `${yen(plan.inputs.investable_jpy)}円 ・ ${plan.inputs.risk_label} ・ ${plan.inputs.horizon_years}年 ・ ${source}`;
  $("#allocBar").innerHTML = [["core", a.core_pct], ["sat", a.satellite_pct], ["cash", a.cash_pct]]
    .map(([k, v]) => `<div style="width:${v}%;background:var(--${k})" title="${v}%"></div>`).join("");
  $("#allocLegend").innerHTML = `
    <span><i style="background:var(--core)"></i>コア（インデックス）${a.core_pct}%　<b class="num">${yen(a.core_index_jpy)}円</b></span>
    <span><i style="background:var(--sat)"></i>SWF追随の個別株 ${a.satellite_pct}%　<b class="num">${yen(a.satellite_stocks_jpy)}円</b></span>
    <span><i style="background:var(--cash)"></i>現金 ${a.cash_pct}%　<b class="num">${yen(a.cash_jpy)}円</b></span>`;
  const rows = plan.positions.map((p) => `<tr>
      <td class="name"><b>${esc(p.name)}</b><small>${esc(p.code)}・${esc(p.sector)}</small></td>
      <td>${p.followed_investors.map((f) => `<span class="fund">${esc(f)}</span>`).join("")}</td>
      <td class="num">${p.score}</td><td>${purposeChip(p.purpose_class)}<br>${trendCell(p.trend)}</td><td class="num">${pct(p.weight_pct, 1)}</td><td class="num">${yen(p.amount_jpy)}</td>
      <td class="num">${p.price_jpy ? yen(p.price_jpy) : "—"}</td><td>${timingBadge({ label: p.timing, score: p.timing_score })}</td><td>${esc(p.order_method)}<br><small class="muted">${esc(p.entry_plan)}</small></td></tr>`).join("");
  $("#posTable").innerHTML = `<thead><tr><th>銘柄</th><th>追随するファンド</th><th>スコア</th><th>保有目的・比率の推移</th><th>比率</th><th>金額(円)</th><th>株価</th><th>買い時</th><th>買い方</th></tr></thead>
    <tbody><tr><td class="name"><b>コア：国内株インデックス</b><small>${esc(plan.core.name)}</small></td><td><span class="fund">NBIMの分散投資を再現</span></td><td></td><td></td>
    <td class="num">${pct(a.core_pct, 1)}</td><td class="num">${yen(a.core_index_jpy)}</td><td></td><td></td><td><small class="muted">${esc(plan.core.why)}</small></td></tr>${rows}</tbody>`;
  $("#checks").innerHTML = plan.compliance_checks.map((c) => `<li class="${c.passed ? "ok" : "ng"}">${esc(c.rule)}（${esc(c.limit)}）</li>`).join("");
  $("#warns").innerHTML = plan.warnings.map((w) => `<li>⚠ ${esc(w)}</li>`).join("");
  const b = $("#checkBadge");
  b.textContent = plan.all_checks_passed ? "ガードレール：全項目クリア" : "ガードレール：要確認";
  b.className = `badge ${plan.all_checks_passed ? "ok" : "ng"}`;
  $("#disclaimer").textContent = plan.disclaimer;
  $("#approveBtn").disabled = false;
  $("#toMineBtn").disabled = !plan.positions.some((p) => p.shares && p.price_jpy);
}

$("#approveBtn").addEventListener("click", () => {
  if (!lastPlan) return;
  const lines = [["区分", "コード", "銘柄", "金額(円)", "株数", "買い方", "分割計画"]];
  lines.push(["コア", lastPlan.core.code, lastPlan.core.name, lastPlan.allocation.core_index_jpy, "", "積立/一括", "毎月積立も可"]);
  lastPlan.positions.forEach((p) => lines.push(["個別株", p.code, p.name, p.amount_jpy, p.shares ?? "", p.order_method, p.entry_plan]));
  lines.push(["現金", "", "", lastPlan.allocation.cash_jpy, "", "", ""]);
  lines.push([]);
  lines.push([lastPlan.disclaimer]);
  const csv = "﻿" + lines.map((r) => r.map((c) => `"${String(c).replace(/"/g, '""')}"`).join(",")).join("\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
  a.download = `sovereign-lens-plan-${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
});

$("#profile").addEventListener("submit", async (e) => {
  e.preventDefault();
  const p = profile();
  showTab("plan");
  const btn = $("#runBtn");
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span> エージェント実行中…`;
  $("#trace").innerHTML = "";
  $("#traceFoot").textContent = "";
  const STEPS = [
    ["rank_swf_conviction_stocks", "候補を採点"],
    ["build_investment_strategy", "配分を計算"],
    ["get_stock_dossier", "銘柄の保有履歴を確認"],
    ["rationale_researcher", "買った理由をGoogle検索で調査"],
    ["plan_reviewer", "独立エージェントが審査"],
    ["report", "レポートを作成"],
  ];
  const t0 = Date.now();
  $("#report").innerHTML = `<ol class="steps">${STEPS.map(([k, l]) => `<li data-step="${k}"><span class="dot"></span>${l}<em></em></li>`).join("")}</ol><p class="fine" id="elapsed"></p><div id="streamed" class="md"></div>`;
  const tick = setInterval(() => { const el = $("#elapsed"); if (el) el.textContent = `経過 ${((Date.now() - t0) / 1000).toFixed(0)}秒（配分は即時試算を表示中。レポートは書き上がった部分から表示します）`; }, 500);
  const mark = (k, state, note = "") => { const li = document.querySelector(`.steps li[data-step="${k}"]`); if (li) { li.className = state; if (note) li.querySelector("em").textContent = note; } };
  try {
    const r = await fetch("/api/strategy/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(p) });
    if (r.ok) renderPlan(await r.json(), "即時試算");
    else { const d = await r.json(); $("#report").innerHTML = `<p class="down">${esc(d.detail)}</p>`; }
  } catch {}
  let text = "";
  const base = traceHandlers($("#trace"), (d) => {
    if (d.final) { text = d.text; mark("report", "done"); $("#report").innerHTML = md(text); }
    else traceItem($("#trace"), "txt", `<em>${d.t}s</em> ${esc(d.text.slice(0, 120))}`);
  });
  await streamAgent({ profile: p }, {
    ...base,
    tool_call: (d) => { base.tool_call(d); mark(d.tool, "run"); },
    tool_result: (d) => { base.tool_result(d); mark(d.tool, d.error ? "err" : "done", d.cached ? "（キャッシュ）" : ""); },
    delta: (d) => { mark("report", "run"); text += d.text; const el = $("#streamed"); if (el) el.innerHTML = md(text); },
    plan: (plan) => renderPlan(plan, "エージェント確定版"),
    done: (d) => ($("#traceFoot").textContent = `完了 ${d.elapsed_s}s ・ 入力 ${d.tokens.prompt.toLocaleString()} / 出力 ${d.tokens.output.toLocaleString()} tokens ・ ${d.model}`),
    error: (d) => { $("#report").innerHTML = `<p class="down">${esc(d.message)}</p>`; traceItem($("#trace"), "err", esc(d.message)); },
  });
  clearInterval(tick);
  btn.disabled = false;
  btn.textContent = "エージェントに戦略を組ませる";
});

/* ---------- universe ---------- */
function fundChips(s) {
  return s.holders.filter((h) => h.status !== "exited").map((h) => `<span class="fund">${esc(h.investor_short || h.investor_id)}${h.ownership_pct != null ? " " + h.ownership_pct + "%" : ""}</span>`).join("");
}
function renderGuide() {
  $("#guideBody").innerHTML = `<p><b>Conviction Score（0〜100点）</b>…プロのファンドがどれだけ本気でその銘柄を持っているか。高いほど「追随する根拠」が強い。</p>
    <table class="grid guide-t"><tbody>${scoreGuide.map((g) => `<tr><td><b>${esc(g.key)}</b></td><td class="num">${g.max}点</td><td>${esc(g.desc)}</td></tr>`).join("")}</tbody></table>
    <p><b>買い時判定</b>…${esc(timingGuide)}</p>
    <p class="fine"><span class="tbadge buy">買い場候補</span> <span class="tbadge split">分割で買い</span> <span class="tbadge wait">様子見</span> <span class="tbadge avoid">見送り推奨</span>　スコアが高くても、株価が上がりすぎていれば「様子見」になります。</p>`;
}
function renderUniverse() {
  const rows = universe.filter((s) => catFilter === "all" || (s.categories || []).includes(catFilter));
  $("#uniTable").innerHTML = `<thead><tr><th>スコア</th><th>銘柄</th><th>区分</th><th>保有ファンド</th><th>最大保有比率</th><th>保有目的</th><th>推定取得単価</th><th>現在値</th><th>買い時</th><th>保有年数</th><th>比率の推移</th></tr></thead><tbody>${rows
    .map((s) => {
      const mom = { new: "新規", increased: "買増", holding: "継続", reduced: "一部売却", exited: "対象外" }[s.momentum] || s.momentum;
      return `<tr data-code="${esc(s.code)}" ${s.candidate ? "" : 'class="dim"'}>
        <td><div class="scorebar"><span style="width:${s.score * 0.6}px"></span><b class="num">${s.score}</b></div></td>
        <td class="name"><b>${esc(s.name_ja)}</b><small>${esc(s.code)}・${esc(s.sector)}</small>${s.warning ? '<small class="down">⚠ 警告：買わない方がよい</small>' : ""}</td>
        <td>${catChips(s.categories)}</td>
        <td>${fundChips(s)}</td><td class="num">${pct(s.max_ownership_pct)}</td><td>${purposeChip(s.purpose_class)}</td><td class="num">${yen(s.ref_cost_jpy)}</td>
        <td class="num">${yen(s.price_jpy)}</td><td>${timingBadge(s.timing)}</td>
        <td class="num">${s.holding_years ?? "—"}</td><td>${esc(mom)}<br>${trendCell(s.trend, s.heavy_selling)}</td></tr>`;
    })
    .join("")}</tbody>`;
}
$("#catFilter").addEventListener("click", (e) => {
  const c = e.target.dataset.cat; if (!c) return;
  catFilter = c;
  document.querySelectorAll("#catFilter button").forEach((b) => b.classList.toggle("on", b.dataset.cat === c));
  renderUniverse();
});
$("#uniTable").addEventListener("click", (e) => {
  const tr = e.target.closest("tr[data-code]");
  if (tr) openDossier(tr.dataset.code);
});
function openDossier(code) {
  const s = universe.find((x) => x.code === code);
  if (!s) return;
  const holders = s.holders.map((h) => `<tr><td>${esc(h.investor_name)}<br><small class="muted">${esc(h.entity || "")}</small></td>
    <td class="num">${pct(h.ownership_pct)}</td><td class="num">${yen(h.est_cost_jpy)}</td><td>${esc(h.first_seen || "—")}<br><small class="muted">最新 ${esc(h.latest_report || "—")}</small></td>
    <td>${esc(h.status_label)}${h.trend ? `<br><small class="muted">${esc(h.trend)}</small>` : ""}</td>
    <td>${purposeChip(h.purpose_class)}${h.edinet_synced ? '<br><small class="muted">EDINET自動取得</small>' : ""}</td></tr>
    <tr><td colspan="6"><small>${h.purpose ? "保有目的（原文）：" + esc(h.purpose) + "<br>" : ""}${h.important_proposal && !/^該当(なし|事項なし)$/.test(h.important_proposal) ? "重要提案行為等：" + esc(h.important_proposal) + "<br>" : ""}${esc(h.history || "")}${h.cost_basis_note ? "<br>取得単価の根拠：" + esc(h.cost_basis_note) : ""}<br>${(h.sources || []).map((u) => `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(new URL(u).hostname)}</a>`).join(" ・ ")}</small></td></tr>`).join("");
  $("#dossierBody").innerHTML = `<h2>${esc(s.name_ja)} <small>${esc(s.code)}・${esc(s.sector)}</small></h2>
    ${s.warning ? `<p class="warnbox">⚠ ${esc(s.warning)}</p>` : ""}
    <div class="dgrid">
      <div><h3>Conviction Score ${s.score} / 100</h3>${scoreGuide.map((g) => { const v = s.score_breakdown[g.key] ?? 0; return `<div class="bd"><span>${esc(g.key)}</span><i><b style="width:${(v / g.max) * 100}%"></b></i><em>${v}/${g.max}</em></div>`; }).join("")}</div>
      <div><h3>買い時判定 ${timingBadge(s.timing)}</h3><ul class="reasons">${((s.timing || {}).reasons || []).map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
        <p class="fine">52週高値 ${yen(s.timing?.high52)}円 ・ 安値 ${yen(s.timing?.low52)}円 ・ 200日平均 ${yen(s.timing?.ma200)}円</p></div>
    </div>
    <ul>${s.signals.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
    <div class="tablewrap"><table class="grid"><thead><tr><th>ファンド</th><th>保有比率</th><th>推定取得単価</th><th>初回／最新報告</th><th>状態・推移</th><th>保有目的</th></tr></thead><tbody>${holders}</tbody></table></div>
    <p><button class="secondary" id="whyBtn">このファンドが買った理由をエージェントに調べさせる</button></p>`;
  $("#dossier").showModal();
  $("#whyBtn").onclick = () => {
    $("#dossier").close();
    const funds = s.holders.filter((h) => h.status !== "exited").map((h) => h.investor_name).join("、");
    ask(`${funds}が${s.name_ja}（${s.code}）に投資した理由を、取得コストと保有期間も含めて教えてください。`);
  };
}

/* ---------- funds ---------- */
async function loadFunds() {
  const d = await (await fetch("/api/investors")).json();
  const groups = [["sovereign", "政府系ファンド・公的年金（長期保有）"], ["activist", "物言う株主（増配・TOB・経営改革などのイベント狙い）"], ["financing", "警告対象：資金調達型（保有銘柄は買わない方がよい）"]];
  $("#fundCards").innerHTML = groups.map(([c, title]) => `<h2 class="grouph">${title}</h2><div class="cards">${d.investors.filter((i) => i.category === c).map((i) => `<div class="panel ${c}"><h3>${esc(i.name_ja)}</h3>
    <div class="meta">${esc(i.country)} ・ ${esc(i.type)} ・ 開示：${esc(i.disclosure)}</div>
    <p>${esc(i.profile)}</p><p class="fine">追跡中の日本株：${i.japan_holdings_tracked}銘柄</p></div>`).join("")}</div>`).join("") +
    `<p class="fine">ADIA・KIA・テマセク・中国系（CIC/SAFE）なども日本株を保有していますが、1社あたり5%未満のため大量保有報告書には出てきません。保有の全容を公開しているのはNBIMだけです（「NBIMライブ」タブ）。</p>`;
}

/* ---------- NBIM live ---------- */
$("#nbimBtn").addEventListener("click", async () => {
  $("#nbimOut").innerHTML = `<p class="muted"><span class="spinner"></span> NBIMから取得中（初回は数十秒）…</p>`;
  const r = await fetch("/api/nbim/live?top_n=30");
  if (!r.ok) { $("#nbimOut").innerHTML = `<p class="down">取得できませんでした（${r.status}）</p>`; return; }
  const d = await r.json();
  $("#nbimOut").innerHTML = `<p class="fine">基準日 ${esc(d.period)} ・ 日本株 ${d.japan_company_count.toLocaleString()}社 ・ 合計 ${(d.japan_total_usd / 1e9).toFixed(1)}十億USD</p>
    <table class="grid"><thead><tr><th>#</th><th>銘柄</th><th>業種</th><th>時価(百万USD)</th><th>保有比率</th><th>初保有（推定）</th></tr></thead><tbody>${d.top
      .map((h, i) => `<tr><td class="num">${i + 1}</td><td>${esc(h.name_en)}${h.code ? ` <small class="muted">${esc(h.code)}</small>` : ""}</td><td>${esc(h.industry || "")}</td>
      <td class="num">${h.market_value_usd ? Math.round(h.market_value_usd / 1e6).toLocaleString() : "—"}</td><td class="num">${pct(h.ownership_pct)}</td><td>${esc((h.first_seen || "2020年以降").slice(0, 4))}</td></tr>`).join("")}</tbody></table>`;
});

/* ---------- ask ---------- */
async function ask(q) {
  showTab("ask");
  $("#askInput").value = q;
  $("#askOut").innerHTML = `<p class="muted"><span class="spinner"></span> 調査中…</p>`;
  $("#askTrace").innerHTML = "";
  await streamAgent({ message: q }, {
    ...traceHandlers($("#askTrace"), (d) => { if (d.final) $("#askOut").innerHTML = md(d.text); }),
    done: (d) => traceItem($("#askTrace"), "txt", `<em>${d.elapsed_s}s</em> 完了 ・ ${d.tokens.prompt + d.tokens.output} tokens`),
    error: (d) => ($("#askOut").innerHTML = `<p class="down">${esc(d.message)}</p>`),
  });
}
$("#askForm").addEventListener("submit", (e) => { e.preventDefault(); const q = $("#askInput").value.trim(); if (q) ask(q); });
$("#examples").addEventListener("click", (e) => { if (e.target.tagName === "BUTTON") ask(e.target.textContent); });

/* ---------- boot ---------- */
(async () => {
  try {
    const h = await (await fetch("/api/health")).json();
    $("#status").innerHTML = `Agent: ${esc(h.model)}<br>Reviewer: ${esc(h.reviewer || h.model)}<br>EDINET API: ${h.edinet ? "接続" : "シードデータ"}${h.edinet_synced_at ? `<br>最終同期: ${esc(new Date(h.edinet_synced_at).toLocaleString("ja-JP", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }))}` : ""}`;
  } catch { $("#status").textContent = "API未接続"; }
  loadFunds();
  const loadUni = async (tries = 0) => {
    const u = await (await fetch("/api/universe")).json();
    universe = u.stocks;
    scoreGuide = u.score_guide || []; timingGuide = u.timing_guide || ""; renderGuide();
    $("#asOf").textContent = u.prices_loading ? `データ基準日 ${u.as_of}${u.synced_at ? "・EDINET同期 " + u.synced_at.slice(0, 10) : ""}・株価を取得中…` : `データ基準日 ${u.as_of}${u.synced_at ? "・EDINET同期 " + u.synced_at.slice(0, 10) : ""}・株価は取得時点`;
    renderUniverse();
    if (u.prices_loading && tries < 6) setTimeout(() => loadUni(tries + 1), 5000);
  };
  loadUni();
})();

/* ---------- 保有管理（このブラウザのlocalStorageのみ） ---------- */
const MINE_KEY = "sovereign-lens-holdings-v1";
let mine = [];
let mineQuotes = {};
const loadMine = () => { try { mine = JSON.parse(localStorage.getItem(MINE_KEY) || "[]"); } catch { mine = []; } };
const saveMine = () => { try { localStorage.setItem(MINE_KEY, JSON.stringify(mine)); } catch {} };
const nameOf = (code) => universe.find((s) => s.code === code)?.name_ja || mineQuotes[code]?.name_ja || "";

async function refreshMine() {
  const codes = [...new Set(mine.map((m) => m.code))];
  if (codes.length) {
    try { mineQuotes = (await (await fetch(`/api/quotes?codes=${codes.join(",")}`)).json()).quotes || {}; } catch {}
  }
  renderMine();
}
function renderMine() {
  if (!mine.length) {
    $("#mineTable").innerHTML = `<tbody><tr><td class="muted">まだ記録がありません。上のフォームで追加するか、戦略タブの「買った銘柄を保有管理に記録」を押してください。</td></tr></tbody>`;
    $("#mineSum").innerHTML = "";
    return;
  }
  let cost = 0, value = 0, day = 0;
  const rows = mine.map((m, i) => {
    const q = mineQuotes[m.code] || {};
    const c = m.shares * m.price;
    const v = q.price ? m.shares * q.price : null;
    const pl = v == null ? null : v - c;
    cost += c; if (v != null) value += v; else value += c;
    if (q.price && q.prev_close) day += m.shares * (q.price - q.prev_close);
    const cls = pl == null ? "" : pl >= 0 ? "up" : "down";
    return `<tr><td class="name"><b>${esc(nameOf(m.code) || m.code)}</b><small>${esc(m.code)}${m.date ? "・" + esc(m.date) : ""}</small></td>
      <td class="num">${m.shares.toLocaleString()}</td><td class="num">${yen(m.price)}</td><td class="num">${yen(q.price)}</td>
      <td class="num">${yen(c)}</td><td class="num">${yen(v)}</td>
      <td class="num ${cls}">${pl == null ? "—" : (pl >= 0 ? "+" : "") + yen(pl)}<br><small>${pl == null ? "" : ((pl / c) * 100).toFixed(1) + "%"}</small></td>
      <td>${q.timing ? timingBadge(q.timing) : '<small class="muted">追跡外</small>'}${q.fund_alert ? `<br><small class="down">⚠ ${esc(q.fund_alert)}</small>` : ""}</td>
      <td><button class="linkbtn" data-del="${i}" aria-label="削除">削除</button></td></tr>`;
  }).join("");
  $("#mineTable").innerHTML = `<thead><tr><th>銘柄</th><th>株数</th><th>取得単価</th><th>現在値</th><th>取得額</th><th>評価額</th><th>損益</th><th>ファンドの動き・買い時</th><th></th></tr></thead><tbody>${rows}</tbody>`;
  const pl = value - cost;
  $("#mineSum").innerHTML = `<div><span>取得額合計</span><b class="num">${yen(cost)}円</b></div><div><span>評価額合計</span><b class="num">${yen(value)}円</b></div>
    <div><span>評価損益</span><b class="num ${pl >= 0 ? "up" : "down"}">${pl >= 0 ? "+" : ""}${yen(pl)}円（${cost ? ((pl / cost) * 100).toFixed(2) : "0.00"}%）</b></div>
    <div><span>前日比</span><b class="num ${day >= 0 ? "up" : "down"}">${day >= 0 ? "+" : ""}${yen(day)}円</b></div>`;
}
$("#mineForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const code = $("#mCode").value.trim().toUpperCase();
  if (!/^[0-9]{3}[0-9A-Z]$/.test(code)) { $("#mCode").setCustomValidity("4桁の銘柄コードを入力してください"); $("#mCode").reportValidity(); return; }
  $("#mCode").setCustomValidity("");
  mine.push({ code, shares: Number($("#mShares").value), price: Number($("#mPrice").value), date: $("#mDate").value || new Date().toISOString().slice(0, 10) });
  saveMine(); e.target.reset(); refreshMine();
});
$("#mCode").addEventListener("input", () => $("#mCode").setCustomValidity(""));
$("#mineTable").addEventListener("click", (e) => {
  const i = e.target.dataset.del; if (i == null) return;
  mine.splice(Number(i), 1); saveMine(); renderMine();
});
$("#mineRefresh").addEventListener("click", refreshMine);
$("#mineExport").addEventListener("click", () => {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([JSON.stringify(mine, null, 2)], { type: "application/json" }));
  a.download = "sovereign-lens-holdings.json"; a.click();
});
$("#mineImport").addEventListener("change", async (e) => {
  const f = e.target.files[0]; if (!f) return;
  try {
    const d = JSON.parse(await f.text());
    if (Array.isArray(d)) { mine = d.filter((m) => m && /^[0-9]{3}[0-9A-Z]$/.test(m.code) && m.shares > 0 && m.price >= 0); saveMine(); refreshMine(); }
  } catch {}
});
$("#toMineBtn").addEventListener("click", () => {
  if (!lastPlan) return;
  const today = new Date().toISOString().slice(0, 10);
  lastPlan.positions.filter((p) => p.shares && p.price_jpy).forEach((p) => mine.push({ code: p.code, shares: p.shares, price: p.price_jpy, date: today }));
  saveMine(); showTab("mine"); refreshMine();
});
document.querySelector('.tabs button[data-tab="mine"]').addEventListener("click", refreshMine);
loadMine();
