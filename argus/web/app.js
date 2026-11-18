"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ROWS = "ABCDEFGH";
const md = (t) => esc(t).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`([^`]+)`/g, '<code class="mono">$1</code>');
let runId = null, source = null, sopText = "", toolEls = {}, lastProgress = null, review = null;

// ---------------------------------------------------------------- tabs
document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
function showTab(name) {
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
}

// ---------------------------------------------------------------- start
fetch("/api/defaults").then((r) => r.json()).then((d) => {
  if (!$("prompt").value) $("prompt").value = d.prompt;
  if (!$("sop").value) { $("sop").value = d.sop_text; $("sopName").textContent = d.sop_name; }
  $("cost").textContent = "agent: " + d.model;
});
$("sopFile").addEventListener("change", async (e) => {
  const f = e.target.files[0]; if (!f) return;
  $("sop").value = await f.text(); $("sopName").textContent = f.name;
});
$("form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("go").disabled = true;
  const body = { prompt: $("prompt").value, sop_text: $("sop").value, sop_name: $("sopName").textContent || "pasted SOP", offline: $("offline").checked };
  const r = await fetch("/api/runs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) { alert((await r.json()).detail || "Could not start"); $("go").disabled = false; return; }
  const { id } = await r.json();
  location.hash = id;
  attach(id, body);
});
if (location.hash.length > 1) {
  const id = location.hash.slice(1);
  fetch("/api/runs/" + id).then((r) => (r.ok ? r.json() : null)).then((s) => {
    if (s) { $("prompt").value = s.prompt; $("sop").value = s.sop_text; $("sopName").textContent = s.sop_name; attach(id, s); }
  });
}

function attach(id, body) {
  runId = id; sopText = body.sop_text; toolEls = {}; lastProgress = null;
  $("runId").textContent = id;
  $("timeline").innerHTML = "";
  addLi(`<div class="msg user">${esc(body.prompt)}</div>`);
  if (source) source.close();
  source = new EventSource(`/api/runs/${id}/events?after=-1`);
  source.onmessage = (m) => handle(JSON.parse(m.data));
}

// ---------------------------------------------------------------- events
function addLi(html) {
  const li = document.createElement("li"); li.innerHTML = html; $("timeline").appendChild(li);
  const box = $("timeline").parentElement; box.scrollTop = box.scrollHeight; return li;
}
function handle(e) {
  const d = e.data || {};
  switch (e.type) {
    case "agent_text": lastProgress = null; addLi(`<div class="msg">${md(d.text)}</div>`); break;
    case "agent_turn": if (e.i > 0 && d.message && !d.message.includes("<sop>")) addLi(`<div class="msg user">${esc(d.message)}</div>`); break;
    case "tool_start": {
      lastProgress = null;
      const li = addLi(`<div class="tool"><span class="spin"></span><span class="name">${esc(d.name)}</span><span class="state">running…</span></div>`);
      toolEls[d.id] = li; break;
    }
    case "tool_end": {
      const li = toolEls[d.id]; if (!li) break;
      const head = li.querySelector(".tool");
      head.querySelector(".spin").outerHTML = d.ok ? '<span class="tick">✓</span>' : '<span class="cross">✕</span>';
      head.querySelector(".state").textContent = d.seconds + " s";
      if (d.summary) li.insertAdjacentHTML("beforeend", `<div class="tool-sum">${esc(d.summary)}</div>`);
      if (d.error) li.insertAdjacentHTML("beforeend", `<div class="tool-err">${esc(d.error)}</div>`);
      lastProgress = null; break;
    }
    case "progress": {
      const open = Object.values(toolEls).filter((li) => li.querySelector(".spin")).pop();
      if (!open) break;
      if (!lastProgress) { lastProgress = document.createElement("div"); lastProgress.className = "progress"; open.appendChild(lastProgress); }
      lastProgress.textContent += d.line + "\n"; lastProgress.scrollTop = lastProgress.scrollHeight; break;
    }
    case "ahc_call": addCall(d); break;
    case "ahc_view": showView(d.url); break;
    case "panel": renderPanel(d.key, d.value); break;
    case "status": setStatus(d.status); review = d.review; renderReviewState(); break;
    case "review_requested": addLi(`<div class="event-note">Waiting for your review → tab 2</div>`); review = d.review; renderReviewState(); showTab("review"); break;
    case "review_decided": review = d; renderReviewState(); break;
    case "error": addLi(`<div class="tool-err">${esc(d.message)}</div>`); break;
    case "turn_done": $("go").disabled = false; refreshCost(); break;
  }
}
function setStatus(s) {
  const el = $("status"); el.textContent = s.replace(/_/g, " ");
  el.className = "pill " + ({ done: "ok", awaiting_review: "warn", simulation_failed: "bad", error: "bad", not_ready: "bad" }[s] || "");
}
function refreshCost() {
  if (!runId) return;
  fetch("/api/runs/" + runId).then((r) => r.json()).then((s) => { if (s.state.cost_usd) $("cost").textContent = "agent cost $" + s.state.cost_usd.toFixed(3); });
}

// ---------------------------------------------------------------- panels
function renderPanel(key, v) {
  if (key === "variants") renderVariants(v);
  else if (key === "plate_map") renderPlateMap(v);
  else if (key === "protocol") renderProtocol(v);
  else if (key === "simulation") renderSimulation(v);
}
let variantsData = null;
function renderVariants(v) {
  variantsData = v;
  $("variantsEmpty").classList.add("hidden"); $("variantsBody").classList.remove("hidden");
  $("variantsTitle").textContent = `Top ${v.variants.length} LanM variants · ${v.target} over ${v.competitor}`;
  $("variantsMeta").textContent = `${v.eligible_count} candidates ranked`;
  const max = Math.max(...v.variants.map((x) => x.contrast));
  $("variantsTable").innerHTML = `<tr><th>#</th><th>Variant</th><th>Organism</th><th class="num">Contrast</th><th style="width:22%"></th><th class="num">Start conc.</th></tr>` +
    v.variants.map((x) => `<tr data-id="${esc(x.candidate_id)}"><td>${x.rank}</td><td class="mono">${esc(x.name)}</td><td>${esc(x.organism || "")}</td><td class="num">${x.contrast.toFixed(3)}</td><td><div class="bar" style="width:${(100 * x.contrast / max).toFixed(1)}%"></div></td><td class="num start">—</td></tr>`).join("");
  $("variantsNote").textContent = v.rationale || "";
  showTab("variants");
}
function concColor(v, lo, hi) {
  const t = hi > lo ? (Math.log10(v) - Math.log10(lo)) / (Math.log10(hi) - Math.log10(lo)) : 1;
  return `color-mix(in oklab, var(--accent) ${Math.round(12 + 70 * t)}%, var(--card))`;
}
function fmt(uM) {
  if (uM == null) return "–";
  if (uM >= 1) return uM.toPrecision(3).replace(/\.?0+$/, "") + " µM";
  if (uM >= 1e-3) return (uM * 1e3).toPrecision(3).replace(/\.?0+$/, "") + " nM";
  if (uM >= 1e-6) return (uM * 1e6).toPrecision(3).replace(/\.?0+$/, "") + " pM";
  return uM.toExponential(1) + " µM";
}
function renderPlateMap(pm) {
  const cols = pm.columns;
  const all = pm.rows.flatMap((r) => [r.start_uM, ...r.nominal_uM]);
  const lo = Math.min(...all), hi = Math.max(...all);
  const g = $("plateMap");
  g.style.gridTemplateColumns = `150px 80px repeat(${cols.length}, minmax(64px, 1fr))`;
  let html = `<div></div><div class="pm-head">stock</div>` + cols.map((c) => `<div class="pm-head">col ${c}</div>`).join("");
  for (const r of pm.rows) {
    html += `<div class="pm-row">${r.row} · ${esc(r.name)}<span>contrast ${Number(r.contrast).toFixed(3)}</span></div>`;
    html += `<div class="pm-cell" style="background:${concColor(r.start_uM, lo, hi)};font-weight:700">${fmt(r.start_uM)}</div>`;
    html += r.nominal_uM.map((v) => `<div class="pm-cell" style="background:${concColor(v, lo, hi)}">${fmt(v)}</div>`).join("");
  }
  g.innerHTML = html;
  $("pmFormula").textContent = `${pm.params.c_max_uM}→${pm.params.c_min_uM} µM · 1:${pm.params.dilution_factor} · ${pm.params.n_points} pts`;
  $("pmNote").textContent = pm.note + " " + pm.formula + ".";
  if (variantsData) pm.rows.forEach((r) => {
    const tr = document.querySelector(`#variantsTable tr[data-id="${CSS.escape(r.candidate_id)}"] .start`);
    if (tr) tr.textContent = `${fmt(r.start_uM)} (row ${r.row})`;
  });
  window.__plateMap = pm;
}

function renderProtocol(p) {
  $("reviewEmpty").classList.add("hidden"); $("reviewBody").classList.remove("hidden");
  if (p.status === "NOT_READY") {
    $("steps").innerHTML = `<li><div class="t">PreCheck could not make this protocol pass the simulator.</div><ul><li>${esc(p.failure)}</li></ul></li>`;
    $("protoMeta").textContent = ""; return;
  }
  $("protoTitle").textContent = p.protocol_name || "Automated steps";
  const blocked = $("blockedBanner");
  if (blocked) blocked.remove();
  if (p.ahc_blocked) $("reviewBody").insertAdjacentHTML("afterbegin", `<div id="blockedBanner" class="banner bad">Not ready for review: ${esc(p.failure)}</div>`);
  $("protoMeta").textContent = `${p.readable_steps.length} steps · ${p.n_ir_steps} robot actions · ${p.status}${p.offline ? " · offline fixture" : ""}`;
  const pm = window.__plateMap;
  if (pm) {
    $("assayCtx").classList.remove("hidden");
    $("assayCtx").textContent = "PreCheck also got the TEA gate's plate map: one variant per row\n" + pm.rows.map((r) => `  ${r.row}: ${r.name} at ${fmt(r.start_uM)}`).join("\n");
  }
  $("steps").innerHTML = p.readable_steps.map((s) => `
    <li>
      <span class="chip irs">IR ${s.ir_steps[0]}–${s.ir_steps[1]}</span>
      <div class="t">${esc(s.title)}</div>
      ${s.sop ? `<blockquote class="sopq"><span class="muted">SOP:</span> “${esc(sopText.slice(s.sop.start, s.sop.end).trim())}”</blockquote>` : ""}
      <ul>${s.details.map((d) => `<li>${esc(d)}</li>`).join("")}</ul>
      ${s.concentrations.length ? `<div class="conc">${s.concentrations.map((c) => `<span class="chip" title="${esc(Object.entries(c.by_row).map(([r, v]) => r + ": " + v).join(", "))}">col ${c.column}: ${esc(c.range)}</span>`).join("")}</div>` : ""}
    </li>`).join("");
  const flags = p.needs_review || [];
  $("flagCount").textContent = `${flags.filter((f) => f.severity === "must").length} must · ${flags.filter((f) => f.severity !== "must").length} check`;
  $("flags").innerHTML = flags.map((f) => `<li class="${f.severity === "must" ? "must" : "check"}"><span class="sev">${esc(f.severity)}</span><b>${esc(f.item)}</b> — ${esc(f.detail)}</li>`).join("") || "<li>No flags.</li>";
  $("assumptions").innerHTML = (p.assumptions || []).map((a) => `<li>${esc(a)}</li>`).join("");
  const setup = p.deck_setup || [];
  $("setupBox").classList.toggle("hidden", !setup.length);
  $("setup").innerHTML = setup.map((s) => `<li>${esc(s)}</li>`).join("");
  const notes = p.model_notes || [];
  $("notesBox").classList.toggle("hidden", !notes.length);
  $("notesCount").textContent = `(${notes.length})`;
  $("modelNotes").innerHTML = notes.map((n) => `<li>${esc(n)}</li>`).join("");
  renderLabwareCheck(p.labware_check);
  $("labware").innerHTML = `<tr><th>Name</th><th>Labware</th><th>Where</th><th>Holds</th></tr>` + (p.labware || []).map((l) => `<tr><td class="mono">${esc(l.name)}</td><td>${esc(l.type)}</td><td>${esc(l.where)}</td><td>${esc(l.holds || "")}</td></tr>`).join("");
  $("layoutYaml").textContent = p.ahc_layout_yaml || "";
  const rep = [p.changes, p.repairs].filter(Boolean).join("\n\n");
  $("repairBox").classList.toggle("hidden", !rep); $("repairs").textContent = rep;
  $("codeEmpty").classList.add("hidden"); $("codeBody").classList.remove("hidden");
  $("code").textContent = p.plr_script || ""; $("codePath").textContent = (p.files || {}).script || "";
  renderReviewState();
}
function renderLabwareCheck(lc) {
  const card = $("labwareCard");
  if (!lc || !lc.rounds || !lc.rounds.length) { card.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  const last = lc.rounds[lc.rounds.length - 1];
  const st = $("labwareState");
  const ran = last.dry_run && last.dry_run.ok;
  st.textContent = lc.clean ? "AHC runs it" : ran ? "runs, with open items" : "AHC refuses it";
  st.className = "pill " + (lc.clean ? "ok" : ran ? "warn" : "bad");
  const depth = {};
  (last.depth || []).forEach((d) => { depth[d.labware] = d; });
  $("roles").innerHTML = `<tr><th>Role</th><th>Lab labware (VENUS)</th><th>AHC loads it as</th><th>Where</th><th>Liquid depth</th></tr>` +
    last.roles.map((r) => {
      const d = depth[r.role];
      const dep = d ? `<span class="${d.shallow ? "cross" : "tick"}">${d.start_mm} → ${d.end_mm} mm</span> <span class="muted">(${d.loaded_ul} µL)</span>` : "";
      return `<tr><td class="mono">${esc(r.role)}</td><td>${esc(r.venus_type || "–")}</td><td class="mono">${r.ahc_type ? esc(r.ahc_type) : '<span class="cross">not loadable</span>'}</td><td>${esc(r.where || "")}</td><td>${dep}</td></tr>`;
    }).join("");
  $("labwareRounds").innerHTML = lc.rounds.map((r) => `
    <li><b>Round ${r.round}</b> <span class="muted small mono">${esc(r.version)}</span> —
      ${!r.ahc_loaded ? `<span class="cross">AHC refused the deck: ${esc(r.ahc_refusal)}</span>`
        : r.dry_run && r.dry_run.ok ? `<span class="tick">AHC ran every step (${r.dry_run.calls} calls)</span>`
        : `<span class="cross">AHC refused IR step ${esc((r.dry_run.refusal || {}).ir_step)} after ${r.dry_run.calls} calls</span><div class="small mono">${esc((r.dry_run.refusal || {}).message || "")}</div>`}
      ${r.suggestion ? `<div class="small"><b>AHC suggests:</b> ${esc(r.suggestion.text)}</div>` : ""}
      ${(r.design_problems || []).length ? `<div class="small"><b>TEA plate map / robot work:</b><ul class="plain small">${r.design_problems.map((d) => `<li class="cross">${esc(d)}</li>`).join("")}</ul></div>` : ""}
      ${r.not_on_layout.length ? `<ul class="plain small">${r.not_on_layout.map((x) => `<li class="cross">${esc(x)}</li>`).join("")}</ul>` : ""}
      <ul class="plain small">${r.actions.map((a) => `<li>${esc(a)}</li>`).join("")}</ul>
    </li>`).join("");
  const acc = lc.accepts || {};
  $("accepts").textContent = `carriers: ${(acc.carriers || []).join(", ")}\nlabware: ${(acc.labware || []).join(", ")}`;
}
function renderReviewState() {
  if (!review) return;
  const b = $("reviewBanner"), dec = $("decision");
  $("reviewDot").classList.toggle("hidden", review.state !== "pending");
  b.classList.remove("hidden", "pending", "ok", "info");
  if (review.state === "pending") { b.classList.add("pending"); b.textContent = "Your review is needed: compare the SOP with the steps, check the flags, then approve or ask for changes."; dec.classList.remove("hidden"); }
  else if (review.state === "approved") { b.classList.add("ok"); b.textContent = `Approved by ${review.reviewer}${review.note ? " — " + review.note : ""}. Sent to AHC's simulator.`; dec.classList.add("hidden"); }
  else if (review.state === "changes_requested") { b.classList.add("info"); b.textContent = `Changes requested by ${review.reviewer}: "${review.note}". The agent is revising.`; dec.classList.add("hidden"); }
  else { b.classList.add("hidden"); dec.classList.add("hidden"); }
}
async function decide(decision) {
  const body = { decision, reviewer: $("reviewer").value, note: $("note").value };
  $("decisionMsg").textContent = "";
  const r = await fetch(`/api/runs/${runId}/review`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) { $("decisionMsg").textContent = (await r.json()).detail; return; }
  try { localStorage.setItem("argus.reviewer", body.reviewer); } catch (_) {}
  $("note").value = "";
  if (decision === "approve") showTab("simulation");
}
$("approve").addEventListener("click", () => decide("approve"));
$("revise").addEventListener("click", () => decide("revise"));
try { $("reviewer").value = localStorage.getItem("argus.reviewer") || ""; } catch (_) {}

// ---------------------------------------------------------------- simulation
let callN = 0;
function addCall(c) {
  $("simEmpty").classList.add("hidden"); $("simBody").classList.remove("hidden");
  if (callN === 0) $("calls").innerHTML = `<tr><th>#</th><th>AHC tool</th><th>Arguments</th><th>IR step</th><th></th></tr>`;
  callN++;
  const args = Object.entries(c.args || {}).filter(([k]) => !["device", "plan_step"].includes(k)).map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join(" ");
  $("calls").insertAdjacentHTML("beforeend", `<tr><td>${callN}</td><td class="mono">${esc(c.tool)}</td><td class="mono">${esc(args).slice(0, 160)}</td><td>${c.ir_step ?? ""}</td><td>${c.ok ? '<span class="tick">✓</span>' : '<span class="cross">✕</span>'}</td></tr>`);
  $("callCount").textContent = callN;
  if (!c.ok) $("calls").insertAdjacentHTML("beforeend", `<tr><td></td><td colspan="4" class="tool-err">${esc(c.error)}</td></tr>`);
}
function showView(url) {
  $("simEmpty").classList.add("hidden"); $("simBody").classList.remove("hidden");
  $("viewCard").classList.remove("hidden");
  $("viewLink").href = url;
  if ($("view3d").getAttribute("src") !== url) $("view3d").setAttribute("src", url);
  showTab("simulation");
}
function renderSimulation(s) {
  $("simEmpty").classList.add("hidden"); $("simBody").classList.remove("hidden");
  $("simSummary").classList.remove("hidden");
  if (s.view_url) showView(s.view_url);
  const comp = s.comparison;
  $("simSummary").innerHTML = `
    <div class="row-between"><h2>${s.passed ? "Simulation passed" : "Simulation did not pass"}</h2><span class="pill ${s.passed ? "ok" : "bad"}">${s.passed ? "PASS" : "FAIL"}</span></div>
    <div class="muted small">${esc(s.device)} · ${s.seconds}s · results are simulated, not measured</div>
    <div class="stat-row">
      <div class="stat"><b>${s.log.length}</b><span class="muted small">AHC calls</span></div>
      <div class="stat"><b>${comp.checked - comp.mismatches}/${comp.checked}</b><span class="muted small">wells match PreCheck</span></div>
      <div class="stat"><b>${esc((s.volumes_match || {}).verdict || "–")}</b><span class="muted small">AHC volumes_match</span></div>
      <div class="stat"><b>${(s.skipped || []).length}</b><span class="muted small">steps not run on AHC</span></div>
    </div>
    ${s.refusal ? `<div class="refusal"><b>AHC refused ${esc(s.refusal.tool)}</b>\n${esc(s.refusal.message)}</div>` : ""}
    ${s.deck_check ? `<details><summary>Deck check recorded from your approval</summary><p class="small">${esc(s.deck_check)}</p></details>` : ""}`;
  const by = {};
  comp.wells.forEach((w) => { by[w.well.split(":")[1]] = w; });
  let html = `<div></div>` + Array.from({ length: 12 }, (_, i) => `<div class="vg h">${i + 1}</div>`).join("");
  for (const r of ROWS) {
    html += `<div class="vg h">${r}</div>`;
    for (let c = 1; c <= 12; c++) {
      const w = by[r + c];
      html += w ? `<div class="vg ${w.ok ? "ok" : "bad"}" title="expected ${w.expected_ul} µL, AHC ${w.simulated_ul ?? "–"} µL">${w.simulated_ul ?? "–"}</div>` : `<div class="vg"></div>`;
    }
  }
  $("volGrid").innerHTML = html;
  showTab("simulation");
}
