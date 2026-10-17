// Traceback page. Renders ONLY what is in the run state from the API.
// No framework. Polls about once a second while a run is live.

const STEPS = ["Intake", "Define done", "Repair", "Mutation audit", "Close gap", "Report"];
const PHASE_STEP = {
  READY: 1, BASELINE: 1, DEFINE_DONE: 1, VALIDATE_DEFINITION: 1,
  REPAIR: 2, RUN_CHECKS: 2,
  MUTATION_AUDIT: 3,
  GAP_CLOSURE: 4, VALIDATE_GAP: 4, FINAL_CHECKS: 4,
  REPORT: 5, COMPLETED: 5,
};
const NOW_TEXT = {
  READY: "Preparing the isolated copy",
  BASELINE: "Running baseline checks",
  DEFINE_DONE: "Waiting for the planner's definition of done",
  VALIDATE_DEFINITION: "Running the agent-written test on the original code",
  REPAIR: "Waiting for the repair agent's patch",
  RUN_CHECKS: "Testing the proposed repair",
  MUTATION_AUDIT: "Auditing whether the checks catch changes to the fix",
  GAP_CLOSURE: "Waiting for the gap-closer's test",
  VALIDATE_GAP: "Validating the added test on the repair and the variant",
  FINAL_CHECKS: "Running final checks",
  REPORT: "Assembling the report",
};
const BASIS = {
  user_goal: "From your goal",
  existing_tests: "From existing tests",
  agent_inference: "Agent inference · please confirm",
  unresolved: "Unresolved",
};

// Animated dots: shown ONLY while the run is live and the phase shown is still in progress.
const DOTS = `<span class="dots" aria-hidden="true"><span>.</span><span>.</span><span>.</span></span>`;

const S = { info: null, run: null, intake: true, view: null, timer: null,
            ev: {}, open: new Set(), lastKey: "", error: "", offline: false };

// ---------------------------------------------------------------- helpers
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const hhmm = (iso) => (iso || "").slice(11, 16);

async function api(path, opts) {
  const res = await fetch(path, opts);
  const isJson = (res.headers.get("content-type") || "").includes("json");
  const body = isJson ? await res.json() : await res.text();
  if (!res.ok) throw new Error((body && body.error) || `Request failed (${res.status})`);
  return body;
}

function isLive(r) { return !!(r && r.status === "running" && r.live); }
function interrupted(r) { return !!(r && r.status === "running" && !r.live); }

function liveStep() {
  const r = S.run;
  if (S.intake || !r || !r.run_id) return 0;
  if (!isLive(r)) return 5;                       // any final status, or interrupted
  return PHASE_STEP[r.current_phase] ?? 1;
}

// Which steps really finished (from completed_phases), so a stopped or blocked run
// never shows later steps as done.
const STEP_DONE_PHASE = [null, "VALIDATE_DEFINITION", "RUN_CHECKS", "MUTATION_AUDIT", "FINAL_CHECKS", "REPORT"];
function stepDone(i, live) {
  const r = S.run;
  if (i >= live) return false;
  if (i === 0 || isLive(r)) return true;
  return !!(r && (r.completed_phases || []).includes(STEP_DONE_PHASE[i]));
}

function result(outcome, expectFail) {
  if (!outcome) return `<span class="label soft">— Not yet run</span>`;
  if (outcome === "passed") return `<span class="label">✓ Passed</span>`;
  if (outcome === "failed") return `<span class="label">✗ Failed${expectFail ? " (expected)" : ""}</span>`;
  return `<span class="label">✗ ${esc(outcome)}</span>`;
}

function evidence(eid) {
  const r = S.run;
  if (!r || !eid) return null;
  const key = `${r.run_id}/${eid}`;
  if (key in S.ev) return S.ev[key];
  S.ev[key] = null;
  api(`/api/runs/${r.run_id}/evidence/${encodeURIComponent(eid)}`)
    .then((t) => { S.ev[key] = t; render(true); })
    .catch(() => { S.ev[key] = "(could not load this evidence file)"; render(true); });
  return null;
}

function diffHtml(text) {
  if (text == null) return `<span class="muted">Loading diff…</span>`;
  return text.split("\n")
    .filter((l) => l && !l.startsWith("---") && !l.startsWith("+++") && !l.startsWith("@@"))
    .map((l) => {
      const cls = l[0] === "-" ? "del" : l[0] === "+" ? "add" : "ctx";
      return `<span class="${cls}">${esc(l)}</span>`;
    }).join("\n");
}

function detailsOpen(id) { return S.open.has(id) ? " open" : ""; }

// ---------------------------------------------------------------- screens
function scrIntake() {
  const busy = S.info && S.info.running;
  const goal = (S.run && S.run.goal) || "";
  return `
    <h1 class="focal">What should the agent do?</h1>
    <p class="muted intake-note">Works in an isolated copy. Your original project is never changed.</p>
    <textarea id="goal" class="goal-input" maxlength="500"
      placeholder="Describe the change you want, in a sentence or two">${esc(goal)}</textarea>
    <div class="btn-row">
      <button class="btn" id="start" ${busy ? "disabled" : ""}>Start investigation</button>
    </div>
    ${S.run && S.run.run_id && !isLive(S.run) ? `<p class="label last-run">Last run: ${esc(interrupted(S.run) ? "Interrupted" : (S.run.report && S.run.report.status_label) || S.run.status)} ·
      <button class="textbtn" id="view-last">View report</button></p>` : ""}
    ${S.info && !S.info.key_loaded ? `<p class="error">The API key is not loaded in the Terminal running the server. Runs will be refused.</p>` : ""}
    ${S.error ? `<p class="error">${esc(S.error)}</p>` : ""}`;
}

function scrDefine(r) {
  const dod = r.definition_of_done;
  const gapStatements = new Set((r.gaps || []).map((g) => g.statement));
  const assumptions = (r.assumptions || []).filter((a) => !gapStatements.has(a.statement));
  const focal = dod ? "The agent wrote its definition of done."
    : isLive(r) ? "Writing the definition of done." : "No definition of done was accepted.";
  const accepted = (r.planner_attempts || []).find((a) => a.accepted);
  const rejected = (r.planner_attempts || []).filter((a) => !a.accepted && a.rejection_reason);

  let panel = "";
  if (dod) {
    const rows = [`<li><span>${esc(dod.required_behavior)}</span><span class="label">Required</span></li>`]
      .concat(dod.preservation_requirements.map((p) =>
        `<li><span>${esc(p.statement)}</span><span class="label">${esc(BASIS[p.basis] || p.basis)}</span></li>`))
      .concat(assumptions.map((a) =>
        `<li><span>${esc(a.statement)}</span><span class="label">${esc(BASIS[a.basis] || a.basis)}</span></li>`));
    panel = `<div class="panel"><span class="label">Definition of done · scope: ${esc(dod.allowed_scope.join(", "))}</span>
      <ul class="rows">${rows.join("")}</ul></div>`;
  }

  return `
    <div class="block"><span class="label">Goal</span><p>${esc(r.goal)}</p></div>
    <h1 class="focal">${focal}</h1>
    ${panel}
    <ul class="rows">
      <li><span>Existing tests on original code${r.baseline ? ` (${r.baseline.passed_count})` : ""}</span>
        ${result(r.baseline && r.baseline.outcome)}</li>
      <li><span>Agent's new test on original code${dod ? `: ${esc(dod.test_summary)}` : ""}</span>
        ${result(accepted && accepted.validation && accepted.validation.outcome, true)}</li>
    </ul>
    ${rejected.map((a) => `<p class="muted mono">Planner attempt ${a.attempt} rejected: ${esc(a.rejection_reason)}</p>`).join("")}
    <p class="muted" style="margin-top:16px">The new test must fail on the broken code before any fix is allowed.</p>`;
}

function scrRepair(r) {
  const p = r.patch;
  const accepted = (r.repair_attempts || []).find((a) => a.accepted);
  const rejected = (r.repair_attempts || []).filter((a) => !a.accepted && a.rejection_reason);
  const focal = p ? "Repair applied. All checks pass."
    : isLive(r) ? "Repairing in an isolated copy." : "No repair was accepted.";
  return `
    <h1 class="focal">${focal}</h1>
    ${p ? `<div class="panel"><span class="label">${esc(p.file)} · line${p.changed_lines.length === 1 ? "" : "s"} ${esc(p.changed_lines.join(", "))}</span>
      <pre class="diff">${diffHtml(evidence(p.diff_evidence_id))}</pre></div>` : ""}
    ${accepted ? `<ul class="rows">
      <li><span>Full suite on the repair (${accepted.checks.passed_count} tests, including the agent's test)</span>${result(accepted.checks.outcome)}</li>
    </ul>` : ""}
    ${rejected.map((a) => `<p class="muted mono">Repair attempt ${a.attempt} rejected: ${esc(a.rejection_reason)}</p>`).join("")}
    ${r.blocked_reason_agent_words ? `<p>Agent's words: “${esc(r.blocked_reason_agent_words)}”</p>` : ""}
    ${p ? `<p class="muted" style="margin-top:16px">Next: checking whether these tests would catch a wrong fix.</p>` : ""}`;
}

function scrAudit(r) {
  const a = r.audit_summary;
  if (!a) {
    return `<h1 class="focal">${isLive(r) ? "Auditing whether the checks catch changes to the fix." : "The mutation audit did not run."}</h1>`;
  }
  if (a.no_mutable_code) {
    return `<h1 class="focal">No mutable code on the changed lines.</h1>
      <p>The change could not be audited with generated variants. This is listed under what could not be checked.</p>`;
  }
  const muts = r.mutations || [];
  const valid = muts.filter((m) => m.outcome !== "invalid");
  const invalid = muts.filter((m) => m.outcome === "invalid");
  const surv = a.survived > 0 ? `<mark>${a.survived} survived</mark>` : `${a.survived} survived`;
  const row = (m) => `<li><span>${esc(m.description)}</span><span class="leader"></span>
    <span class="label">${m.outcome === "caught" ? "Caught" : m.outcome === "survived" ? "✗ Survived" : "Invalid"}</span></li>`;
  return `
    <h1 class="focal">${a.tested} variants tested · ${a.caught} caught · ${surv}</h1>
    <div class="panel"><span class="label">Variants of the changed lines</span>
      <ul class="rows mono">${valid.map(row).join("")}</ul>
      ${invalid.length ? `<span class="label" style="margin-top:16px">Invalid (tests could not run)</span>
        <ul class="rows mono">${invalid.map(row).join("")}</ul>` : ""}
    </div>
    ${a.survived > 0
      ? `<p>A variant also passed every check. It doesn't mean the fix is wrong. It means the checks can't tell the two apart yet.</p>
         ${isLive(r) ? `<p class="muted">The agent will add a stronger test. No action needed.</p>` : ""}`
      : `<p>No verification gap found among these variants. Other changes were not tested.</p>`}`;
}

function scrGap(r) {
  const a = r.audit_summary;
  const gaps = r.gaps || [];
  if (a && (a.survived === 0 || a.no_mutable_code)) {
    return `<h1 class="focal">No gap found among the tested variants.</h1>`;
  }
  if (!gaps.length) {
    return `<h1 class="focal">${isLive(r) ? "Closing the verification gap." : "No gap was closed."}</h1>`;
  }
  const allClosed = gaps.every((g) => g.status === "accepted");
  const focal = allClosed ? (gaps.length === 1 ? "Gap closed with a new test." : "Gaps closed with new tests.")
    : "Verification gap remains.";
  const panels = gaps.map((g) => {
    if (g.status === "accepted") {
      const v = g.validation || {};
      return `<div class="panel"><span class="label">Agent-written test · flagged for review</span>
        <p>${esc(g.statement)}</p>
        <ul class="rows">
          <li><span>New test on the repair</span>${result(v.on_repair && v.on_repair.outcome)}</li>
          <li><span>New test on the variant (${esc(g.survivor)})</span>${result(v.on_variant && v.on_variant.outcome, true)}</li>
          ${g.recheck ? `<li><span>Variant re-run with the full suite</span><span class="label">${esc(g.recheck.outcome)}</span></li>` : ""}
        </ul></div>`;
    }
    const tries = (r.gap_attempts || []).filter((x) => x.survivor === g.mutation_id && x.rejection_reason);
    return `<div class="panel"><span class="label">${g.status === "beyond_closing_limit" ? "Not attempted (beyond the closing limit)" : "Unresolved"}</span>
      <p>${esc(g.survivor)}</p>
      ${tries.map((x) => `<p class="muted mono">Added test ${x.attempt} rejected: ${esc(x.rejection_reason)}</p>`).join("")}
      </div>`;
  });
  return `<h1 class="focal">${focal}</h1>${panels.join("")}`;
}

function scrReport(r) {
  const rep = r.report || {};
  let label = rep.status_label || r.status;
  let summary = rep.summary || "";
  if (interrupted(r)) {
    label = "Interrupted";
    summary = `The server stopped during ${r.current_phase}. No result is claimed for this run.`;
  }
  const sym = r.status === "completed_verified_within_scope" ? "✓ "
    : ["blocked", "attempt_limit_reached", "failed"].includes(r.status) ? "✗ " : "";
  const changed = rep.what_changed || {};
  const evList = (r.evidence || []).map((e) => {
    const id = `ev-${e.id}`;
    const text = S.open.has(id) ? evidence(e.id) : null;
    return `<details id="${id}"${detailsOpen(id)}><summary class="mono">${esc(e.id)} <span class="muted">· ${esc(e.phase)}</span></summary>
      ${S.open.has(id) ? `<pre>${text == null ? "Loading…" : esc(text)}</pre>` : ""}</details>`;
  }).join("");
  const diffId = "rep-diff";
  return `
    <h1 class="focal">${sym}${esc(label)}</h1>
    <p class="muted">${esc(summary)}</p>
    ${r.blocked_reason_agent_words ? `<p>Agent's words: “${esc(r.blocked_reason_agent_words)}”</p>` : ""}
    <div class="block" style="margin-top:32px">
      <h2>What changed</h2>
      <p>${esc(changed.text || "No change was applied.")}</p>
      ${changed.diff_evidence_id ? `<details id="${diffId}"${detailsOpen(diffId)}><summary class="label">Show diff</summary>
        ${S.open.has(diffId) ? `<pre class="diff mono">${diffHtml(evidence(changed.diff_evidence_id))}</pre>` : ""}</details>` : ""}
    </div>
    <div class="block"><h2>What was checked</h2>
      <ul class="plain">${(rep.what_was_checked || []).map((x) => `<li>${esc(x)}</li>`).join("") || "<li>Nothing was checked.</li>"}</ul></div>
    <div class="block"><h2>What I couldn't check</h2>
      <ul class="plain">${(rep.what_i_couldnt_check || []).map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>
    ${(rep.assumptions || []).length ? `<div class="block"><h2>Assumptions</h2>
      <ul class="rows">${rep.assumptions.map((a) => `<li><span>${esc(a.statement)}</span><span class="label">${esc(a.basis_label)}</span></li>`).join("")}</ul></div>` : ""}
    <p class="label" style="color:var(--ink)">${esc(rep.integrity || "")} · Model calls: ${esc(rep.model_calls || "")}</p>
    <div class="btn-row" style="margin-top:24px">
      <button class="btn quiet" id="toggle-ev">View evidence</button>
      <button class="btn quiet" id="new-run" ${isLive(r) ? "disabled" : ""}>New run</button>
    </div>
    <div class="evidence" id="ev-list" ${S.open.has("ev-list") ? "" : "hidden"}>
      <p class="label" style="margin-top:24px">Evidence · ${(r.evidence || []).length} files (real command output and model replies)</p>
      ${evList}
    </div>`;
}

function logPanel(r) {
  const d = r.decisions || [];
  if (!d.length) return "";
  const line = (x) => `<div class="log-line"><span class="t">${hhmm(x.time)}</span><span class="d">${esc(x.decision)}</span><span>${esc(x.reason)}</span></div>`;
  return `<div class="log"><span class="label">Decision log · latest</span>
    ${d.slice(-3).map(line).join("")}
    ${d.length > 3 ? `<details id="log-all"${detailsOpen("log-all")}><summary class="label">Show all ${d.length}</summary>${d.map(line).join("")}</details>` : ""}
  </div>`;
}

// ---------------------------------------------------------------- render
function render(force) {
  const r = S.run;
  const key = JSON.stringify([S.intake, S.view, r && r.updated_at, r && r.status, r && r.live, S.error, S.info && S.info.running, S.offline]);
  if (!force && key === S.lastKey) return;
  S.lastKey = key;

  const live = liveStep();
  const shown = S.view ?? live;

  $("track").innerHTML = STEPS.map((name, i) => {
    const done = stepDone(i, live);
    const cls = [done ? "done" : "", i === live ? "current" : "", i === shown && S.view !== null ? "viewing" : ""].join(" ");
    return `<button class="step ${cls}" data-step="${i}" ${done ? "" : "tabindex='-1'"}>
      <span class="nodebox"><span class="node"></span></span><span>${String(i + 1).padStart(2, "0")} ${esc(name)}</span></button>`;
  }).join("");

  const stopping = isLive(r) && (r.stop_requested || S.stopping);
  $("now").innerHTML = S.offline ? "Connection lost. Retrying…"
    : !isLive(r) ? ""
    : stopping ? `● Stop requested · ending after the current step${DOTS}`
    : `● ${esc(NOW_TEXT[r.current_phase] || r.current_phase)}${DOTS}`;
  $("back-live").hidden = S.view === null;
  $("stop").hidden = !(isLive(r) && live >= 1 && live <= 4) || stopping;

  let html;
  if (shown === 0) html = scrIntake();
  else if (shown === 1) html = scrDefine(r);
  else if (shown === 2) html = scrRepair(r);
  else if (shown === 3) html = scrAudit(r);
  else if (shown === 4) html = scrGap(r);
  else html = scrReport(r);
  const res = r && r.resumed && r.resumed.length ? r.resumed[r.resumed.length - 1] : null;
  if (res && shown !== 0) {
    html = `<div class="panel"><span class="label">Resumed from phase: ${esc(res.phase)}</span>
      <p class="muted">Completed before the interruption: ${esc(res.completed_phases.join(", ") || "none")}</p></div>` + html;
  }
  if (shown !== 0 && r) html += logPanel(r);
  $("screen").innerHTML = html;
  if (isLive(r) && shown === live) {
    const focal = $("screen").querySelector(".focal");
    if (focal) focal.insertAdjacentHTML("beforeend", DOTS);
  }

  if (S.info) {
    const m = S.info.models;
    const short = (id) => String(id).split("/").pop();
    $("fixture-label").textContent = `Sample project (test input): ${S.info.fixture_name}`;
    $("foot").textContent = `Workspace: isolated copy · Models: ${short(m.planner)} (planner, repair), ${short(m.gap)} (gap-closer) via Crusoe Managed Inference`;
  }
  wire();
}

function wire() {
  const start = $("start");
  if (start) {
    start.onclick = startRun;
    $("goal").onkeydown = (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) startRun(); };
  }
  const vl = $("view-last");
  if (vl) vl.onclick = () => { S.intake = false; S.view = null; render(true); };
  const nr = $("new-run");
  if (nr) nr.onclick = () => { S.intake = true; S.view = null; S.error = ""; refreshInfo().then(() => render(true)); };
  const tg = $("toggle-ev");
  if (tg) tg.onclick = () => { S.open.has("ev-list") ? S.open.delete("ev-list") : S.open.add("ev-list"); render(true); };
  document.querySelectorAll("#screen details").forEach((d) => {
    d.ontoggle = () => {
      const was = S.open.has(d.id);
      if (d.open) S.open.add(d.id); else S.open.delete(d.id);
      if (was !== d.open) render(true);
    };
  });
  document.querySelectorAll(".step.done").forEach((b) => {
    b.onclick = () => { S.view = Number(b.dataset.step); render(true); };
  });
}

// ---------------------------------------------------------------- actions
async function refreshInfo() {
  try { S.info = await api("/api/info"); } catch (e) { /* server unreachable */ }
}

async function startRun() {
  const goal = ($("goal").value || "").trim();
  S.error = "";
  try {
    const { run_id } = await api("/api/runs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ goal }),
    });
    S.intake = false; S.view = null; S.open.clear(); S.stopping = false;
    S.run = await api(`/api/runs/${run_id}`);
    await refreshInfo();
    render(true);
    poll();
  } catch (e) {
    S.error = e.message;
    render(true);
  }
}

async function stopRun() {
  if (!S.run) return;
  S.stopping = true;
  render(true);
  try { await api(`/api/runs/${S.run.run_id}/stop`, { method: "POST" }); } catch (e) { /* already ended */ }
}

async function poll() {
  clearTimeout(S.timer);
  if (!S.run) return;
  try {
    S.run = await api(`/api/runs/${S.run.run_id}`);
    S.offline = false;
    if (!isLive(S.run)) await refreshInfo();
    render();
  } catch (e) {
    // Server unreachable (for example, restarting): keep the last state and retry every 2 seconds.
    S.offline = true;
    render();
    S.timer = setTimeout(poll, 2000);
    return;
  }
  if (isLive(S.run)) S.timer = setTimeout(poll, 1000);
}

$("back-live").onclick = () => { S.view = null; render(true); };
$("stop").onclick = stopRun;

(async function init() {
  await refreshInfo();
  try {
    const latest = await api("/api/runs/latest");
    if (latest && latest.run_id) {
      S.run = latest;
      S.intake = !isLive(latest);   // a live run is followed; a finished one opens Intake with a link to it
    }
  } catch (e) { /* no runs yet */ }
  render(true);
  if (isLive(S.run)) poll();
})();
