"""Crusoe model client.

The one place where Traceback talks to a model:
  - sends chat-completion requests to Crusoe (OpenAI-compatible API)
  - parses replies: one JSON object + at most one BEGIN_CODE/END_CODE block
  - gives one follow-up when a reply does not parse
  - counts every call against the run's limit
  - saves evidence for every call (never the API key)

Run the first-hour test from the repository root:
    python3 -m backend.crusoe_client
    python3 -m backend.crusoe_client --model <model id>
"""

import ast
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import config


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class MissingAPIKey(RuntimeError):
    pass


class ModelCallLimitReached(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# API key handling (read from the environment only; never printed or saved)
# ---------------------------------------------------------------------------
MISSING_KEY_MESSAGE = (
    "CRUSOE_API_KEY is not set in this Terminal window. Load it with: "
    "read -s CRUSOE_API_KEY   then   export CRUSOE_API_KEY"
)


def api_key_present():
    return bool(os.environ.get(config.API_KEY_ENV_VAR, "").strip())


def _get_key():
    key = os.environ.get(config.API_KEY_ENV_VAR, "").strip()
    if not key:
        raise MissingAPIKey(MISSING_KEY_MESSAGE)
    return key


def redact(text):
    """Remove the API key from any text before it is saved or shown."""
    if not text:
        return text
    key = os.environ.get(config.API_KEY_ENV_VAR, "").strip()
    if key:
        text = text.replace(key, "[REDACTED]")
    return text


_client = None


def _get_client():
    global _client
    key = _get_key()
    if _client is None:
        from openai import OpenAI

        _client = OpenAI(
            base_url=config.CRUSOE_BASE_URL,
            api_key=key,
            timeout=config.MODEL_TIMEOUT_SECONDS,
            max_retries=1,
        )
    return _client


# ---------------------------------------------------------------------------
# Call counting
# ---------------------------------------------------------------------------
class CallCounter:
    """Counts model calls for one run. Refuses calls past the limit."""

    def __init__(self, limit=None, used=0):
        self.limit = config.MAX_MODEL_CALLS if limit is None else limit
        self.used = used

    def remaining(self):
        return max(0, self.limit - self.used)

    def take(self):
        if self.used >= self.limit:
            raise ModelCallLimitReached("Model call limit reached.")
        self.used += 1
        return self.used


# ---------------------------------------------------------------------------
# Evidence (minimal folder writer; state.py provides the run version with
# the same save(label, phase, text) -> evidence_id method)
# ---------------------------------------------------------------------------
class FolderEvidence:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.counter = 0

    def save(self, label, phase, text):
        self.counter += 1
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)[:60]
        name = f"{self.counter:03d}_{safe}.txt"
        (self.folder / name).write_text(redact(text), encoding="utf-8")
        return name


# ---------------------------------------------------------------------------
# Reply parsing
# ---------------------------------------------------------------------------
_BEGIN_RE = re.compile(r"^[ \t]*BEGIN_CODE[ \t]*\r?$", re.M)
_END_RE = re.compile(r"^[ \t]*END_CODE[ \t]*\r?$", re.M)
_FENCE_RE = re.compile(r"```[A-Za-z0-9_+-]*[ \t]*\r?\n(.*?)```", re.S)


@dataclass
class ParseResult:
    data: dict = None
    code: str = None
    error: str = None

    @property
    def ok(self):
        return self.error is None


def _strip_inner_fence(code):
    """If the code inside the markers is itself wrapped in ``` fences, unwrap it."""
    lines = code.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) >= 2 and lines[0].strip().startswith("```") and lines[-1].strip() == "```":
        lines = lines[1:-1]
    return "\n".join(lines) + "\n" if lines else ""


def parse_reply(text, code_required):
    """Split a reply into (JSON object, code). Returns a ParseResult.

    The error message is written so it can be sent back to the agent
    as a follow-up.
    """
    if text is None or not text.strip():
        return ParseResult(error="Your reply was empty.")

    begins = list(_BEGIN_RE.finditer(text))
    ends = list(_END_RE.finditer(text))
    code = None
    rest = text

    if len(begins) > 1:
        return ParseResult(
            error="Your reply had more than one BEGIN_CODE line. Send exactly one code block."
        )
    if begins:
        b = begins[0]
        ends_after = [e for e in ends if e.start() >= b.end()]
        if not ends_after:
            return ParseResult(
                error="Your reply had a BEGIN_CODE line but no END_CODE line after it."
            )
        e = ends_after[0]
        code = _strip_inner_fence(text[b.end():e.start()])
        rest = text[:b.start()] + text[e.end():]
    elif ends:
        return ParseResult(error="Your reply had an END_CODE line but no BEGIN_CODE line.")
    elif code_required:
        return ParseResult(
            error=(
                "Your reply had no BEGIN_CODE line. Put the complete file between "
                "a line containing only BEGIN_CODE and a line containing only END_CODE."
            )
        )

    if code_required and (code is None or not code.strip()):
        return ParseResult(error="Your code block between BEGIN_CODE and END_CODE was empty.")

    # JSON: a fenced block containing a brace if present, otherwise first { to last }.
    json_text = None
    for m in _FENCE_RE.finditer(rest):
        if "{" in m.group(1):
            json_text = m.group(1)
            break
    if json_text is None:
        json_text = rest
    first = json_text.find("{")
    last = json_text.rfind("}")
    if first == -1 or last <= first:
        return ParseResult(
            error="Your reply had no JSON object. Start your reply with one JSON object."
        )
    json_text = json_text[first:last + 1]

    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as exc:
        return ParseResult(
            error=(
                f"Your JSON object could not be parsed ({exc.msg} at line {exc.lineno}, "
                f"column {exc.colno}). Send valid JSON with double quotes, and keep code "
                "out of the JSON."
            )
        )
    if not isinstance(data, dict):
        return ParseResult(error="Your JSON must be a single object in curly braces.")

    return ParseResult(data=data, code=code)


# ---------------------------------------------------------------------------
# Model calls
# ---------------------------------------------------------------------------
@dataclass
class CallRecord:
    role: str
    model: str
    call_number: int
    duration_seconds: float
    content: str = None
    finish_reason: str = None
    usage: dict = None
    api_error: str = None
    parse_ok: bool = False
    parse_error: str = None
    evidence_id: str = None


@dataclass
class AgentReply:
    ok: bool
    data: dict = None
    code: str = None
    error: str = None
    calls: list = field(default_factory=list)

    @property
    def evidence_ids(self):
        return [c.evidence_id for c in self.calls if c.evidence_id]


def _messages_text(messages):
    parts = []
    for m in messages:
        parts.append(f"[{m['role']}]\n{m['content']}")
    return "\n\n".join(parts)


def _evidence_text(rec, phase, messages, raw_json):
    usage = json.dumps(rec.usage) if rec.usage is not None else "not returned"
    return "\n".join([
        f"role: {rec.role}",
        f"model: {rec.model}",
        f"phase: {phase}",
        f"call_number: {rec.call_number}",
        f"duration_seconds: {rec.duration_seconds}",
        f"finish_reason: {rec.finish_reason}",
        f"usage: {usage}",
        f"api_error: {rec.api_error}",
        f"parse_ok: {rec.parse_ok}",
        f"parse_error: {rec.parse_error}",
        "",
        "===== REQUEST =====",
        _messages_text(messages),
        "",
        "===== RESPONSE CONTENT =====",
        rec.content if rec.content is not None else "(none)",
        "",
        "===== RAW RESPONSE =====",
        raw_json or "(none)",
        "",
    ])


def _one_call(role, model, messages, max_tokens, counter):
    """Make exactly one API call. Returns (CallRecord, raw_json)."""
    client = _get_client()          # raises MissingAPIKey before anything is counted
    call_number = counter.take()     # raises ModelCallLimitReached at the limit
    start = time.monotonic()
    rec = CallRecord(role=role, model=model, call_number=call_number, duration_seconds=0.0)
    raw_json = ""
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=config.TEMPERATURE,
            max_tokens=max_tokens,
            stream=False,
        )
        choice = resp.choices[0]
        rec.content = choice.message.content   # any separate reasoning field is ignored
        rec.finish_reason = getattr(choice, "finish_reason", None)
        usage = getattr(resp, "usage", None)
        if usage is not None and hasattr(usage, "model_dump"):
            rec.usage = usage.model_dump()
        raw_json = resp.model_dump_json(indent=2) if hasattr(resp, "model_dump_json") else str(resp)
    except Exception as exc:  # network errors, timeouts, API errors
        rec.api_error = redact(f"{type(exc).__name__}: {exc}")
    rec.duration_seconds = round(time.monotonic() - start, 2)
    return rec, raw_json


def ask_agent(role, system_prompt, user_prompt, *, code_required, counter,
              evidence=None, phase="", model=None, max_tokens=None, allow_followup=True):
    """Ask one agent. If the reply does not parse, send one follow-up saying
    exactly what was wrong. Every call is counted and saved as evidence."""
    model = model or config.model_for(role)
    max_tokens = max_tokens or config.MAX_TOKENS_BY_ROLE.get(role, 2000)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    reply = AgentReply(ok=False)

    for attempt in (1, 2):
        rec, raw_json = _one_call(role, model, messages, max_tokens, counter)
        if rec.api_error:
            rec.parse_error = "No reply (API error)."
            parsed = ParseResult(error=rec.parse_error)
        else:
            parsed = parse_reply(rec.content, code_required)
            if not parsed.ok and rec.finish_reason == "length":
                parsed.error = (
                    "Your reply was cut off before it finished (token limit reached). "
                    + parsed.error
                    + " Reply again, keeping the JSON short."
                )
            rec.parse_ok = parsed.ok
            rec.parse_error = parsed.error
        if evidence is not None:
            rec.evidence_id = evidence.save(
                f"{role}_call_{rec.call_number:02d}", phase,
                _evidence_text(rec, phase, messages, raw_json),
            )
        reply.calls.append(rec)

        if parsed.ok:
            reply.ok, reply.data, reply.code, reply.error = True, parsed.data, parsed.code, None
            return reply

        reply.error = rec.api_error or parsed.error
        # One follow-up, only for a parse problem (not an API error), only if calls remain.
        if attempt == 1 and allow_followup and not rec.api_error and counter.remaining() > 0:
            fmt = "first one JSON object"
            if code_required:
                fmt += (", then one line containing only BEGIN_CODE, the complete file, "
                        "and one line containing only END_CODE")
            messages = messages + [
                {"role": "assistant", "content": rec.content or ""},
                {"role": "user", "content": (
                    f"{parsed.error} Reply again with the same content in the required "
                    f"format: {fmt}. Do not add anything else."
                )},
            ]
            continue
        break

    return reply


# ---------------------------------------------------------------------------
# First-hour test (generic; nothing about the fixture project)
# ---------------------------------------------------------------------------
_FH_SYSTEM = "You are a careful Python developer. You always reply in the exact format requested."

_FH_USER = """Write a small Python function named clamp(value, low, high) that returns value limited to the range from low to high, and pytest tests for it, all in one file.

Reply format (follow exactly):
1. First, one JSON object with these fields:
   "function_name": the function's name (string)
   "summary": one sentence describing what the tests check (string)
   "test_count": how many test functions the file contains (integer)
2. Then a line containing only BEGIN_CODE
3. Then the complete file content (the function and its tests)
4. Then a line containing only END_CODE

Do not put code inside the JSON. Do not write anything after END_CODE."""


def _check_first_hour(reply):
    """Return a list of problems (empty list means the test passed)."""
    problems = []
    if not reply.ok:
        return [f"Reply did not parse: {reply.error}"]
    for key in ("function_name", "summary", "test_count"):
        if key not in reply.data:
            problems.append(f"JSON is missing the field '{key}'")
    try:
        tree = ast.parse(reply.code)
        tests = [n.name for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")]
        if not tests:
            problems.append("Code parses but defines no test_ function")
    except SyntaxError as exc:
        problems.append(f"Code does not parse as Python: {exc}")
    return problems


def first_hour_test(models):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = config.RUNS_ROOT / f"first_hour_{stamp}" / "evidence"
    evidence = FolderEvidence(folder)
    counter = CallCounter(limit=20)

    for model in models:
        for attempt in (1, 2):
            print(f"\n--- Model: {model}  (try {attempt} of 2) ---")
            reply = ask_agent(
                "first_hour", _FH_SYSTEM, _FH_USER,
                code_required=True, counter=counter, evidence=evidence,
                phase="FIRST_HOUR_TEST", model=model,
            )
            for c in reply.calls:
                print(f"  call {c.call_number}: {c.duration_seconds}s, finish_reason={c.finish_reason}, "
                      f"parse_ok={c.parse_ok}, evidence={folder / c.evidence_id}")
                if c.api_error:
                    print(f"  API error: {c.api_error}")
            problems = _check_first_hour(reply)
            if not problems:
                code_lines = len(reply.code.splitlines())
                print(f"  JSON fields: {sorted(reply.data.keys())}")
                print(f"  Code block: {code_lines} lines, parses as Python, has test_ functions")
                print(f"\nFIRST_HOUR_TEST: PASS with {model}")
                print(f"Evidence folder: {folder}")
                return 0
            for p in problems:
                print(f"  Problem: {p}")
        print(f"  {model} failed twice. Trying the next model.")

    print("\nFIRST_HOUR_TEST: FAIL on every model tried.")
    print(f"Evidence folder: {folder}")
    return 1


def main(argv):
    if not api_key_present():
        print(MISSING_KEY_MESSAGE)
        return 2
    if len(argv) >= 2 and argv[0] == "--model":
        models = [argv[1]]
    else:
        models = [config.PLANNER_MODEL] + config.BACKUP_MODELS
    return first_hour_test(models)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))