/* InvoiceFlow front-end — vanilla JS, hash routing, live run view over SSE. */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (v, cur = "USD") => {
  if (v === null || v === undefined || isNaN(v)) return "—";
  cur = (cur || "USD").toUpperCase();
  try { return new Intl.NumberFormat(cur === "INR" ? "en-IN" : "en-US", { style: "currency", currency: cur }).format(v); }
  catch { return `${cur} ${Number(v).toFixed(2)}`; }
};
const moneyMulti = (o) => { const e = Object.entries(o || {}); return e.length ? e.map(([c, v]) => money(v, c)).join(" + ") : money(0); };
const num = (v) => (v === null || v === undefined) ? "—" : Number(v).toLocaleString("en-US", { maximumFractionDigits: 2 });
const ago = (iso) => {
  const s = Math.max(0, (Date.now() - new Date(iso)) / 1000);
  if (s < 45) return "just now"; if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
};
const api = async (url, opts) => {
  const r = await fetch(url, opts);
  if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail || m; } catch {} throw new Error(m); }
  return r.json();
};
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

const I = {
  check: '<svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>',
  x: '<svg viewBox="0 0 24 24"><path d="M7 7l10 10M17 7L7 17"/></svg>',
  pause: '<svg viewBox="0 0 24 24"><path d="M9.5 7v10M14.5 7v10"/></svg>',
  info: '<svg viewBox="0 0 24 24"><path d="M12 11v6M12 7.5v.5"/></svg>',
  chev: '<svg class="chev" viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg>',
  back: '<svg viewBox="0 0 24 24"><path d="M15 6l-6 6 6 6"/></svg>',
  up: '<svg viewBox="0 0 24 24"><path d="M12 15V4m0 0L7.5 8.5M12 4l4.5 4.5M4 15v3a2 2 0 002 2h12a2 2 0 002-2v-3"/></svg>',
  spin: '<svg viewBox="0 0 24 24"><path d="M12 3a9 9 0 109 9"/></svg>',
  search: '<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4-4"/></svg>',
  play: '<svg viewBox="0 0 24 24"><path d="M8 5.5v13l10-6.5z"/></svg>',
  ext: '<svg viewBox="0 0 24 24"><path d="M14 5h5v5M19 5l-8 8M18 14v4a1 1 0 01-1 1H6a1 1 0 01-1-1V7a1 1 0 011-1h4"/></svg>',
};
const DOT = { pass: I.check, note: I.info, warn: I.pause, fail: I.x, skip: "", pending: "", running: "" };
const WORD = { APPROVE: "Approved", HOLD: "On hold", REJECT: "Rejected" };
const cleanHeadline = (d, h) => {
  const t = (h || "").replace(/^(On hold|Rejected|Approved for payment) - /, "");
  return d === "APPROVE" ? `Pay ${t}` : t;
};
const SEV = { reject: "Reject", hold: "Hold", note: "Note", pass: "Pass" };

const state = { config: null, engine: localStorage.getItem("engine") || "auto", filter: "ALL", cat: "All", search: "", es: null, timers: [], seen: {} };

function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.add("show"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 2400); }
function cleanup() { state.timers.forEach(clearInterval); state.timers = []; if (state.es) { state.es.close(); state.es = null; } }

/* Sliding indicator for segmented controls and top tabs */
function slide(ind, el) { if (!ind || !el) return; ind.style.width = el.offsetWidth + "px"; ind.style.transform = `translateX(${el.offsetLeft - (ind.parentElement === $("#tabs") ? 0 : 3)}px)`; }
function seg(id, opts, current, onPick) {
  return { html: `<div class="seg" id="${id}"><span class="seg-ind"></span>${opts.map((o) => `<button data-k="${o.k}" class="${o.k === current ? "on" : ""}" ${o.disabled ? "disabled title='" + esc(o.title || "") + "'" : ""}>${o.label}</button>`).join("")}</div>`,
    mount() {
      const root = $("#" + id), ind = $(".seg-ind", root);
      const place = () => slide(ind, $("button.on", root));
      requestAnimationFrame(place);
      root.addEventListener("click", (e) => {
        const b = e.target.closest("button"); if (!b || b.disabled) return;
        $$("button", root).forEach((x) => x.classList.toggle("on", x === b)); place(); onPick(b.dataset.k);
      });
    } };
}

function countUp(el, to, fmt = (v) => Math.round(v).toString()) {
  const from = Number(el.dataset.v || 0); el.dataset.v = to;
  if (reduceMotion || from === to) { el.textContent = fmt(to); return; }
  const t0 = performance.now(), d = 500;
  const step = (t) => { const p = Math.min(1, (t - t0) / d), e = 1 - Math.pow(1 - p, 3); el.textContent = fmt(from + (to - from) * e); if (p < 1) requestAnimationFrame(step); };
  requestAnimationFrame(step);
}

async function loadConfig() {
  state.config = await api("/api/config");
  const e = state.config.engines, el = $("#engine");
  el.className = "engine" + (e.gemini ? " on" : "");
  el.innerHTML = `<i></i><span>${e.gemini ? "Gemini connected" : "Offline mode"}</span>`;
  el.title = e.gemini ? `AI extraction with ${e.model}. OCR fallback ${e.ocr ? "ready" : "not installed"}.` : `Rule-based parser${e.ocr ? " with Tesseract OCR" : ""}. Set GEMINI_API_KEY to enable AI extraction.`;
  if (!e.gemini && state.engine === "gemini") state.engine = "auto";
}

/* ============================ Router ============================ */
async function route() {
  cleanup(); closeDrawer(); window.scrollTo(0, 0);
  const [, page = "process", id] = (location.hash || "#/process").split("/");
  const nav = page === "run" ? "dashboard" : page;
  $$("#tabs a").forEach((a) => a.classList.toggle("active", a.dataset.nav === nav));
  slide($("#tab-ind"), $("#tabs a.active"));
  const v = $("#view");
  v.classList.remove("enter"); void v.offsetWidth; v.classList.add("enter");
  try {
    if (page === "run" && id) return await renderRun(v, id);
    if (page === "dashboard") return await renderDashboard(v);
    if (page === "pos") return await renderPOs(v);
    if (page === "rules") return await renderRules(v);
    return renderProcess(v);
  } catch (e) { v.innerHTML = `<div class="err">${esc(e.message)}</div>`; }
}
window.addEventListener("hashchange", route);
window.addEventListener("resize", () => slide($("#tab-ind"), $("#tabs a.active")));

/* ============================ Upload (anywhere) ============================ */
async function upload(file) {
  if (state.config?.demo_mode) { toast("Uploads are disabled on this public demo."); return; }
  const dz = $("#dropzone");
  if (dz) { dz.classList.add("busy"); $("strong", dz).textContent = `Uploading ${file.name}`; }
  const fd = new FormData(); fd.append("file", file); fd.append("engine", state.engine);
  try { const r = await api("/api/runs", { method: "POST", body: fd }); location.hash = `#/run/${r.run_id}`; }
  catch (e) {
    if (dz) { dz.classList.remove("busy"); $("strong", dz).textContent = "Drop an invoice here, or choose a file"; $("#up-err").innerHTML = `<div class="err">${esc(e.message)}</div>`; }
    else toast(e.message);
  }
}
async function runSample(name) {
  const fd = new FormData(); fd.append("sample", name); fd.append("engine", state.engine);
  try { const r = await api("/api/runs", { method: "POST", body: fd }); location.hash = `#/run/${r.run_id}`; }
  catch (e) { toast(e.message); }
}
let dragDepth = 0;
window.addEventListener("dragenter", (e) => { if (state.config?.demo_mode || ![...e.dataTransfer.types].includes("Files")) return; e.preventDefault(); dragDepth++; $("#drop-overlay").classList.add("show"); });
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("dragleave", () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) $("#drop-overlay").classList.remove("show"); });
window.addEventListener("drop", (e) => { e.preventDefault(); dragDepth = 0; $("#drop-overlay").classList.remove("show"); if (state.config?.demo_mode) { toast("Uploads are disabled on this public demo."); return; } const f = e.dataTransfer.files[0]; if (f) upload(f); });

/* ============================ Process ============================ */
function renderProcess(v) {
  const samples = state.config.samples || [];
  const cats = ["All", ...new Set(samples.map((s) => s.category))];
  const g = state.config.engines.gemini;
  const uploadUi = state.config.demo_mode
    ? `<div class="surface" style="padding:16px;margin-bottom:16px">This public demo uses the prepared invoices below; file uploads are disabled.</div>`
    : `<label class="dropzone" id="dropzone">
      <input type="file" id="file" accept=".pdf,.png,.jpg,.jpeg,.tif,.tiff,.webp" hidden />
      <div><div class="ring">${I.up}</div><strong>Drop an invoice here, or choose a file</strong>
      <div class="hint">You can also drop a file anywhere on this page.</div>
      <div class="types">Digital or scanned PDF, PNG, JPG or TIFF, up to 15 MB</div></div>
    </label>
    <div id="up-err"></div>`;
  const eng = seg("engine-seg", [{ k: "auto", label: "Auto" }, { k: "gemini", label: "Gemini", disabled: !g, title: "Add GEMINI_API_KEY to enable" }, { k: "offline", label: "Offline" }],
    state.engine, (k) => { state.engine = k; localStorage.setItem("engine", k); });
  v.innerHTML = `
    <div class="head">
      <div><h1>Process an invoice</h1><p>${state.config.demo_mode ? "Try a prepared invoice and see every check behind its decision." : "Drop in a vendor invoice. You'll see every check as it runs and get a decision you can explain."}</p></div>
      <div><div class="faint small" style="margin-bottom:6px">Read invoices with</div>${eng.html}</div>
    </div>
    ${uploadUi}

    <div class="section-title"><h2>Demo invoices</h2><span class="faint small">${samples.length} prepared cases, each with the outcome it should get</span><span class="spacer"></span>
      <button class="btn sm" id="batch">Run all in order</button></div>
    <div class="filters" id="cats" style="margin-bottom:12px">${cats.map((c) => `<button class="filter ${c === state.cat ? "on" : ""}" data-c="${esc(c)}">${esc(c)}</button>`).join("")}</div>
    <div class="library" id="lib">${samples.map((s) => `
      <div class="lib-row" tabindex="0" data-sample="${esc(s.file)}" data-cat="${esc(s.category)}">
        <div><div class="t">${esc(s.title)}</div><div class="s">${esc(s.story)}</div>
          <div class="f">${esc(s.category)} · <a href="/samples/${encodeURIComponent(s.file)}" target="_blank" rel="noopener" data-stop>${esc(s.file)}</a></div></div>
        <div class="expect">Expect <b class="${s.expected}">${WORD[s.expected] || s.expected}</b></div>
        <button class="btn sm primary go" data-run tabindex="-1">Run</button>
      </div>`).join("")}</div>`;
  eng.mount();
  const file = $("#file");
  if (file) file.addEventListener("change", () => file.files[0] && upload(file.files[0]));
  const filterLib = () => $$(".lib-row").forEach((r) => r.classList.toggle("hide", state.cat !== "All" && r.dataset.cat !== state.cat));
  filterLib();
  $("#cats").addEventListener("click", (e) => { const b = e.target.closest(".filter"); if (!b) return; state.cat = b.dataset.c; $$(".filter").forEach((x) => x.classList.toggle("on", x === b)); filterLib(); });
  $("#lib").addEventListener("click", (e) => { if (e.target.closest("[data-stop]")) return; const r = e.target.closest(".lib-row"); if (r) runSample(r.dataset.sample); });
  $("#lib").addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.classList.contains("lib-row")) runSample(e.target.dataset.sample); });
  $("#batch").addEventListener("click", async () => {
    const fd = new FormData(); fd.append("engine", state.engine);
    try { const r = await api("/api/batch", { method: "POST", body: fd }); toast(`Queued ${r.run_ids.length} invoices`); location.hash = "#/dashboard"; }
    catch (e) { toast(e.message); }
  });
}

/* ============================ Run (live) ============================ */
async function renderRun(v, id) {
  const run = await api(`/api/runs/${id}`);
  const stages = state.config.stages;
  const st = Object.fromEntries(stages.map((s) => [s.id, { status: "pending", summary: s.desc, logs: [], payload: null, open: false }]));
  v.innerHTML = `
    <div class="run-top"><a class="back" href="#/dashboard">${I.back}All runs</a></div>
    <div class="run-title"><h1>${esc(run.filename)}</h1><span class="chip running" id="status">Processing</span></div>
    <div class="facts"><span>Started <b>${new Date(run.created_at).toLocaleTimeString("en-GB")}</b></span><span>Read with <b id="eng">${esc(run.engine_used)}</b></span>
      <span>Source <b>${esc(run.source)}</b></span><span id="dur"></span><span>Run <b>${esc(run.id)}</b></span></div>
    <div class="bar" id="bar"><i></i></div>
    <div class="run-grid">
      <div class="surface" style="padding:10px 18px"><ol class="steps" id="steps">${stages.map((s) => `
        <li class="step pending" data-id="${s.id}">
          <span class="dot">${I.check}</span>
          <div class="step-head"><div class="step-main"><div class="step-name">${esc(s.title)}</div><div class="step-sum">${esc(s.desc)}</div><div class="step-logs"></div></div>
            <span class="step-ms"></span>${I.chev}</div>
          <div class="collapse"><div><div class="detail"></div></div></div>
        </li>`).join("")}</ol></div>
      <div class="side">
        <div class="surface slip" id="slip"></div>
        <div class="surface doc"><div class="doc-head"><h3 style="font-size:14px">Original document</h3><span class="spacer"></span>
          <a class="btn sm quiet" href="/api/runs/${id}/file" target="_blank" rel="noopener">${I.ext}Open</a></div>
          <img class="loading" src="/api/runs/${id}/preview.png" alt="Invoice preview" onload="this.classList.remove('loading')" data-zoom /></div>
      </div>
    </div>`;
  renderSlip(null);

  const paint = (sid) => {
    const x = st[sid], li = $(`.step[data-id="${sid}"]`), done = !["pending", "running"].includes(x.status);
    li.className = `step ${x.status}${done ? " done" : ""}${x.payload && x.status !== "skip" ? " has-detail" : ""}${x.open ? " open" : ""}`;
    $(".dot", li).innerHTML = DOT[x.status] || "";
    $(".step-sum", li).textContent = x.summary;
    $(".step-ms", li).textContent = x.payload && x.status !== "skip" ? `${x.payload.ms} ms` : "";
    $(".step-logs", li).innerHTML = x.logs.map((l) => `<div>${esc(l)}</div>`).join("");
    if (x.payload && !x.rendered) { $(".detail", li).innerHTML = stageDetail(sid, x.payload); x.rendered = true; }
    const n = stages.filter((s) => !["pending", "running"].includes(st[s.id].status)).length;
    $("#bar > i").style.width = `${(100 * n) / stages.length}%`;
  };
  $("#steps").addEventListener("click", (e) => {
    const head = e.target.closest(".step-head"); if (!head) return;
    const sid = head.parentElement.dataset.id; if (!st[sid].payload || st[sid].status === "skip") return;
    st[sid].open = !st[sid].open; paint(sid);
    if (st[sid].open) $$(".balance .track i", head.parentElement).forEach((i) => requestAnimationFrame(() => (i.style.width = i.dataset.w)));
  });

  const es = new EventSource(`/api/runs/${id}/stream`);
  state.es = es;
  es.onmessage = (m) => {
    const e = JSON.parse(m.data), x = st[e.stage]; if (!x) return;
    if (e.kind === "start") x.status = "running";
    else if (e.kind === "log") { x.logs.push(e.title); if (/fall(ing)? back/i.test(e.title)) $("#eng").textContent = "offline (fallback)"; }
    else if (e.kind === "end") {
      x.status = e.status; x.summary = e.title; x.payload = e.payload || { findings: [], data: {}, ms: 0 };
      if (["warn", "fail"].includes(e.status) && e.stage !== "decision") x.open = true;
      if (e.stage === "decision" && x.payload.data && x.payload.data.decision) renderSlip(x.payload.data, true);
    }
    paint(e.stage);
    if (x.open) $$(`.step[data-id="${e.stage}"] .balance .track i`).forEach((i) => requestAnimationFrame(() => requestAnimationFrame(() => (i.style.width = i.dataset.w))));
  };
  es.addEventListener("done", async () => {
    es.close(); state.es = null;
    const full = await api(`/api/runs/${id}`);
    const s = $("#status"); s.className = `chip ${full.decision || full.status}`; s.textContent = WORD[full.decision] || full.status;
    $("#bar").classList.add("done");
    if (full.duration_ms) $("#dur").innerHTML = `Took <b>${(full.duration_ms / 1000).toFixed(1)} s</b>`;
    $("#eng").textContent = full.engine_used;
    stages.forEach((sg) => { if (["pending", "running"].includes(st[sg.id].status)) { st[sg.id].status = "skip"; paint(sg.id); } });
    if (full.result && full.result.decision) { if (!$("#slip .stamp")) renderSlip(full.result, true); }
    else if (full.status === "error") renderSlip({ decision: "HOLD", headline: full.headline, reasons: [{ severity: "hold", rule: "SYS", title: "Processing error", detail: full.result?.error || "" }], actions: [{ owner: "AP clerk", action: "Review manually" }] }, true);
  });
}

function slipHtml(d, animate) {
  const reasons = d.reasons || [];
  return `
    <div class="slip-top"><div class="stamp ${d.decision}${animate ? " land" : ""}">${WORD[d.decision] || d.decision}</div><div><div class="headline">${esc(cleanHeadline(d.decision, d.headline))}</div>
      <div class="meta">${d.confidence !== undefined ? `<span>Extraction confidence ${Math.round(d.confidence * 100)}%</span>` : ""}${d.queue ? `<span>Routed to ${esc(d.queue)}</span>` : ""}${d.ledger_posted ? "<span>PO balance updated</span>" : ""}</div></div></div>
    ${reasons.length ? `<div class="slip-sec"><h3>Why</h3>${reasons.map((r) => `<div class="finding ${r.severity}"><span class="mark"></span><div><div class="ft">${esc(r.title)}<span class="rule-id">${esc(r.rule)}</span></div>${r.detail ? `<div class="fd">${esc(r.detail)}</div>` : ""}</div></div>`).join("")}</div>` : ""}
    ${(d.actions || []).length ? `<div class="slip-sec"><h3>Next step</h3>${d.actions.map((a) => `<div class="action"><span class="owner">${esc(a.owner)}</span><span>${esc(a.action)}</span></div>`).join("")}</div>` : ""}
    ${d.message ? `<div class="slip-sec"><details><summary>Draft message to ${esc(d.message.to)}</summary>
      <div class="mail"><div class="mh">Subject: ${esc(d.message.subject)}</div><pre>${esc(d.message.body)}</pre><div class="mf"><button class="btn sm" data-copy>Copy message</button></div></div></details></div>` : ""}
    ${(d.passed || []).length ? `<div class="slip-sec" style="padding-top:0;border:0"><details><summary>${d.passed.length} checks passed</summary><div class="passed" style="margin-top:6px">${d.passed.map((p) => `<div>${esc(p.title)}</div>`).join("")}</div></details></div>` : ""}`;
}
function wireSlip(root, d) {
  const b = $("[data-copy]", root);
  if (b) b.onclick = () => { navigator.clipboard?.writeText(`To: ${d.message.to}\nSubject: ${d.message.subject}\n\n${d.message.body}`); toast("Message copied"); };
}
function renderSlip(d, animate) {
  const el = $("#slip"); if (!el) return;
  if (!d) { el.className = "surface slip"; el.innerHTML = `<div class="label">Decision</div><div class="muted" style="margin-top:4px">Appears when the last check finishes.</div><div class="skel" style="width:70%"></div><div class="skel" style="width:45%"></div>`; return; }
  el.className = `surface slip ${d.decision}`;
  el.innerHTML = slipHtml(d, animate);
  wireSlip(el, d);
}

function findings(fs) {
  if (!fs || !fs.length) return "";
  return fs.map((f) => `<div class="finding ${f.severity}"><span class="mark"></span><div><div class="ft">${esc(f.title)}<span class="rule-id">${esc(f.rule)}</span></div>${f.detail ? `<div class="fd">${esc(f.detail)}</div>` : ""}</div></div>`).join("");
}
function conf(c) {
  if (c === undefined || c === null) return "<span></span>";
  const p = Math.round(c * 100), col = c >= 0.85 ? "var(--green)" : c >= 0.7 ? "var(--amber)" : "var(--red)";
  return `<span class="conf" title="Extraction confidence"><span class="track"><i style="width:${p}%;background:${col}"></i></span>${p}%</span>`;
}

function stageDetail(stage, p) {
  const d = p.data || {}, fs = findings(p.findings);
  if (stage === "intake") return `<div class="kv">
      <span class="k">File</span><span class="v">${esc(d.filename)}</span><span></span>
      <span class="k">Size</span><span class="v">${num(Math.round((d.bytes || 0) / 102.4) / 10)} KB</span><span></span>
      <span class="k">Fingerprint</span><span class="v faint">${esc((d.sha256 || "").slice(0, 24))}…</span><span></span></div>`;
  if (stage === "read") return `<div class="kv">
      <span class="k">Method</span><span class="v">${esc({ pdf_text_layer: "Text layer of a digital PDF", ocr_tesseract: "OCR (Tesseract)", scanned_pdf: "Scanned PDF, no OCR available", image: "Image, no OCR available" }[d.method] || d.method)}</span><span></span>
      <span class="k">Pages</span><span class="v">${esc(d.pages)}</span><span></span>
      <span class="k">Characters read</span><span class="v">${num(d.chars)}</span><span></span>
      ${d.ocr_confidence ? `<span class="k">OCR confidence</span><span class="v"></span>${conf(d.ocr_confidence)}` : ""}</div>
      ${(d.ocr_variants || []).length ? `<div class="sub">OCR passes</div><table><tr><th>Pass</th><th class="r">Word confidence</th></tr>${d.ocr_variants.map((o) => `<tr><td>${esc(o.variant)}</td><td class="r">${conf(o.confidence)}</td></tr>`).join("")}</table>` : ""}
      ${d.text_preview ? `<details><summary>Show the text that was read</summary><pre class="text">${esc(d.text_preview)}</pre></details>` : ""}`;
  if (stage === "extract") {
    const f = d.fields || {}, c = d.confidence || {}, cur = f.currency || "USD";
    const row = (k, label, val) => `<span class="k">${label}</span><span class="v">${val === null || val === undefined || val === "" ? '<span class="missing">Not found</span>' : val}</span>${conf(c[k])}`;
    const cc = d.crosscheck;
    return `<p class="muted small" style="margin:0 0 6px">Read by ${esc(d.model ? "Gemini " + d.model : "the offline parser")}${d.ocr_pick ? `, using OCR pass ${esc(d.ocr_pick.chosen)}` : ""}.</p>
      <div class="kv">
        ${row("vendor_name", "Vendor", esc(f.vendor_name))}
        ${row("invoice_number", "Invoice number", f.invoice_number && esc(f.invoice_number))}
        ${row("invoice_date", "Invoice date", f.invoice_date && esc(f.invoice_date))}
        ${row("po_number", "PO reference", f.po_number && esc(f.po_number))}
        ${row("", "Document type", esc(f.document_type))}
        ${row("", "Subtotal", f.subtotal != null ? money(f.subtotal, cur) : null)}
        ${row("", "Tax", f.tax_amount != null ? money(f.tax_amount, cur) + (f.tax_inclusive ? " (included in prices)" : "") : null)}
        ${row("total", "Total", f.total != null ? `<b>${money(f.total, cur)}</b>` : null)}
      </div>
      <div class="sub">Line items</div>
      ${(d.line_items || []).length ? `<div class="tablewrap"><table><tr><th>Description</th><th class="r">Qty</th><th class="r">Unit price</th><th class="r">Amount</th></tr>
        ${d.line_items.map((l) => `<tr><td>${esc(l.description)}</td><td class="r">${num(l.quantity)}</td><td class="r">${money(l.unit_price, cur)}</td><td class="r">${money(l.amount, cur)}</td></tr>`).join("")}</table></div>` : '<span class="missing">None found</span>'}
      ${cc ? `<div class="sub">Second read</div><p class="small muted" style="margin:0">${cc.disagreements.length ? cc.disagreements.map((x) => `The local parser read ${esc(x.field)} as <b>${esc(x.parser)}</b>; Gemini read <b>${esc(x.gemini)}</b>.`).join(" ") : `The local parser agrees on ${esc(cc.compared.join(", ") || "nothing comparable")}.`}</p>` : ""}
      ${(d.notes || []).length ? `<div class="sub">Notes</div>${d.notes.map((n) => `<div class="small muted">${esc(n)}</div>`).join("")}` : ""}`;
  }
  if (stage === "vendor") return fs + (d.vendor_id ? `<div class="kv" style="margin-top:8px">
      <span class="k">On the invoice</span><span class="v">${esc(d.extracted_name)}</span><span></span>
      <span class="k">Vendor master</span><span class="v">${esc(d.vendor_name)} <span class="faint">${esc(d.vendor_id)}</span></span><span></span>
      <span class="k">Status</span><span class="v"><span class="chip ${d.status === "approved" ? "pass" : "fail"}">${esc(d.status)}</span></span><span></span>
      <span class="k">Payment terms</span><span class="v">Net ${esc(d.terms)}</span><span></span>
      <span class="k">Matched by</span><span class="v">${esc(d.method)}</span>${conf(d.match_score)}</div>` : "");
  if (stage === "duplicate") {
    const m = d.matches || [];
    return fs + (m.length ? `<div class="sub">Earlier invoices that match</div><table><tr><th>Invoice</th><th>Date</th><th class="r">Total</th><th>Where</th></tr>${m.map((h) => `<tr><td>${esc(h.invoice_number)}</td><td>${esc(h.invoice_date)}</td><td class="r">${money(h.total, h.currency)}</td><td>${esc(h.source)}</td></tr>`).join("")}</table>` : "");
  }
  if (stage === "po_match") {
    const c = d.candidates || [];
    return fs + `<div class="kv" style="margin-top:8px">
        <span class="k">On the invoice</span><span class="v">${d.reference_on_invoice ? esc(d.reference_on_invoice) : '<span class="missing">No PO reference</span>'}</span><span></span>
        <span class="k">How it was matched</span><span class="v">${esc({ explicit: "Reference printed on the invoice", inferred: "Inferred from line items and amount" }[d.method] || "Not matched")}</span><span></span>
        ${d.po ? `<span class="k">Purchase order</span><span class="v">${esc(d.po.po_number)}, ${esc(d.po.description)}, ${money(d.po.total_amount, d.po.currency)}</span><span></span>` : ""}</div>
      ${c.length ? `<div class="sub">Open POs considered</div><div class="tablewrap"><table><tr><th>PO</th><th>Description</th><th class="r">Remaining</th><th class="r">Lines matched</th><th class="r">Amount fit</th><th class="r">Score</th></tr>
        ${c.map((x, i) => `<tr${i === 0 ? ' style="font-weight:600"' : ""}><td>${esc(x.po_number)}</td><td>${esc(x.description)}</td><td class="r">${money(x.remaining)}</td><td class="r">${Math.round(x.line_coverage * 100)}%</td><td class="r">${Math.round(x.amount_fit * 100)}%</td><td class="r">${conf(x.score)}</td></tr>`).join("")}</table></div>` : ""}`;
  }
  if (stage === "amount") {
    if (!d.lines) return fs;
    const cur = d.currency || "USD", tot = d.po_total || 0, sub = d.invoice_subtotal || 0;
    const over = sub > d.remaining_before + 0.005;
    const prevW = tot ? Math.min(100, (100 * d.billed_before) / tot) : 0;
    const thisW = over ? 100 - prevW : (tot ? Math.min(100 - prevW, (100 * sub) / tot) : 0);
    return fs + `
      <div class="sub">Invoice lines against the PO</div>
      <div class="tablewrap"><table><tr><th>Invoice line</th><th>PO line</th><th class="r">Qty</th><th class="r">Billed price</th><th class="r">PO price</th><th class="r">Variance</th></tr>
      ${d.lines.map((l) => `<tr><td>${esc(l.description)}</td><td>${l.po_line ? esc(l.po_description) : '<span class="missing">Not on the PO</span>'}</td>
        <td class="r">${num(l.qty)}</td><td class="r">${money(l.unit_price, cur)}</td>
        <td class="r">${l.billing_type === "quantity" ? money(l.po_unit_price, cur) : l.billing_type === "amount" ? '<span class="faint">Milestone</span>' : "—"}</td>
        <td class="r ${Math.abs(l.variance) >= 0.01 ? "over" : "ok"}">${Math.abs(l.variance) >= 0.01 ? (l.variance > 0 ? "+" : "") + money(l.variance, cur) : "✓"}</td></tr>`).join("")}
      <tr><td colspan="5" class="r muted">Total variance, tolerance ${money(d.tolerance, cur)}</td><td class="r ${d.variance > d.tolerance ? "over" : "ok"}"><b>${d.variance > 0 ? "+" : ""}${money(d.variance, cur)}</b></td></tr></table></div>
      <div class="sub">PO balance</div>
      <div class="balance"><div class="track"><i class="prev" data-w="${prevW}%"></i><i class="this ${over ? "over" : ""}" data-w="${thisW}%"></i></div>
      <div class="legend"><span>Billed before ${money(d.billed_before, cur)}</span><span style="--c:${over ? "var(--red)" : "var(--ink)"}">This invoice ${money(sub, cur)}</span>
        <span class="nokey">${over ? `<b class="over">${money(sub - d.remaining_before, cur)} over the ${money(tot, cur)} PO</b>` : `${money(d.remaining_after, cur)} left of ${money(tot, cur)}`}</span></div></div>
      ${(d.prior_invoices || []).length ? `<p class="small muted" style="margin:10px 0 0">Earlier invoices on this PO: ${d.prior_invoices.map((p) => `${esc(p.invoice_number)} (${money(p.subtotal, cur)})`).join(", ")}</p>` : ""}`;
  }
  if (stage === "decision") return d.traceback ? `<pre class="text">${esc(d.traceback)}</pre>` : `<p class="small muted" style="margin:0">See the decision on the right.</p>`;
  return fs;
}

/* ============================ Dashboard ============================ */
async function renderDashboard(v) {
  const filt = seg("dfilter", ["ALL", "APPROVE", "HOLD", "REJECT"].map((k) => ({ k, label: `${k === "ALL" ? "All" : WORD[k]}<span class="count" data-n="${k}"></span>` })), state.filter, (k) => { state.filter = k; draw(); });
  v.innerHTML = `
    <div class="head"><div><h1>Dashboard</h1><p>Every invoice processed, what was decided and why. Select a row for a quick look.</p></div>
      <a class="btn primary" href="#/process">Process an invoice</a></div>
    <div class="surface" style="margin-bottom:22px">
      <div class="stats">
        <div class="stat"><div class="v" id="k-total">0</div><div class="l">Invoices processed</div><div class="s" id="k-prog">&nbsp;</div></div>
        <div class="stat"><div class="v" id="k-touch">0%</div><div class="l">Straight-through</div><div class="s">Approved with no human touch</div></div>
        <div class="stat approve"><div class="v" id="k-app">0</div><div class="l">Approved</div><div class="s" id="k-appv">&nbsp;</div></div>
        <div class="stat hold"><div class="v" id="k-hold">0</div><div class="l">On hold</div><div class="s" id="k-holdv">&nbsp;</div></div>
        <div class="stat reject"><div class="v" id="k-rej">0</div><div class="l">Rejected</div><div class="s" id="k-rejv">&nbsp;</div></div>
      </div>
      <div class="mix" id="mix"><i style="background:var(--green)"></i><i style="background:var(--amber)"></i><i style="background:var(--red)"></i></div>
    </div>
    <div class="surface">
      <div class="toolbar">${filt.html}<span class="spacer"></span><div class="search">${I.search}<input id="q" placeholder="Search vendor, invoice or PO" value="${esc(state.search)}" /></div></div>
      <div id="runs"></div>
    </div>`;
  filt.mount();
  $("#q").addEventListener("input", (e) => { state.search = e.target.value; draw(); });
  let runs = [];

  function draw() {
    const q = state.search.toLowerCase();
    const counts = { ALL: runs.length, APPROVE: 0, HOLD: 0, REJECT: 0 };
    runs.forEach((r) => { if (counts[r.decision] !== undefined) counts[r.decision]++; });
    $$("[data-n]").forEach((el) => (el.textContent = counts[el.dataset.n] || ""));
    const rows = runs.filter((r) => (state.filter === "ALL" || r.decision === state.filter) &&
      (!q || [r.vendor_name, r.invoice_number, r.po_number, r.filename].some((x) => (x || "").toLowerCase().includes(q))));
    if (!runs.length) { $("#runs").innerHTML = `<div class="empty"><strong>No invoices yet</strong>Process one, or run the prepared demo invoices to fill this view.<br><a class="btn" href="#/process">Go to Process</a></div>`; return; }
    if (!rows.length) { $("#runs").innerHTML = `<div class="empty"><strong>Nothing matches</strong>Try another filter or search term.</div>`; return; }
    const selected = $("#drawer").dataset.run;
    $("#runs").innerHTML = `<div class="tablewrap"><table class="runs">
      <tr><th>Vendor</th><th>Invoice</th><th>PO</th><th class="r">Amount</th><th>Decision</th><th>Reason</th><th class="r">Received</th></tr>
      ${rows.map((r) => {
        const done = r.status === "done" || r.status === "error";
        const changed = state.seen[r.id] && state.seen[r.id] !== (r.decision || r.status) && done;
        return `<tr data-id="${r.id}" class="${changed ? "flash" : ""}${selected === r.id ? " sel" : ""}">
        <td><div class="who">${esc(r.vendor_name || "Reading…")}</div><div class="file">${esc(r.filename)}</div></td>
        <td class="nw">${esc(r.invoice_number || "—")}</td><td class="nw">${esc(r.po_number || "—")}</td>
        <td class="r nw">${money(r.total, r.currency || "USD")}</td>
        <td>${done ? `<span class="chip ${r.decision}">${WORD[r.decision]}</span>` : `<span class="chip running">${r.status === "running" ? "Running" : "Queued"}</span>`}</td>
        <td class="why">${esc(done ? cleanHeadline(r.decision, r.headline) : "")}</td>
        <td class="r faint" style="white-space:nowrap">${ago(r.created_at)}</td></tr>`; }).join("")}</table></div>`;
    runs.forEach((r) => (state.seen[r.id] = r.decision || r.status));
  }
  $("#runs").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-id]"); if (tr) openDrawer(tr.dataset.id); });

  async function refresh() {
    const [s, rs] = await Promise.all([api("/api/stats"), api("/api/runs")]);
    const sig = JSON.stringify(rs.map((r) => [r.id, r.status, r.decision]));
    countUp($("#k-total"), s.total_runs); countUp($("#k-app"), s.approved); countUp($("#k-hold"), s.held); countUp($("#k-rej"), s.rejected);
    countUp($("#k-touch"), Math.round(s.touchless_rate * 100), (x) => `${Math.round(x)}%`);
    $("#k-prog").textContent = s.in_progress ? `${s.in_progress} still running` : `Average ${(s.avg_duration_ms / 1000).toFixed(1)} s each`;
    $("#k-appv").textContent = moneyMulti(s.value_approved); $("#k-holdv").textContent = moneyMulti(s.value_held); $("#k-rejv").textContent = moneyMulti(s.value_rejected) + " stopped";
    const mix = $$("#mix i"); [s.approved, s.held, s.rejected].forEach((n, i) => (mix[i].style.flexGrow = n));
    if (sig !== refresh.sig) { refresh.sig = sig; runs = rs; draw(); }
  }
  await refresh();
  state.timers.push(setInterval(refresh, 1500));
}

/* Drawer: quick look at a run */
async function openDrawer(id) {
  const dr = $("#drawer"), panel = $("#drawer-panel");
  dr.dataset.run = id; dr.classList.add("open"); dr.setAttribute("aria-hidden", "false");
  $$(".runs tr").forEach((tr) => tr.classList.toggle("sel", tr.dataset.id === id));
  panel.innerHTML = `<div class="drawer-head"><button class="icon-btn" data-close aria-label="Close">${I.x.replace("<svg", '<svg style="stroke:currentColor;fill:none;stroke-width:2"')}</button><span class="spacer"></span></div><div class="drawer-body"><div class="skel" style="width:60%;margin-top:24px"></div><div class="skel" style="width:80%"></div></div>`;
  const r = await api(`/api/runs/${id}`);
  if (dr.dataset.run !== id) return;
  const d = r.result && r.result.decision ? r.result : null;
  panel.innerHTML = `
    <div class="drawer-head"><button class="icon-btn" data-close aria-label="Close">${I.x.replace("<svg", '<svg style="stroke:currentColor;fill:none;stroke-width:2"')}</button>
      <div style="min-width:0"><div style="font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.vendor_name || r.filename)}</div><div class="faint small">${esc(r.invoice_number || r.filename)}</div></div>
      <span class="spacer"></span><a class="btn sm blue" href="#/run/${id}">Open full trace</a></div>
    <div class="drawer-body">
      ${d ? `<div class="slip ${d.decision}" style="box-shadow:none">${slipHtml(d, true)}</div>` : `<p class="muted" style="margin-top:20px">${r.status === "error" ? esc(r.headline) : "This invoice is still being processed."}</p>`}
      <img src="/api/runs/${id}/preview.png" alt="Invoice preview" data-zoom />
    </div>`;
  if (d) wireSlip(panel, d);
}
function closeDrawer() {
  const dr = $("#drawer"); if (!dr.classList.contains("open")) return;
  dr.classList.remove("open"); dr.setAttribute("aria-hidden", "true"); delete dr.dataset.run;
  $$(".runs tr.sel").forEach((tr) => tr.classList.remove("sel"));
}
$("#drawer").addEventListener("click", (e) => { if (e.target.closest("[data-close]")) closeDrawer(); });

/* Lightbox for document previews */
document.addEventListener("click", (e) => {
  const img = e.target.closest("img[data-zoom]");
  if (img) { const lb = $("#lightbox"); $("img", lb).src = img.src; lb.classList.add("show"); return; }
  if (e.target.closest("#lightbox")) $("#lightbox").classList.remove("show");
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { $("#lightbox").classList.remove("show"); closeDrawer(); $("#menu-pop").classList.remove("open"); } });

/* ============================ Purchase orders ============================ */
async function renderPOs(v) {
  const pos = await api("/api/pos");
  v.innerHTML = `
    <div class="head"><div><h1>Purchase orders</h1><p>The POs invoices are matched against. Each approved invoice draws down its PO's remaining balance. Select a PO to see its lines and invoices.</p></div></div>
    <div class="surface po-list">${pos.map((p) => {
      const pct = p.total_amount ? Math.min(100, (100 * p.billed) / p.total_amount) : 0;
      return `<div class="po" data-po="${esc(p.po_number)}">
        <div class="po-row"><div class="pn">${esc(p.po_number)}${p.status !== "open" ? `<span class="closed-tag">${esc(p.status)}</span>` : ""}</div>
          <div><div style="font-weight:550">${esc(p.vendor_name)}</div><div class="desc">${esc(p.description)}</div></div>
          <div class="bal"><div class="balance"><div class="track"><i class="this" data-w="${pct}%" style="${p.remaining <= 0.01 ? "background:#b9c1cb" : ""}"></i></div></div><div class="small faint" style="margin-top:6px">${Math.round(pct)}% billed</div></div>
          <div class="amt">${p.remaining < -0.005 ? `<b class="over">${money(-p.remaining, p.currency)} over</b>within tolerance` : `<b>${money(p.remaining, p.currency)}</b>left of ${money(p.total_amount, p.currency)}`}</div>${I.chev}</div>
        <div class="collapse"><div><div class="po-detail">
          <div class="tablewrap"><table><tr><th>Line</th><th class="r">Qty</th><th class="r">Unit price</th><th class="r">Line total</th></tr>
          ${p.lines.map((l) => `<tr><td>${esc(l.description)}</td><td class="r">${l.billing_type === "amount" ? "—" : num(l.quantity)}</td><td class="r">${l.billing_type === "amount" ? '<span class="faint">Milestone</span>' : money(l.unit_price, p.currency)}</td><td class="r">${money(l.line_total, p.currency)}</td></tr>`).join("")}</table></div>
          <p class="small muted" style="margin:10px 0 0">${p.invoices.length ? "Invoices: " + p.invoices.map((i) => `${esc(i.invoice_number)} (${money(i.subtotal, p.currency)}${i.source === "seed" ? ", from ERP history" : ""})`).join(", ") : "No invoices billed yet."}${p.tax_rate ? ` Tax rate ${(p.tax_rate * 100).toFixed(2).replace(/\.?0+$/, "")}%.` : ""}</p>
        </div></div></div>
      </div>`; }).join("")}</div>`;
  requestAnimationFrame(() => requestAnimationFrame(() => $$(".balance .track i").forEach((i) => (i.style.width = i.dataset.w))));
  $(".po-list").addEventListener("click", (e) => { const row = e.target.closest(".po-row"); if (row) row.parentElement.classList.toggle("open"); });
}

/* ============================ Rules ============================ */
async function renderRules(v) {
  const c = state.config, r = c.rules, vendors = await api("/api/vendors");
  const stageNames = Object.fromEntries(c.stages.map((s) => [s.id, s.title]));
  const groups = {}; c.rule_catalog.forEach((x) => (groups[x.stage] = groups[x.stage] || []).push(x));
  const outcome = { HOLD: "hold", REJECT: "reject" };
  v.innerHTML = `
    <div class="head"><div><h1>Rules</h1><p>The AI only reads the invoice. Every decision comes from these fixed rules, so the same invoice always gets the same answer and you can point to the rule behind it.</p></div></div>
    <div class="rules-grid">
      <div class="surface pad">${Object.entries(groups).map(([st, rs]) => `
        <div class="rule-group"><h3>${esc(stageNames[st] || st)}</h3>${rs.map((x) => `<div class="rule-line"><span class="id">${esc(x.id)}</span><span>${esc(x.title)}</span><span class="chip ${outcome[x.outcome]}">${x.outcome === "HOLD" ? "Hold" : "Reject"}</span></div>`).join("")}</div>`).join("")}</div>
      <div style="display:flex;flex-direction:column;gap:20px">
        <div class="surface pad logic"><h2 style="margin-bottom:10px">How a decision is made</h2>
          <p>If any rule says reject, the invoice is rejected.</p><p>Otherwise, if any rule says hold, it goes to the team that owns the problem.</p><p>Otherwise it is approved, and any notes are kept with the approval.</p>
          <p class="faint small" style="margin:0">A blocked vendor or an exact duplicate stops processing early, because nothing later could change the outcome.</p></div>
        <div class="surface pad"><h2 style="margin-bottom:6px">Thresholds</h2><div class="kv" style="grid-template-columns:150px 1fr">
          <span class="k">Price tolerance</span><span class="v">The lesser of ${r.tolerance_pct}% and ${r.tolerance_abs}</span>
          <span class="k">Rounding allowed</span><span class="v">${r.arithmetic_tolerance} on totals, ${r.tax_tolerance} on tax</span>
          <span class="k">Oldest invoice</span><span class="v">${r.max_invoice_age_days} days</span>
          <span class="k">Duplicate window</span><span class="v">${r.duplicate_window_days} days</span>
          <span class="k">Minimum confidence</span><span class="v">${Math.round(r.min_extraction_confidence * 100)}%</span>
          <span class="k">Inferring a PO</span><span class="v">Score at least ${r.po_inference_min_score} and ${r.po_inference_min_margin} ahead of the next PO, then ${r.auto_approve_inferred_po ? "approve" : "hold"}</span>
          <span class="k">Date checks use</span><span class="v">${esc(c.today)}</span></div></div>
        <div class="surface pad"><h2 style="margin-bottom:6px">Vendors</h2><table>${vendors.map((x) => `<tr><td>${esc(x.name)}<div class="faint small">${esc(x.vendor_id)}, net ${x.payment_terms_days}, ${esc(x.currency)}</div></td><td class="r"><span class="chip ${x.status === "approved" ? "pass" : "fail"}">${x.status === "approved" ? "Approved" : "Blocked"}</span></td></tr>`).join("")}</table></div>
      </div>
    </div>`;
}

/* ============================ Boot ============================ */
$("#menu-btn").addEventListener("click", (e) => { e.stopPropagation(); $("#menu-pop").classList.toggle("open"); });
document.addEventListener("click", (e) => { if (!e.target.closest(".menu")) $("#menu-pop").classList.remove("open"); });
$("#reset-btn").addEventListener("click", async () => {
  $("#menu-pop").classList.remove("open");
  await api("/api/reset", { method: "POST" });
  state.seen = {};
  toast("Demo data reset");
  route();
});
(async () => { await loadConfig(); route(); })();
