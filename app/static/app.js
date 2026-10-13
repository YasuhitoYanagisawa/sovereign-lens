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
      <td class="num">${p.score}</td><td class="num">${pct(p.weight_pct, 1)}</td><td class="num">${yen(p.amount_jpy)}</td>
      <td class="num">${p.price_jpy ? yen(p.price_jpy) : "—"}</td><td>${esc(p.order_method)}<br><small class="muted">${esc(p.entry_plan)}</small></td></tr>`).join("");
  $("#posTable").innerHTML = `<thead><tr><th>銘柄</th><th>追随するファンド</th><th>スコア</th><th>比率</th><th>金額(円)</th><th>株価</th><th>買い方</th></tr></thead>
    <tbody><tr><td class="name"><b>コア：国内株インデックス</b><small>${esc(plan.core.name)}</small></td><td><span class="fund">NBIMの分散投資を再現</span></td><td></td>
    <td class="num">${pct(a.core_pct, 1)}</td><td class="num">${yen(a.core_index_jpy)}</td><td></td><td><small class="muted">${esc(plan.core.why)}</small></td></tr>${rows}</tbody>`;
  $("#checks").innerHTML = plan.compliance_checks.map((c) => `<li class="${c.passed ? "ok" : "ng"}">${esc(c.rule)}（${esc(c.limit)}）</li>`).join("");
  $("#warns").innerHTML = plan.warnings.map((w) => `<li>⚠ ${esc(w)}</li>`).join("");
  const b = $("#checkBadge");
  b.textContent = plan.all_checks_passed ? "ガードレール：全項目クリア" : "ガードレール：要確認";
  b.className = `badge ${plan.all_checks_passed ? "ok" : "ng"}`;
  $("#disclaimer").textContent = plan.disclaimer;
  $("#approveBtn").disabled = false;
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
  $("#report").innerHTML = `<p class="muted"><span class="spinner"></span> 収集・採点・理由調査・審査を自律実行しています（30〜90秒）</p>`;
  try {
    const r = await fetch("/api/strategy/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(p) });
    if (r.ok) renderPlan(await r.json(), "即時試算");
    else { const d = await r.json(); $("#report").innerHTML = `<p class="down">${esc(d.detail)}</p>`; }
  } catch {}
  let text = "";
  await streamAgent({ profile: p }, {
    ...traceHandlers($("#trace"), (d) => {
      if (d.final) { text = d.text; $("#report").innerHTML = md(text); }
      else traceItem($("#trace"), "txt", `<em>${d.t}s</em> ${esc(d.text.slice(0, 120))}`);
    }),
    plan: (plan) => renderPlan(plan, "エージェント確定版"),
    done: (d) => ($("#traceFoot").textContent = `完了 ${d.elapsed_s}s ・ 入力 ${d.tokens.prompt.toLocaleString()} / 出力 ${d.tokens.output.toLocaleString()} tokens ・ ${d.model}`),
    error: (d) => { $("#report").innerHTML = `<p class="down">${esc(d.message)}</p>`; traceItem($("#trace"), "err", esc(d.message)); },
  });
  btn.disabled = false;
  btn.textContent = "エージェントに戦略を組ませる";
});

/* ---------- universe ---------- */
function fundChips(s) {
  return s.holders.filter((h) => h.status !== "exited").map((h) => `<span class="fund">${esc(h.investor_short || h.investor_id)}${h.ownership_pct != null ? " " + h.ownership_pct + "%" : ""}</span>`).join("");
}
function renderUniverse() {
  $("#uniTable").innerHTML = `<thead><tr><th>スコア</th><th>銘柄</th><th>保有ファンド</th><th>最大保有比率</th><th>推定取得単価</th><th>現在値</th><th>取得比</th><th>保有年数</th><th>直近</th></tr></thead><tbody>${universe
    .map((s) => {
      const r = s.price_to_cost;
      const rc = r == null ? "" : r <= 1 ? "up" : r > 1.5 ? "down" : "";
      const mom = { new: "新規", increased: "買増", holding: "継続", reduced: "一部売却", exited: "対象外" }[s.momentum] || s.momentum;
      return `<tr data-code="${esc(s.code)}" ${s.candidate ? "" : 'style="opacity:.5"'}>
        <td><div class="scorebar"><span style="width:${s.score * 0.6}px"></span><b class="num">${s.score}</b></div></td>
        <td class="name"><b>${esc(s.name_ja)}</b><small>${esc(s.code)}・${esc(s.sector)}</small></td>
        <td>${fundChips(s)}</td><td class="num">${pct(s.max_ownership_pct)}</td><td class="num">${yen(s.ref_cost_jpy)}</td>
        <td class="num">${yen(s.price_jpy)}</td><td class="num ${rc}">${r == null ? "—" : r.toFixed(2) + "x"}</td>
        <td class="num">${s.holding_years ?? "—"}</td><td>${esc(mom)}</td></tr>`;
    })
    .join("")}</tbody>`;
}
$("#uniTable").addEventListener("click", (e) => {
  const tr = e.target.closest("tr[data-code]");
  if (tr) openDossier(tr.dataset.code);
});
function openDossier(code) {
  const s = universe.find((x) => x.code === code);
  if (!s) return;
  const holders = s.holders.map((h) => `<tr><td>${esc(h.investor_name)}<br><small class="muted">${esc(h.entity || "")}</small></td>
    <td class="num">${pct(h.ownership_pct)}</td><td class="num">${yen(h.est_cost_jpy)}</td><td>${esc(h.first_seen || "—")}<br><small class="muted">最新 ${esc(h.latest_report || "—")}</small></td>
    <td>${esc(h.status_label)}</td></tr>
    <tr><td colspan="5"><small>${esc(h.history || "")}${h.cost_basis_note ? "<br>取得単価の根拠：" + esc(h.cost_basis_note) : ""}<br>${(h.sources || []).map((u) => `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(new URL(u).hostname)}</a>`).join(" ・ ")}</small></td></tr>`).join("");
  $("#dossierBody").innerHTML = `<h2>${esc(s.name_ja)} <small>${esc(s.code)}・${esc(s.sector)}</small></h2>
    <p><b>Sovereign Conviction Score ${s.score}</b>　${Object.entries(s.score_breakdown).map(([k, v]) => `${esc(k)} ${v}`).join(" / ")}</p>
    <ul>${s.signals.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>
    <div class="tablewrap"><table class="grid"><thead><tr><th>ファンド</th><th>保有比率</th><th>推定取得単価</th><th>初回／最新報告</th><th>状態</th></tr></thead><tbody>${holders}</tbody></table></div>
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
  $("#fundCards").innerHTML = d.investors.map((i) => `<div class="panel"><h3>${esc(i.name_ja)}</h3>
    <div class="meta">${esc(i.country)} ・ ${esc(i.type)} ・ 開示：${esc(i.disclosure)}</div>
    <p>${esc(i.profile)}</p><p class="fine">追跡中の日本株：${i.japan_holdings_tracked}銘柄</p></div>`).join("");
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
    $("#status").innerHTML = `Agent: ${esc(h.model)}<br>Reviewer: ${esc(h.reviewer || h.model)}<br>EDINET API: ${h.edinet ? "接続" : "シードデータ"}`;
  } catch { $("#status").textContent = "API未接続"; }
  loadFunds();
  const loadUni = async (tries = 0) => {
    const u = await (await fetch("/api/universe")).json();
    universe = u.stocks;
    $("#asOf").textContent = u.prices_loading ? `データ基準日 ${u.as_of}・株価を取得中…` : `データ基準日 ${u.as_of}・株価は取得時点`;
    renderUniverse();
    if (u.prices_loading && tries < 6) setTimeout(() => loadUni(tries + 1), 5000);
  };
  loadUni();
})();
