"""Controller: the state machine. Ordinary Python that owns every decision.

Models only propose. Every transition is decided here from real command
output, and every decision is logged with a fixed template.

Step C version: READY -> BASELINE -> DEFINE_DONE <-> VALIDATE_DEFINITION
-> REPAIR <-> RUN_CHECKS. Later steps add MUTATION_AUDIT, GAP_CLOSURE,
VALIDATE_GAP, FINAL_CHECKS, REPORT.
"""

import re
import threading
import traceback

from . import config
from . import agents
from .crusoe_client import CallCounter, ModelCallLimitReached, MissingAPIKey, api_key_present
from .state import RunState, RunEvidence, new_state
from .test_runner import run_pytest
from .workspace import RunFolder, make_run_id, hash_tree, list_files, unified_diff, changed_lines


class StopRequested(Exception):
    pass


class RunEnded(Exception):
    """Raised internally when a phase sets a final status."""


def assertion_failure(output):
    out = output or ""
    return "AssertionError" in out or re.search(r"^E\s+assert\b", out, re.M) is not None


class Controller:
    def __init__(self, goal, log=None):
        self.goal = goal.strip()
        self.log = log or (lambda msg: None)
        self.stop_event = threading.Event()
        self.run_id = make_run_id()
        self.folder = RunFolder(self.run_id)
        self.folder.create()
        self.state = RunState(self.folder.root, new_state(self.run_id, self.goal))
        self.state.save()
        self.evidence = RunEvidence(self.state)
        self.counter = CallCounter()
        self.fixture_hashes = None
        self.done_test_code = None
        self.done_test_output = None

    # ------------------------------------------------------------------ utils
    @property
    def data(self):
        return self.state.data

    def request_stop(self):
        self.stop_event.set()
        self.data["stop_requested"] = True
        self.state.save()

    def check_stop(self):
        if self.stop_event.is_set():
            raise StopRequested()

    def phase(self, name):
        self.check_stop()
        self.state.set_phase(name)
        self.log(f"-- phase: {name}")

    def decide(self, word, reason):
        self.state.add_decision(word, reason)
        self.log(f"   [{word}] {reason}")

    def end(self, status, word, reason):
        self.decide(word, reason)
        self.data["status"] = status
        self.state.save()
        raise RunEnded()

    def sync_calls(self):
        self.data["model_calls_used"] = self.counter.used
        self.state.save()

    def test(self, target, test_file=None, label="tests"):
        self.check_stop()
        result, output = run_pytest(target, test_file, evidence=self.evidence,
                                    label=label, phase=self.data["current_phase"])
        return result, output

    def add_check(self, label, result):
        entry = dict(result)
        entry["label"] = label
        self.data["checks"].append(entry)
        self.state.save()

    # -------------------------------------------------------------------- run
    def run(self):
        try:
            if not api_key_present():
                raise MissingAPIKey("CRUSOE_API_KEY is not set in the Terminal that started Traceback.")
            self.ready()
            self.baseline()
            self.define_done()
            self.repair()
            # Step C stops here. Later steps continue with the mutation audit.
            self.data["status"] = "build_incomplete"
            self.state.set_phase("MUTATION_AUDIT (not built yet)")
        except RunEnded:
            pass
        except StopRequested:
            self.decide("stop", "Stopped by user.")
            self.data["status"] = "stopped"
        except ModelCallLimitReached:
            self.decide("stop", "Model call limit reached.")
            self.data["status"] = "attempt_limit_reached"
        except Exception as exc:  # unexpected error: shown with its text
            self.data["error"] = f"{type(exc).__name__}: {exc}"
            self.evidence.save("unexpected_error", self.data.get("current_phase", ""),
                               traceback.format_exc())
            self.decide("stop", f"Unexpected error: {self.data['error']}.")
            self.data["status"] = "failed"
        finally:
            self.sync_calls()
            if self.fixture_hashes is not None:
                self.data["fixture_unchanged"] = hash_tree(config.FIXTURE_DIR) == self.fixture_hashes
            self.state.save()
        return self.data

    # ----------------------------------------------------------------- phases
    def ready(self):
        self.phase("READY")
        self.fixture_hashes = hash_tree(config.FIXTURE_DIR)
        self.folder.copy_fixture()
        self.state.complete_phase("READY")

    def baseline(self):
        self.phase("BASELINE")
        result, _ = self.test(self.folder.workspace, label="baseline_output")
        self.data["baseline"] = result
        self.add_check("Existing tests on the original code", result)
        if result["outcome"] != "passed":
            self.end("blocked", "stop", "The existing tests do not pass on the original code.")
        self.decide("continue", "Baseline established: the existing tests pass on the original code.")
        self.state.complete_phase("BASELINE")

    def define_done(self):
        max_attempts = config.MAX_PLANNER_ATTEMPTS
        used = 0
        history = []
        files = list_files(self.folder.workspace)
        b = self.data["baseline"]
        baseline_summary = f"{b['passed_count']} existing tests, all passing"

        while used < max_attempts:
            self.phase("DEFINE_DONE")
            reply = agents.run_planner(self.goal, self.folder.workspace, files, baseline_summary,
                                       history, counter=self.counter, evidence=self.evidence,
                                       phase="DEFINE_DONE")
            self.sync_calls()
            used += max(1, len(reply.calls))
            attempt = {"attempt": len(self.data["planner_attempts"]) + 1,
                       "model_calls": len(reply.calls), "evidence_ids": reply.evidence_ids,
                       "accepted": False, "rejection_reason": None, "summary": None,
                       "validation": None}

            if not reply.ok:
                reason, feedback = "the reply was not in the required format", reply.error
            else:
                dod, problem = agents.validate_planner(reply.data, reply.code,
                                                       self.folder.workspace, files)
                attempt["summary"] = {k: reply.data.get(k) for k in
                                      ("required_behavior", "test_summary", "allowed_scope")}
                if problem:
                    reason, feedback = problem, f"Your reply was rejected: {problem}."
                else:
                    reason, feedback = self.validate_definition(dod, reply.code, attempt)
                    if reason is None:
                        self.data["planner_attempts"].append(attempt)
                        self.state.complete_phase("DEFINE_DONE")
                        self.state.complete_phase("VALIDATE_DEFINITION")
                        return

            attempt["rejection_reason"] = reason
            output = attempt.pop("_output", None)
            self.data["planner_attempts"].append(attempt)
            self.state.save()
            history.append({"attempt": attempt["attempt"], "reason": feedback, "output": output})
            if used < max_attempts:
                self.decide("replan", f"Agent's test rejected ({reason}). Asking the planner again, "
                                      f"attempt {used + 1} of {max_attempts}.")

        self.end("blocked", "stop", f"No valid definition of done after {max_attempts} attempts.")

    def validate_definition(self, dod, code, attempt):
        """Write the planner's test and run it on the original code.
        Returns (None, None) if accepted, else (short reason, feedback)."""
        self.phase("VALIDATE_DEFINITION")
        ws = self.folder.workspace
        self.folder.write(ws, config.DONE_TEST_FILE, code)
        self.evidence.save("done_test_file", "VALIDATE_DEFINITION", code)
        result, output = self.test(ws, config.DONE_TEST_FILE, label="done_test_on_original")
        attempt["validation"] = result

        if result["outcome"] == "failed" and assertion_failure(output):
            self.add_check("Agent's test on the original code (must fail)", result)
            self.data["definition_of_done"] = {
                "required_behavior": dod["required_behavior"],
                "preservation_requirements": dod["preservation_requirements"],
                "allowed_scope": dod["allowed_scope"],
                "test_summary": dod["test_summary"],
                "done_test_file": config.DONE_TEST_FILE,
                "validation_result": {"outcome": result["outcome"],
                                      "evidence_id": result["evidence_id"]},
            }
            self.data["assumptions"] = dod["assumptions"]
            attempt["accepted"] = True
            self.done_test_code = code
            self.done_test_output = output
            self.decide("continue", "Definition of done validated: the agent's test fails on the "
                                    "original code, as required.")
            return None, None

        self.folder.delete(ws, config.DONE_TEST_FILE)
        attempt["_output"] = output
        if result["outcome"] == "passed":
            return ("it passes on the original code",
                    "Your test PASSED on the current code, so it does not detect the problem. "
                    "Write a test that fails on the current code because the goal is not yet met.")
        if result["outcome"] == "failed":
            return ("it fails with an error, not an assertion failure",
                    "Your test failed with an error instead of an assertion failure. Make it fail "
                    "only through an assert that checks the goal.")
        return (f"the test could not run ({result['outcome']})",
                f"Your test could not run (pytest result: {result['outcome']}). Fix the import or "
                "syntax problem shown in the output.")

    def repair(self):
        max_attempts = config.MAX_REPAIR_ATTEMPTS
        dod = self.data["definition_of_done"]
        ws = self.folder.workspace
        used = 0
        history = []
        proposed = set()

        while used < max_attempts:
            self.phase("REPAIR")
            reply = agents.run_repair(self.goal, dod, ws, self.done_test_code, self.done_test_output,
                                      history, counter=self.counter, evidence=self.evidence,
                                      phase="REPAIR")
            self.sync_calls()
            used += max(1, len(reply.calls))
            attempt = {"attempt": len(self.data["repair_attempts"]) + 1,
                       "model_calls": len(reply.calls), "evidence_ids": reply.evidence_ids,
                       "accepted": False, "rejection_reason": None, "file": None,
                       "reasoning": None, "checks": None}
            output = None

            if not reply.ok:
                reason = "the reply was not in the required format"
                feedback = reply.error
            else:
                norm, problem = agents.validate_repair(reply.data, reply.code, dod, ws, proposed)
                if norm and norm["action"] == "blocked":
                    attempt["reasoning"] = norm["reasoning"]
                    attempt["rejection_reason"] = "agent reported blocked"
                    attempt["blocked_reason"] = norm["blocked_reason"]
                    self.data["repair_attempts"].append(attempt)
                    self.data["blocked_reason_agent_words"] = norm["blocked_reason"]
                    self.end("blocked", "stop", "The repair agent reported it cannot proceed.")
                attempt["reasoning"] = (norm or {}).get("reasoning") or str(reply.data.get("reasoning", ""))
                if problem:
                    reason, feedback = problem, f"Your patch was rejected: {problem}."
                else:
                    proposed.add(norm["hash"])
                    attempt["file"] = norm["file"]
                    reason, feedback, output = self.run_checks(norm, reply.code, attempt)
                    if reason is None:
                        self.data["repair_attempts"].append(attempt)
                        self.state.complete_phase("REPAIR")
                        self.state.complete_phase("RUN_CHECKS")
                        return

            attempt["rejection_reason"] = reason
            self.data["repair_attempts"].append(attempt)
            self.state.save()
            history.append({"attempt": attempt["attempt"], "reasoning": attempt["reasoning"],
                            "reason": feedback, "output": output})
            if used < max_attempts:
                if attempt.get("checks") and attempt["checks"]["outcome"] == "failed":
                    self.decide("replan", f"Repair failed the checks ({attempt['checks']['failed_count']} "
                                          f"failing). Trying again, attempt {used + 1} of {max_attempts}.")
                else:
                    self.decide("replan", f"Repair rejected ({reason}). Trying again, attempt "
                                          f"{used + 1} of {max_attempts}.")

        self.end("attempt_limit_reached", "stop",
                 f"Stopped after {max_attempts} repair attempts without passing the checks.")

    def run_checks(self, norm, new_code, attempt):
        """Apply the patch to the workspace and run the full suite.
        Returns (None, None, output) if passed, else (reason, feedback, output)."""
        self.phase("RUN_CHECKS")
        rel = norm["file"]
        previous = self.folder.apply_file(rel, new_code)
        self.decide("continue", f"Repair applied to {rel} in the isolated copy.")
        result, output = self.test(self.folder.workspace, label=f"checks_repair_{attempt['attempt']}")
        attempt["checks"] = result
        self.add_check(f"Full suite on repair attempt {attempt['attempt']}", result)

        if result["outcome"] == "passed":
            diff = unified_diff(previous, new_code, rel)
            diff_id = self.evidence.save("repair_diff", "RUN_CHECKS", diff)
            self.data["patch"] = {"file": rel, "diff_evidence_id": diff_id,
                                  "changed_lines": changed_lines(previous, new_code),
                                  "repair_attempt": attempt["attempt"],
                                  "previous_content_evidence_id":
                                      self.evidence.save("original_file_before_repair", "RUN_CHECKS", previous)}
            attempt["accepted"] = True
            self.decide("continue", "All checks pass on the repair. Auditing whether the checks "
                                    "would catch changes to the fix.")
            return None, None, output

        self.folder.write(self.folder.workspace, rel, previous)   # restore
        if result["outcome"] == "failed":
            return ("the checks failed",
                    f"Your patch was applied but the tests failed ({result['failed_count']} failing).",
                    output)
        return (f"the checks could not run ({result['outcome']})",
                f"Your patch was applied but the tests could not run ({result['outcome']}).",
                output)