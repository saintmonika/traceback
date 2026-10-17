"""Run one investigation from Terminal (used while building).

Step B version: creates a run folder, copies the fixture, runs the baseline,
and prints the result. Later steps extend this to the full controller.

Usage, from the repository root:
    python3 -m backend.cli "your one-line goal"
"""

import sys

from . import config
from .state import RunState, RunEvidence, new_state, load_state
from .test_runner import run_pytest
from .workspace import RunFolder, make_run_id, hash_tree


def main(argv):
    if not argv or not argv[0].strip():
        print('Usage: python3 -m backend.cli "your one-line goal"')
        return 2
    goal = argv[0].strip()

    # READY
    run_id = make_run_id()
    folder = RunFolder(run_id)
    folder.create()
    state = RunState(folder.root, new_state(run_id, goal))
    state.save()
    evidence = RunEvidence(state)
    fixture_hashes_before = hash_tree(config.FIXTURE_DIR)
    folder.copy_fixture()
    state.complete_phase("READY")
    print(f"Run: {run_id}")
    print(f"Run folder: {folder.root}")

    # BASELINE
    state.set_phase("BASELINE")
    result, _output = run_pytest(folder.workspace, evidence=evidence,
                                 label="baseline_output", phase="BASELINE")
    state.set("baseline", result)
    if result["outcome"] == "passed":
        state.add_decision("continue", "Baseline established: the existing tests pass on the original code.")
        state.complete_phase("BASELINE")
    else:
        state.add_decision("stop", "The existing tests do not pass on the original code.")
        state.data["status"] = "blocked"
        state.save()

    # Integrity check on the original fixture
    fixture_unchanged = hash_tree(config.FIXTURE_DIR) == fixture_hashes_before
    state.set("fixture_unchanged", fixture_unchanged)

    # Print results, read back from state.json to prove it is readable
    saved = load_state(folder.root)
    b = saved["baseline"]
    print(f"Baseline: {b['outcome']} ({b['passed_count']} passed, {b['failed_count']} failed, "
          f"{b['duration_seconds']}s), evidence {b['evidence_id']}")
    print(f"state.json readable: yes (status={saved['status']}, phase={saved['current_phase']}, "
          f"completed={saved['completed_phases']})")
    print(f"Original fixture unchanged: {saved['fixture_unchanged']}")
    print("Decisions:")
    for d in saved["decisions"]:
        print(f"  [{d['decision']}] {d['reason']}")
    return 0 if b["outcome"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))