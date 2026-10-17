# Traceback

**Trace the fix.**

A fix passing checks is not the same as evidence that the fix is right.

Traceback turns a one-line goal into a checkable definition of done, repairs a bug in an isolated workspace, audits its own repaired code with generated mutations, closes real verification gaps with agent-written checks, and reports an honest, bounded outcome.

Built during the Recursive Agents Hackathon, Saturday October 17, 2026 (problem statement: *Done Means Done*).

---

## What happens in one run

A controller written in ordinary Python runs the whole investigation with no further input. Models only propose; the controller decides, and every decision is based on real command output.

1. **Baseline**: runs the project's existing tests on an isolated copy. They must pass.
2. **Define done**: a planner agent writes a definition of done (required behavior, what must be preserved, assumptions with their basis, allowed files) and one new test for the goal.
3. **Validate the definition**: the controller runs that test on the *original* code. It must fail with an assertion failure, which proves it detects the problem. Otherwise it is rejected and the planner tries again.
4. **Repair**: a repair agent returns the complete new content of one allowed file. The controller rejects out-of-scope, unchanged, repeated, unparseable, or oversized patches. A **loop guard** watches the failures: if two repair attempts in a row fail the checks the same way (same outcome and same failing tests), the controller re-plans once with a note that the previous approach failed twice; if it happens again, the run stops as *Attempt limit reached*.
5. **Checks**: the controller applies the patch to the isolated copy only and runs the full suite.
6. **Mutation audit**: the controller generates small deliberate faults *only on the lines the repair changed* (comparison swaps, number shifts by plus or minus 1, condition removal) and runs the full suite against each one in a fresh copy. Caught means the tests would notice a wrong fix. Survived means they would not.
7. **Close the gap**: for each surviving variant, a gap-closer agent writes one stronger test. The controller accepts it only if it **passes on the repair and fails on the variant**. Accepted tests are labeled *agent-written, flagged for review*.
8. **Final checks and report**: the full suite must pass again, closed variants are re-run and must now be caught, and the original project is hashed again. The report is assembled from run data with fixed wording.

Final statuses: *Verified within scope*, *Verification gap remains*, *Blocked*, *Attempt limit reached*, *Stopped*, *Failed*. The interface never says "fixed".

## Example result from a real run during the event

Goal typed: *Make sure orders of exactly 100 dollars receive the 10% discount.*

- The planner's test failed on the original code, as required.
- The repair changed one line of `pricing.py`. All 9 tests passed.
- 4 variants of that line were tested: 3 caught, 1 survived (changing 100 to 99).
- The gap-closer added a test that an order of 99 dollars gets no discount. It passed on the repair and failed on the variant.
- Status: **Verified within scope**. About 4 model calls; roughly 25 to 60 seconds per run. Original project unchanged.

Agent outputs vary between runs; every run's evidence is saved under `runs/<run_id>/evidence/`.

## Test input (disclosure)

`fixture/pricing-demo/` is a copy of the open-source sample project https://github.com/saintmonika/pricing-demo (MIT license, published October 16, 2026, before the event), committed on its own without its `.git` folder. Traceback's code does not refer to anything specific in this project; the only reference is `FIXTURE_PATH` in `backend/config.py`. Agents receive only the goal, the project files, and run results, never the expected fix.

## Models

All model calls go through **Crusoe Managed Inference** (OpenAI-compatible API):

- Planner and repair agent: `nvidia/NVIDIA-Nemotron-3-Super-120B-A12B`
- Gap-closer: `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B`

Agents reply with one small JSON object for their decisions plus at most one code block between `BEGIN_CODE` / `END_CODE` lines, so code never travels inside JSON. A reply that does not parse gets one follow-up that says exactly what was wrong. Every call is counted (limit 12 per run) and saved as evidence.

## Run it

Requirements: macOS or Linux, Python 3, a Crusoe API key.

```
python3 -m venv venv
source venv/bin/activate
python3 -m pip install -r requirements.txt
read -s CRUSOE_API_KEY        # paste the key, press Enter (nothing is shown)
export CRUSOE_API_KEY
```

Web interface (local only):

```
python3 -m backend.main
```

Then open http://127.0.0.1:5050 (port 5000 is avoided because macOS uses it for AirPlay).

From Terminal instead:

```
python3 -m backend.cli "Make sure orders of exactly 100 dollars receive the 10% discount."
```

Builder's check that compiled-file caches cannot affect mutation results (runs one caught variant twice from fresh copies):

```
python3 -m backend.mutation <run_id>
```

## Project layout

```
backend/
  config.py         model IDs, paths, limits, fixed test file names
  crusoe_client.py  model calls, reply parsing, call counting, evidence
  agents.py         planner, repair agent, gap-closer: prompts and validation
  controller.py     the state machine; owns every decision
  test_runner.py    runs pytest with a fixed command, caches cleared, key removed
  workspace.py      run folders, isolated copies, diffs, changed lines
  mutation.py       mutation generator and audit
  state.py          state.json (atomic writes) and numbered evidence files
  report.py         report assembly with fixed wording
  cli.py            one run from Terminal
  main.py           Flask server and API (127.0.0.1:5050)
web/                index.html, styles.css, app.js (no framework)
fixture/            the test input (see disclosure above)
runs/               created at runtime, ignored by git
```

## Safety and honesty

- The original project is never modified. Every run works on copies inside `runs/<run_id>/`, every write is checked to stay inside that folder, and the fixture is hashed before and after each run.
- The API key is read only from the `CRUSOE_API_KEY` environment variable. It is never written to a file, printed, or saved in evidence, and it is removed from the environment of every test process.
- Commands are fixed in code. Models never supply commands, and test file names are chosen by the controller.
- The page renders only what is in the run's `state.json`. The animated dots appear only while a phase is actually running.
- Traceback is not a secure sandbox. It supports only the trusted fixture project.

## Not implemented

- Kill-and-resume from saved state
- Support for projects other than the fixture
- Applying the patch to the original project
- Watch mode, ticket queues, parallel agents
