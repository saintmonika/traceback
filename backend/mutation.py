"""Mutation generator and mutation audit.

Creates small deliberate faults ONLY on the lines the repair changed, then
runs the full test suite against each one to see whether the tests would
catch a wrong fix. Generic: works on whatever code is on those lines.
Descriptions come from fixed templates, never from a model.

Builder's double-run check (run from the repository root):
    python3 -m backend.mutation <run_id>
"""

import ast
import re
import sys

from . import config
from .test_runner import run_pytest
from .workspace import unified_diff

OP_SWAPS = {  # ast operator class -> (regex for its text, replacement text)
    ast.Gt: (r">(?!=)", ">="),
    ast.GtE: (r">=", ">"),
    ast.Lt: (r"<(?!=)", "<="),
    ast.LtE: (r"<=", "<"),
    ast.Eq: (r"==", "!="),
    ast.NotEq: (r"!=", "=="),
}
OP_TEXT = {ast.Gt: ">", ast.GtE: ">=", ast.Lt: "<", ast.LtE: "<=", ast.Eq: "==", ast.NotEq: "!="}


def _replace(lines, lineno, start, end, new_bytes):
    """Replace bytes [start:end] on a 1-based line. Returns the new full text."""
    out = list(lines)
    b = out[lineno - 1].encode("utf-8")
    out[lineno - 1] = (b[:start] + new_bytes + b[end:]).decode("utf-8")
    return "".join(out)


def _fmt_number(value):
    if isinstance(value, int):
        return str(value)
    return repr(round(value, 10))


def generate_mutations(text, rel_path, changed, limit=None):
    """Return a list of mutations, each changing exactly one thing on a changed line."""
    limit = config.MAX_MUTATIONS if limit is None else limit
    changed = set(changed or [])
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    lines = text.splitlines(keepends=True)
    ops, consts, conds = [], [], []

    for node in ast.walk(tree):
        # Kind 1: comparison operator swap
        if isinstance(node, ast.Compare) and node.lineno == node.end_lineno and node.lineno in changed:
            line_b = lines[node.lineno - 1].encode("utf-8")
            operands = [node.left] + list(node.comparators)
            for i, op in enumerate(node.ops):
                if type(op) not in OP_SWAPS:
                    continue
                a, b = operands[i].end_col_offset, operands[i + 1].col_offset
                pattern, repl = OP_SWAPS[type(op)]
                m = re.search(pattern.encode(), line_b[a:b])
                if not m:
                    continue
                start, end = a + m.start(), a + m.end()
                ops.append({
                    "kind": "operator_swap", "line": node.lineno, "col": start,
                    "text": _replace(lines, node.lineno, start, end, repl.encode()),
                    "description": f"Changed {OP_TEXT[type(op)]} to {repl} on line {node.lineno} of {rel_path}",
                })
        # Kind 2: numeric constant shift (booleans excluded)
        elif (isinstance(node, ast.Constant) and type(node.value) in (int, float)
              and node.lineno == node.end_lineno and node.lineno in changed):
            v = node.value
            if abs(v) >= 1:
                deltas = [1, -1]
            elif isinstance(v, float) and v != 0:
                deltas = [0.01, -0.01]
            else:
                continue
            old_src = lines[node.lineno - 1].encode("utf-8")[node.col_offset:node.end_col_offset].decode("utf-8")
            for d in deltas:
                new_src = _fmt_number(v + d)
                consts.append({
                    "kind": "constant_shift", "line": node.lineno, "col": node.col_offset,
                    "text": _replace(lines, node.lineno, node.col_offset, node.end_col_offset, new_src.encode()),
                    "description": f"Changed {old_src} to {new_src} on line {node.lineno} of {rel_path}",
                })
        # Kind 3: condition removal for an if statement
        elif (isinstance(node, ast.If) and node.test.lineno == node.test.end_lineno
              and node.test.lineno in changed):
            t = node.test
            conds.append({
                "kind": "condition_removal", "line": t.lineno, "col": t.col_offset,
                "text": _replace(lines, t.lineno, t.col_offset, t.end_col_offset, b"True"),
                "description": f"Replaced the condition on line {t.lineno} of {rel_path} with True",
            })

    ordered = []
    for group in (ops, consts, conds):
        ordered += sorted(group, key=lambda m: (m["line"], m["col"]))

    result = []
    for m in ordered:
        if len(result) >= limit:
            break
        if m["text"] == text:
            continue
        try:
            ast.parse(m["text"])
        except SyntaxError:
            continue
        m["id"] = f"m{len(result) + 1:02d}"
        m["file"] = rel_path
        m["diff"] = unified_diff(text, m["text"], rel_path)
        result.append(m)
    return result


def run_mutant(folder, mutation, dest_name, evidence, phase, label):
    """Fresh copy of the workspace, write the mutated file, run the full suite."""
    dest = folder.fresh_copy(folder.mutants / dest_name)
    folder.write(dest, mutation["file"], mutation["text"])
    return run_pytest(dest, evidence=evidence, label=label, phase=phase)


def classify(test_outcome):
    if test_outcome == "failed":
        return "caught"
    if test_outcome == "passed":
        return "survived"
    return "invalid"


def audit(folder, mutations, evidence, check_stop=None, phase="MUTATION_AUDIT"):
    """Run every mutation. Returns (results list, summary dict)."""
    results = []
    for m in mutations:
        if check_stop:
            check_stop()
        diff_id = evidence.save(f"mutation_{m['id']}_diff", phase, m["diff"])
        result, _ = run_mutant(folder, m, m["id"], evidence, phase, f"mutation_{m['id']}_output")
        results.append({
            "id": m["id"], "kind": m["kind"], "line": m["line"], "file": m["file"],
            "description": m["description"], "outcome": classify(result["outcome"]),
            "test_outcome": result["outcome"], "failed_count": result["failed_count"],
            "diff_evidence_id": diff_id, "output_evidence_id": result["evidence_id"],
        })
    caught = sum(r["outcome"] == "caught" for r in results)
    survived = sum(r["outcome"] == "survived" for r in results)
    invalid = sum(r["outcome"] == "invalid" for r in results)
    return results, {"tested": caught + survived, "caught": caught,
                     "survived": survived, "invalid": invalid}


# ---------------------------------------------------------------------------
# Builder's double-run check
# ---------------------------------------------------------------------------
def double_run_check(run_id):
    from .state import RunState, RunEvidence, load_state
    from .workspace import RunFolder

    folder = RunFolder(run_id)
    data = load_state(folder.root)
    patch = data.get("patch")
    caught = [m for m in data.get("mutations", []) if m["outcome"] == "caught"]
    if not patch or not caught:
        print("This run has no patch or no caught mutation to re-check.")
        return 1
    text = folder.read(folder.workspace, patch["file"])
    regenerated = {m["id"]: m for m in generate_mutations(text, patch["file"], patch["changed_lines"])}
    target = regenerated.get(caught[0]["id"])
    if not target or target["description"] != caught[0]["description"]:
        print("Could not regenerate the same mutation.")
        return 1
    state = RunState(folder.root, data)
    evidence = RunEvidence(state)
    print(f"Mutation: {target['description']}")
    outcomes = []
    for name in ("double_run_a", "double_run_b"):
        result, _ = run_mutant(folder, target, name, evidence, "DOUBLE_RUN_CHECK", f"{name}_output")
        outcomes.append(classify(result["outcome"]))
        print(f"  {name}: tests {result['outcome']} ({result['failed_count']} failed) -> {outcomes[-1]}")
    ok = outcomes == ["caught", "caught"]
    print(f"DOUBLE_RUN_CHECK: {'PASS (caught both times)' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 -m backend.mutation <run_id>")
        sys.exit(2)
    sys.exit(double_run_check(sys.argv[1]))