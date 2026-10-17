"""Run one investigation from Terminal (used while building).

Usage, from the repository root (with the API key loaded):
    python3 -m backend.cli "your one-line goal"
"""

import sys
import time

from .controller import Controller
from .crusoe_client import api_key_present, MISSING_KEY_MESSAGE


def show(title):
    print(f"\n=== {title} ===")


def main(argv):
    if not argv or not argv[0].strip():
        print('Usage: python3 -m backend.cli "your one-line goal"')
        return 2
    if not api_key_present():
        print(MISSING_KEY_MESSAGE)
        return 2

    start = time.monotonic()
    c = Controller(argv[0], log=print)
    print(f"Run: {c.run_id}\nRun folder: {c.folder.root}")
    d = c.run()
    elapsed = round(time.monotonic() - start, 1)

    show("Definition of done")
    dod = d.get("definition_of_done")
    if dod:
        print(f"Required behavior: {dod['required_behavior']}")
        print(f"Test summary: {dod['test_summary']}")
        print(f"Allowed scope: {', '.join(dod['allowed_scope'])}")
        for p in dod["preservation_requirements"]:
            print(f"Preserve: {p['statement']} [{p['basis']}]")
        for a in d["assumptions"]:
            print(f"Assumption: {a['statement']} [{a['basis']}]")
    else:
        print("(none accepted)")

    show("Planner attempts")
    for a in d["planner_attempts"]:
        v = a.get("validation") or {}
        print(f"#{a['attempt']}: accepted={a['accepted']}, test on original={v.get('outcome')}, "
              f"rejected because: {a['rejection_reason']}")

    show("Repair attempts")
    for a in d["repair_attempts"]:
        ch = a.get("checks") or {}
        print(f"#{a['attempt']}: file={a['file']}, accepted={a['accepted']}, checks={ch.get('outcome')}, "
              f"rejected because: {a['rejection_reason']}")
        print(f"   agent reasoning: {a.get('reasoning')}")
    if d.get("blocked_reason_agent_words"):
        print(f"Agent's words (blocked): {d['blocked_reason_agent_words']}")

    patch = d.get("patch")
    if patch:
        show(f"Repair diff ({patch['file']}, changed lines {patch['changed_lines']})")
        print((c.folder.evidence / patch["diff_evidence_id"]).read_text(encoding="utf-8"))

    show("Checks")
    for ch in d["checks"]:
        print(f"{ch['label']}: {ch['outcome']} ({ch['passed_count']} passed, {ch['failed_count']} failed)")

    show("Decision log")
    for x in d["decisions"]:
        print(f"[{x['decision']}] {x['reason']}")

    show("Summary")
    print(f"Status: {d['status']}")
    print(f"Model calls used: {d['model_calls_used']} of {d['limits']['MAX_MODEL_CALLS']}")
    print(f"Original project unchanged: {d['fixture_unchanged']}")
    if d.get("error"):
        print(f"Error: {d['error']}")
    print(f"Time: {elapsed}s")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))