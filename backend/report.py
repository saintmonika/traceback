"""Report assembly. Built only from state data, with template wording.
Status words come only from the templates below; models never write them."""

from . import config

STATUS_LABELS = {
    "completed_verified_within_scope": "Verified within scope",
    "completed_with_uncertainty": "Verification gap remains",
    "blocked": "Blocked",
    "attempt_limit_reached": "Attempt limit reached",
    "stopped": "Stopped",
    "failed": "Failed",
    "running": "Running",
}

BASIS_LABELS = {
    "user_goal": "From your goal",
    "existing_tests": "From existing tests",
    "agent_inference": "Agent inference: please confirm",
    "unresolved": "Unresolved",
}


def _plural(n, one, many):
    return one if n == 1 else many


def _last_stop_reason(d):
    for x in reversed(d.get("decisions", [])):
        if x["decision"] == "stop":
            return x["reason"]
    return ""


def summary_sentence(d):
    status = d["status"]
    a = d.get("audit_summary") or {}
    gaps = d.get("gaps", [])
    closed = sum(g["status"] == "accepted" for g in gaps)
    unresolved = sum(g["status"] != "accepted" for g in gaps)
    t, c, s = a.get("tested", 0), a.get("caught", 0), a.get("survived", 0)

    if status in ("completed_verified_within_scope", "completed_with_uncertainty"):
        base = "Checks passed and the agent's test was validated"
        if a.get("no_mutable_code"):
            return base + ", but there was no mutable code on the changed lines, so the change itself was not audited."
        if t == 0:
            return base + ", but every generated variant was invalid, so the change itself was not audited."
        if s == 0:
            return (f"Checks passed, the agent's test was validated, {t} {_plural(t, 'variant was', 'variants were')} "
                    f"tested, and all {c} {_plural(c, 'was', 'were')} caught.")
        if unresolved == 0:
            return (f"Checks passed, the agent's test was validated, {t} variants were tested, {c} caught, "
                    f"{s} survived and {_plural(s, 'was', 'were')} closed by "
                    f"{_plural(closed, 'an added test', 'added tests')}.")
        return (f"Checks passed and the agent's test was validated. {t} variants were tested, {c} caught, "
                f"{s} survived; {unresolved} {_plural(unresolved, 'remains', 'remain')} unresolved, "
                f"so a verification gap remains.")
    if status == "blocked":
        return f"The run was blocked. {_last_stop_reason(d)}"
    if status == "attempt_limit_reached":
        return f"The run reached an attempt limit. {_last_stop_reason(d)}"
    if status == "stopped":
        return f"The run was stopped by the user during {d.get('current_phase')}. No result is claimed."
    if status == "failed":
        return f"The run ended with an unexpected error: {d.get('error', 'unknown')}."
    return "The run has not finished."


def build_report(d):
    patch = d.get("patch")
    dod = d.get("definition_of_done")
    a = d.get("audit_summary") or {}
    gaps = d.get("gaps", [])

    if patch:
        what_changed = {
            "text": f"{patch['file']}, changed {_plural(len(patch['changed_lines']), 'line', 'lines')} "
                    f"{', '.join(str(n) for n in patch['changed_lines'])} (in the isolated copy only)",
            "diff_evidence_id": patch["diff_evidence_id"],
        }
    else:
        what_changed = {"text": "No change was applied.", "diff_evidence_id": None}

    checked = []
    if d.get("baseline"):
        b = d["baseline"]
        checked.append(f"Existing suite on the original code: {b['outcome']} ({b['passed_count']} passed)")
    if dod:
        checked.append(f"Agent's test ({dod['done_test_file']}): {dod['test_summary']} "
                       f"It failed on the original code, as required.")
    final = d.get("final_checks")
    if final:
        checked.append(f"Full suite on the repaired copy: {final['outcome']} ({final['passed_count']} passed)")
    if a and not a.get("no_mutable_code"):
        checked.append(f"{a.get('tested', 0)} variants of the changed lines tested: "
                       f"{a.get('caught', 0)} caught, {a.get('survived', 0)} survived"
                       + (f", {a['invalid']} invalid" if a.get("invalid") else ""))
    for g in gaps:
        if g["status"] == "accepted":
            checked.append(f"Added test: {g['statement']} (agent-written, flagged for review)")

    couldnt = [
        "Variants outside the changed lines.",
        "Other possible wrong fixes not covered by the tested variants.",
        "Behavior outside the sample project.",
    ]
    for g in gaps:
        if g["status"] != "accepted":
            couldnt.append(f"Unresolved: {g['survivor']}")
    for m in d.get("mutations", []):
        if m["outcome"] == "invalid":
            couldnt.append(f"Invalid variant (tests could not run: {m['test_outcome']}): {m['description']}")
    if a.get("no_mutable_code"):
        couldnt.append("No mutable code on the changed lines.")

    assumptions = [{"statement": x["statement"], "basis": x["basis"],
                    "basis_label": BASIS_LABELS.get(x["basis"], BASIS_LABELS["agent_inference"])}
                   for x in d.get("assumptions", [])]

    if d.get("fixture_unchanged") is True:
        integrity = "Original project unchanged"
    elif d.get("fixture_unchanged") is False:
        integrity = "Warning: the original project folder changed during the run"
    else:
        integrity = "Original project not checked"

    return {
        "status_label": STATUS_LABELS.get(d["status"], d["status"]),
        "summary": summary_sentence(d),
        "what_changed": what_changed,
        "what_was_checked": checked,
        "what_i_couldnt_check": couldnt,
        "assumptions": assumptions,
        "integrity": integrity,
        "model_calls": f"{d.get('model_calls_used', 0)} of {config.MAX_MODEL_CALLS}",
    }