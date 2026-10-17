"""The three agents: planner, repair agent, gap-closer.

Each agent builds its input from run data only (never from the build
documents, the plan, or the correct fix), calls the model through
crusoe_client, and validates the reply. Validation failures are returned
to the controller, which decides what happens next.
"""

import ast
import hashlib
from pathlib import Path

from . import config
from .crusoe_client import ask_agent

ALLOWED_BASIS = {"user_goal", "existing_tests", "agent_inference", "unresolved"}
SKIP_CONTENT_NAMES = {"LICENSE", "README.md"}
MAX_TEST_LINES = 200
OUTPUT_EXCERPT_CHARS = 3000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def norm_path(p):
    p = str(p or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def is_test_path(p):
    p = norm_path(p)
    name = p.rsplit("/", 1)[-1]
    return p.startswith("tests/") or "/tests/" in p or name.startswith("test_") or name == "conftest.py"


def excerpt(text, limit=OUTPUT_EXCERPT_CHARS):
    text = text or ""
    return text if len(text) <= limit else "...(earlier output cut)...\n" + text[-limit:]


def project_listing(workspace, files):
    """File list plus full contents of every .py file (source and tests)."""
    parts = ["Files in the project:"]
    parts += [f"  {f}" for f in files]
    for f in files:
        name = f.rsplit("/", 1)[-1]
        if name in SKIP_CONTENT_NAMES or not f.endswith(".py"):
            continue
        text = (Path(workspace) / f).read_text(encoding="utf-8")
        parts.append(f"\n===== FILE: {f} =====\n{text if text.strip() else '(empty file)'}")
    return "\n".join(parts)


def code_problems(code):
    """Shared checks for agent-written test files."""
    if not code or not code.strip():
        return "the test file was empty"
    if len(code.splitlines()) >= MAX_TEST_LINES:
        return f"the test file must be under {MAX_TEST_LINES} lines"
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"the test file does not parse as Python ({exc.msg}, line {exc.lineno})"
    tests = [n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")]
    if not tests:
        return "the test file defines no function whose name starts with test_"
    return None


def _items(value):
    """Normalize a list of {statement, basis} items. Returns (items, problem)."""
    if not isinstance(value, list):
        return None, "must be a list"
    out = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append({"statement": item.strip(), "basis": "agent_inference"})
            continue
        if not isinstance(item, dict):
            return None, "each item must be an object with statement and basis"
        st = str(item.get("statement", "")).strip()
        if not st:
            return None, "each item needs a non-empty statement"
        basis = str(item.get("basis", "")).strip()
        out.append({"statement": st, "basis": basis if basis in ALLOWED_BASIS else "agent_inference"})
    return out, None


def content_hash(text):
    norm = "\n".join(line.rstrip() for line in (text or "").splitlines())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


FORMAT_RULES = (
    "Reply format (follow exactly):\n"
    "1. First, one JSON object with the fields listed below. Use double quotes. Never put code inside the JSON.\n"
    "2. Then a line containing only BEGIN_CODE\n"
    "3. Then the complete file content\n"
    "4. Then a line containing only END_CODE\n"
    "Write nothing after END_CODE. Never include shell commands."
)


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------
PLANNER_SYSTEM = (
    "You are the planner in Traceback, a tool that turns a one-line goal about a small Python "
    "project into a checkable definition of done. You do not fix code. You state what done means "
    "and write one new pytest test file that checks the goal."
)


def build_planner_prompt(goal, workspace, files, baseline_summary, history):
    hist = ""
    if history:
        lines = ["Earlier attempts in this run were rejected:"]
        for h in history:
            lines.append(f"- Attempt {h['attempt']}: {h['reason']}")
            if h.get("output"):
                lines.append(f"  Test output:\n{excerpt(h['output'], 1500)}")
        hist = "\n".join(lines) + "\n\n"
    return f"""GOAL (exactly as the user typed it):
{goal}

BASELINE: {baseline_summary}

{project_listing(workspace, files)}

NEW TEST FILE: the controller will save your test as {config.DONE_TEST_FILE} and run it with pytest from the project folder, on the CURRENT code.

{hist}RULES:
- Write exactly one new test file that checks the goal.
- Import from the project's own modules, the same way the existing tests do.
- The test must fail on the current code if the goal is not yet met. It must fail with an assertion failure, not an import error or syntax error.
- Test only the behavior the goal is about. Do not test unrelated behavior.
- Do not modify any other file.
- List your assumptions honestly. Each needs a basis.
- Allowed basis values: "user_goal", "existing_tests", "agent_inference", "unresolved".

JSON fields:
  "required_behavior": one sentence stating what must be true when the goal is met.
  "preservation_requirements": list of objects {{"statement": one sentence, "basis": one basis value}} for existing behavior that must not change.
  "assumptions": list of objects {{"statement": one sentence, "basis": one basis value}}.
  "allowed_scope": list of source file paths the repair may change (not test files, not conftest.py).
  "test_summary": one plain sentence describing what the new test checks.

{FORMAT_RULES}"""


def validate_planner(data, code, workspace, files):
    """Returns (normalized definition or None, rejection reason or None)."""
    d = {}
    for key in ("required_behavior", "test_summary"):
        v = data.get(key)
        if not isinstance(v, str) or not v.strip():
            return None, f"the field {key} is missing or empty"
        d[key] = v.strip()
    for key in ("preservation_requirements", "assumptions"):
        if key not in data:
            return None, f"the field {key} is missing"
        items, problem = _items(data[key])
        if problem:
            return None, f"{key} {problem}"
        d[key] = items
    scope = data.get("allowed_scope")
    if isinstance(scope, str):
        scope = [scope]
    if not isinstance(scope, list) or not scope:
        return None, "allowed_scope must be a non-empty list of source files"
    clean = []
    for p in scope:
        p = norm_path(p)
        if p not in files or not p.endswith(".py"):
            return None, f"allowed_scope lists '{p}', which is not a .py file in the project"
        if is_test_path(p):
            return None, f"allowed_scope lists '{p}', which is a test file or conftest.py"
        if p not in clean:
            clean.append(p)
    d["allowed_scope"] = clean
    problem = code_problems(code)
    if problem:
        return None, problem
    return d, None


def run_planner(goal, workspace, files, baseline_summary, history, *, counter, evidence, phase):
    prompt = build_planner_prompt(goal, workspace, files, baseline_summary, history)
    return ask_agent("planner", PLANNER_SYSTEM, prompt, code_required=True,
                     counter=counter, evidence=evidence, phase=phase)


# ---------------------------------------------------------------------------
# Repair agent
# ---------------------------------------------------------------------------
REPAIR_SYSTEM = (
    "You are the repair agent in Traceback. You change the code of a small Python project so "
    "that a stated goal is met, inside an isolated copy. You make the smallest change that "
    "meets the goal and you never change tests."
)


def _dod_text(dod):
    lines = [f"Required behavior: {dod['required_behavior']}"]
    if dod.get("preservation_requirements"):
        lines.append("Must not change:")
        lines += [f"  - {p['statement']}" for p in dod["preservation_requirements"]]
    if dod.get("assumptions"):
        lines.append("Assumptions:")
        lines += [f"  - {a['statement']} (basis: {a['basis']})" for a in dod["assumptions"]]
    lines.append(f"Files you may change: {', '.join(dod['allowed_scope'])}")
    lines.append(f"What the new test checks: {dod['test_summary']}")
    return "\n".join(lines)


def build_repair_prompt(goal, dod, workspace, done_test_code, done_test_output, history):
    files = "\n".join(
        f"\n===== FILE: {p} =====\n{(Path(workspace) / p).read_text(encoding='utf-8')}"
        for p in dod["allowed_scope"]
    )
    hist = ""
    if history:
        lines = ["Earlier repair attempts in this run:"]
        for h in history:
            lines.append(f"- Attempt {h['attempt']}: your reasoning was: {h.get('reasoning') or '(none)'}. "
                         f"Result: {h['reason']}")
            if h.get("output"):
                lines.append(f"  Test output:\n{excerpt(h['output'], 1500)}")
        hist = "\n".join(lines) + "\n\n"
    return f"""GOAL:
{goal}

DEFINITION OF DONE:
{_dod_text(dod)}

CURRENT CONTENT OF THE FILES YOU MAY CHANGE:{files}

THE NEW TEST ({config.DONE_TEST_FILE}):
{done_test_code}

ITS OUTPUT ON THE CURRENT CODE (it fails, as expected):
{excerpt(done_test_output)}

{hist}RULES:
- Change only one file, chosen from: {', '.join(dod['allowed_scope'])}.
- Make the smallest change that meets the goal. Keep all other behavior the same.
- Return the complete new content of that file, not a diff.
- Never change any test.
- If the goal cannot be met safely, set "action" to "blocked" and explain why in "blocked_reason". Then send no code block.

JSON fields:
  "action": "propose_patch" or "blocked"
  "file": the one file you changed (a path from the list above)
  "reasoning": one or two sentences
  "blocked_reason": only when action is "blocked"

{FORMAT_RULES}"""


def validate_repair(data, code, dod, workspace, proposed_hashes):
    """Returns (normalized reply or None, rejection reason or None).
    A 'blocked' reply is returned as normalized data with action 'blocked'."""
    action = str(data.get("action", "")).strip()
    reasoning = str(data.get("reasoning", "")).strip()
    if action == "blocked":
        return {"action": "blocked", "reasoning": reasoning,
                "blocked_reason": str(data.get("blocked_reason", "")).strip() or "(no reason given)"}, None
    if action != "propose_patch":
        return None, "the field action must be propose_patch or blocked"
    path = norm_path(data.get("file"))
    if path not in dod["allowed_scope"]:
        return None, f"out of scope: '{path}' is not in the allowed files"
    target = Path(workspace) / path
    if not target.is_file() or is_test_path(path):
        return None, f"'{path}' is not an existing source file"
    if code is None:
        return None, "action was propose_patch but there was no BEGIN_CODE block"
    try:
        ast.parse(code)
    except SyntaxError as exc:
        return None, f"the new content does not parse as Python ({exc.msg}, line {exc.lineno})"
    current = target.read_text(encoding="utf-8")
    if content_hash(code) == content_hash(current):
        return None, "unchanged: the new content is the same as the current file"
    h = content_hash(code)
    if h in proposed_hashes:
        return None, "repeated: this exact content was already proposed in this run"
    if len(code) >= 3 * max(len(current), 1):
        return None, "too large: the new content is three or more times the size of the original"
    return {"action": "propose_patch", "file": path, "reasoning": reasoning, "hash": h}, None


def run_repair(goal, dod, workspace, done_test_code, done_test_output, history, *, counter, evidence, phase):
    prompt = build_repair_prompt(goal, dod, workspace, done_test_code, done_test_output, history)
    return ask_agent("repair", REPAIR_SYSTEM, prompt, code_required=False,
                     counter=counter, evidence=evidence, phase=phase)


# ---------------------------------------------------------------------------
# Gap-closer
# ---------------------------------------------------------------------------
GAP_SYSTEM = (
    "You are the gap-closer in Traceback. A small deliberate change to the repaired code was not "
    "caught by the current tests. You write one new pytest test that passes on the repaired code "
    "and fails on the changed code, by testing real behavior implied by the goal, the definition "
    "of done, or the existing tests."
)


def build_gap_prompt(goal, dod, repaired_file, repaired_text, mutation_desc, mutation_diff,
                     test_files, history):
    tests = "\n".join(f"\n===== FILE: {name} =====\n{text}" for name, text in test_files)
    hist = ""
    if history:
        lines = ["Earlier tests you wrote for this change were rejected:"]
        for h in history:
            lines.append(f"- Attempt {h['attempt']}: {h['reason']}")
            if h.get("output"):
                lines.append(f"  Test output:\n{excerpt(h['output'], 1500)}")
        hist = "\n".join(lines) + "\n\n"
    return f"""GOAL:
{goal}

DEFINITION OF DONE:
{_dod_text(dod)}

REPAIRED FILE ({repaired_file}):
{repaired_text}

CHANGE THAT THE CURRENT TESTS DID NOT CATCH: {mutation_desc}
Diff against the repaired file:
{mutation_diff}

CURRENT TEST FILES:{tests}

{hist}RULES:
- Write one new test file that passes on the repaired code and fails on the changed code.
- Test real behavior implied by the goal, the definition of done, or the existing tests.
- Do not mention the change itself in the test; test only the behavior.
- Import from the project's own modules, the same way the existing tests do.
- Do not modify any other test.

JSON fields:
  "statement": one plain sentence describing the behavior the new test checks
  "reasoning": one or two sentences

{FORMAT_RULES}"""


def validate_gap(data, code):
    statement = str(data.get("statement", "")).strip()
    if not statement:
        return None, "the field statement is missing or empty"
    if "reasoning" not in data:
        return None, "the field reasoning is missing"
    problem = code_problems(code)
    if problem:
        return None, problem
    return {"statement": statement, "reasoning": str(data.get("reasoning", "")).strip()}, None


def run_gap(goal, dod, repaired_file, repaired_text, mutation_desc, mutation_diff, test_files,
            history, *, counter, evidence, phase):
    prompt = build_gap_prompt(goal, dod, repaired_file, repaired_text, mutation_desc,
                              mutation_diff, test_files, history)
    return ask_agent("gap", GAP_SYSTEM, prompt, code_required=True,
                     counter=counter, evidence=evidence, phase=phase)