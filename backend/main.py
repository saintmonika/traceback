"""Web server and API. Local only: 127.0.0.1, port 5050.

Start from the repository root, with the virtual environment active and
the API key loaded:
    python3 -m backend.main
"""

import json
import re
import threading
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, Response

from . import config
from .controller import Controller
from .crusoe_client import api_key_present, MISSING_KEY_MESSAGE
from .report import build_report
from .state import RunState

WEB_DIR = config.REPO_ROOT / "web"
RUN_ID_RE = re.compile(r"^run_\d{8}_\d{6}(_\d+)?$")

app = Flask(__name__, static_folder=None)
_lock = threading.Lock()
_active = {"controller": None, "thread": None}


def _running():
    t = _active["thread"]
    return t is not None and t.is_alive()


def _state_path(run_id):
    if not RUN_ID_RE.match(run_id or ""):
        return None
    p = config.RUNS_ROOT / run_id / "state.json"
    return p if p.is_file() else None


def _read_state(run_id):
    p = _state_path(run_id)
    if not p:
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    c = _active["controller"]
    data["live"] = bool(c and c.run_id == run_id and _running())
    return data


# ------------------------------------------------------------------- pages
@app.get("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.get("/app.js")
def app_js():
    return send_from_directory(WEB_DIR, "app.js")


@app.get("/styles.css")
def styles():
    return send_from_directory(WEB_DIR, "styles.css")


# --------------------------------------------------------------------- API
@app.get("/api/info")
def info():
    return jsonify({
        "fixture_name": Path(config.FIXTURE_PATH).name,
        "models": {"planner": config.PLANNER_MODEL, "repair": config.REPAIR_MODEL,
                   "gap": config.GAP_MODEL},
        "key_loaded": api_key_present(),
        "running": _running(),
    })


@app.post("/api/runs")
def start_run():
    body = request.get_json(silent=True) or {}
    goal = str(body.get("goal", "")).strip()
    if not goal:
        return jsonify({"error": "Type a one-line goal first."}), 400
    if len(goal) > 500:
        return jsonify({"error": "The goal must be one line (under 500 characters)."}), 400
    if not api_key_present():
        return jsonify({"error": MISSING_KEY_MESSAGE}), 400
    with _lock:
        if _running():
            return jsonify({"error": "A run is already in progress."}), 409
        controller = Controller(goal)
        thread = threading.Thread(target=controller.run, daemon=True)
        _active["controller"], _active["thread"] = controller, thread
        thread.start()
    return jsonify({"run_id": controller.run_id})


@app.get("/api/runs/latest")
def latest_run():
    if not config.RUNS_ROOT.is_dir():
        return jsonify({})
    runs = sorted(p.name for p in config.RUNS_ROOT.iterdir()
                  if p.is_dir() and RUN_ID_RE.match(p.name) and (p / "state.json").is_file())
    if not runs:
        return jsonify({})
    return jsonify(_read_state(runs[-1]) or {})


@app.get("/api/runs/<run_id>")
def get_run(run_id):
    data = _read_state(run_id)
    if data is None:
        return jsonify({"error": "Run not found."}), 404
    return jsonify(data)


@app.post("/api/runs/<run_id>/stop")
def stop_run(run_id):
    c = _active["controller"]
    if not c or c.run_id != run_id or not _running():
        return jsonify({"error": "That run is not running."}), 409
    c.request_stop()
    return jsonify({"stopping": True})


@app.get("/api/runs/<run_id>/evidence/<evidence_id>")
def get_evidence(run_id, evidence_id):
    data = _read_state(run_id)
    if data is None:
        return jsonify({"error": "Run not found."}), 404
    listed = {e["id"] for e in data.get("evidence", [])}
    if evidence_id not in listed:          # only IDs listed in state are served
        return jsonify({"error": "Evidence not found."}), 404
    path = config.RUNS_ROOT / run_id / "evidence" / evidence_id
    return Response(path.read_text(encoding="utf-8"), mimetype="text/plain; charset=utf-8")


def resume_interrupted_run():
    """On server start: if the most recent run is still marked running, it was
    interrupted. Resume it from the first incomplete phase (rebuilt from saved state)."""
    if not config.RUNS_ROOT.is_dir():
        return
    runs = sorted(p.name for p in config.RUNS_ROOT.iterdir()
                  if p.is_dir() and RUN_ID_RE.match(p.name) and (p / "state.json").is_file())
    if not runs:
        return
    run_id = runs[-1]
    root = config.RUNS_ROOT / run_id
    data = json.loads((root / "state.json").read_text(encoding="utf-8"))
    if data.get("status") != "running":
        return                                   # final statuses are never resumed
    if not api_key_present():
        st = RunState(root, data)
        st.add_decision("stop", "Cannot resume: API key not loaded in this Terminal.")
        data["status"] = "blocked"
        data["report"] = build_report(data)
        st.save()
        print(f"Run {run_id} was interrupted but cannot resume: API key not loaded in this Terminal.")
        return
    controller = Controller.for_resume(run_id, data, log=print)
    controller.mark_resumed()
    thread = threading.Thread(target=controller.run, daemon=True)
    _active["controller"], _active["thread"] = controller, thread
    thread.start()
    print(f"Resumed interrupted run {run_id} from {controller.data['resumed'][-1]['phase']}.")


if __name__ == "__main__":
    resume_interrupted_run()
    if not api_key_present():
        print("Warning: " + MISSING_KEY_MESSAGE)
        print("The page will open, but runs will be refused until the key is loaded.")
    print(f"Traceback running at http://{config.SERVER_HOST}:{config.SERVER_PORT}")
    app.run(host=config.SERVER_HOST, port=config.SERVER_PORT, debug=False, threaded=True)
