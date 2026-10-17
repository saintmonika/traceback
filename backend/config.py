"""Traceback configuration.

Every value that might change lives here. These are settings, not secrets.
The API key is NEVER stored here; it is read from the CRUSOE_API_KEY
environment variable at call time.
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent

# The only reference to the test input. Change this one line to point
# Traceback at a different project.
FIXTURE_PATH = "fixture/pricing-demo"
RUNS_DIR = "runs"

FIXTURE_DIR = REPO_ROOT / FIXTURE_PATH
RUNS_ROOT = REPO_ROOT / RUNS_DIR

# ---------------------------------------------------------------------------
# Crusoe Managed Inference (OpenAI-compatible)
# ---------------------------------------------------------------------------
CRUSOE_BASE_URL = "https://api.inference.crusoecloud.com/v1"
API_KEY_ENV_VAR = "CRUSOE_API_KEY"

# Model IDs by role. Swap a model by changing one line.
PLANNER_MODEL = "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B"
REPAIR_MODEL = "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B"
GAP_MODEL = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"

# Tried in this order if a model fails the first-hour test.
BACKUP_MODELS = [
    "nvidia/Nemotron-3.5-Lightning-30B-A3B",
    "openai/gpt-oss-120b",
    "deepseek-ai/DeepSeek-V4-Pro",
]

TEMPERATURE = 0.2

MAX_TOKENS_BY_ROLE = {
    "planner": 2000,
    "repair": 2000,
    "gap": 1200,
    "first_hour": 2000,
}


def model_for(role):
    """Return the configured model ID for an agent role."""
    return {
        "planner": PLANNER_MODEL,
        "repair": REPAIR_MODEL,
        "gap": GAP_MODEL,
        "first_hour": PLANNER_MODEL,
    }[role]


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------
MAX_PLANNER_ATTEMPTS = 3
MAX_REPAIR_ATTEMPTS = 3
MAX_GAP_ATTEMPTS = 2          # per surviving mutation
MAX_SURVIVORS_TO_CLOSE = 2
MAX_MODEL_CALLS = 12          # per run, all roles combined
MAX_MUTATIONS = 5
MODEL_TIMEOUT_SECONDS = 120
TEST_TIMEOUT_SECONDS = 60


def limits_dict():
    """Copy of the limits, stored in each run's state file."""
    return {
        "MAX_PLANNER_ATTEMPTS": MAX_PLANNER_ATTEMPTS,
        "MAX_REPAIR_ATTEMPTS": MAX_REPAIR_ATTEMPTS,
        "MAX_GAP_ATTEMPTS": MAX_GAP_ATTEMPTS,
        "MAX_SURVIVORS_TO_CLOSE": MAX_SURVIVORS_TO_CLOSE,
        "MAX_MODEL_CALLS": MAX_MODEL_CALLS,
        "MAX_MUTATIONS": MAX_MUTATIONS,
        "MODEL_TIMEOUT_SECONDS": MODEL_TIMEOUT_SECONDS,
        "TEST_TIMEOUT_SECONDS": TEST_TIMEOUT_SECONDS,
    }


# ---------------------------------------------------------------------------
# Set file names for agent-written tests (chosen by the controller,
# never by a model)
# ---------------------------------------------------------------------------
DONE_TEST_FILE = "tests/test_traceback_done.py"
GAP_TEST_FILES = [
    "tests/test_traceback_gap_1.py",
    "tests/test_traceback_gap_2.py",
]

# ---------------------------------------------------------------------------
# Web server (local only). Port 5000 is avoided: macOS uses it for AirPlay.
# ---------------------------------------------------------------------------
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 5050