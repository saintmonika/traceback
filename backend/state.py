"""Run state file and evidence files.

state.json is rewritten after every step. It is written to a temporary file
in the same folder and then renamed over the old one, so a crash never
leaves a half-written file. The interface reads only this data.

Evidence files live in runs/<run_id>/evidence/ with a three-digit counter
and a short label. They never contain the API key.
"""

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path

from . import config
from .crusoe_client import redact


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def new_state(run_id, goal):
    """Every field from the build specification, with empty starting values."""
    t = now_iso()
    return {
        "run_id": run_id,
        "created_at": t,
        "updated_at": t,
        "goal": goal,
        "fixture_path": config.FIXTURE_PATH,
        "status": "running",
        "current_phase": "READY",
        "completed_phases": [],
        "stop_requested": False,
        "model_calls_used": 0,
        "limits": config.limits_dict(),
        "baseline": None,
        "definition_of_done": None,
        "assumptions": [],
        "planner_attempts": [],
        "repair_attempts": [],
        "gap_attempts": [],
        "patch": None,
        "checks": [],
        "mutations": [],
        "audit_summary": None,
        "gaps": [],
        "decisions": [],
        "fixture_unchanged": None,
        "report": {},
        "evidence": [],
    }


class RunState:
    """Holds one run's state in memory and saves it atomically."""

    def __init__(self, run_dir, data):
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / "state.json"
        self.data = data
        self.lock = threading.Lock()

    # -- saving -------------------------------------------------------------
    def save(self):
        with self.lock:
            self.data["updated_at"] = now_iso()
            text = redact(json.dumps(self.data, indent=2, ensure_ascii=False))
            tmp = self.run_dir / "state.json.tmp"
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self.path)

    # -- helpers used by the controller --------------------------------------
    def set_phase(self, phase):
        self.data["current_phase"] = phase
        self.save()

    def complete_phase(self, phase):
        if phase not in self.data["completed_phases"]:
            self.data["completed_phases"].append(phase)
        self.save()

    def add_decision(self, word, reason):
        self.data["decisions"].append({
            "time": now_iso(),
            "phase": self.data.get("current_phase"),
            "decision": word,
            "reason": reason,
        })
        self.save()

    def set(self, key, value):
        self.data[key] = value
        self.save()


class RunEvidence:
    """Writes numbered evidence files and lists each one in the state."""

    def __init__(self, run_state):
        self.state = run_state
        self.folder = run_state.run_dir / "evidence"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def save(self, label, phase, text):
        with self.lock:
            n = len(self.state.data["evidence"]) + 1
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)[:60]
            evidence_id = f"{n:03d}_{safe}.txt"
            path = (self.folder / evidence_id).resolve()
            if self.state.run_dir.resolve() not in path.parents:
                raise ValueError("Refusing to write evidence outside the run folder")
            path.write_text(redact(text or ""), encoding="utf-8")
            self.state.data["evidence"].append({
                "id": evidence_id,
                "label": label,
                "phase": phase,
                "created_at": now_iso(),
            })
        self.state.save()
        return evidence_id


def load_state(run_dir):
    """Read a run's state.json (used by the CLI check and the web API)."""
    path = Path(run_dir) / "state.json"
    return json.loads(path.read_text(encoding="utf-8"))