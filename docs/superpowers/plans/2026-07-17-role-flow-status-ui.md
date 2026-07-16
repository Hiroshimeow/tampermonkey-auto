# Role Flow Status UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show compact `RUNNING`/`WAITING` state and `From`/`Routed` context only on browser roles participating in the current launcher run.

**Architecture:** Add one run-scoped in-memory status map to the existing backend and expose it through the existing browser poll response. `BridgeClient` publishes bounded role updates; `Coordinator` publishes flow start/route/cleanup transitions, while `role.py` publishes one-role running/cleanup transitions. The userscript only renders the returned record and never derives orchestration state itself.

**Tech Stack:** Python 3, FastAPI/Pydantic, pytest, Tampermonkey JavaScript, Node contract tests.

---

### Task 1: Backend status storage and bridge API

**Files:**
- Modify: `server.py`
- Modify: `apps/bridge.py`
- Test: `tests/test_server.py`
- Test: `tests/test_main_flow.py`

- [ ] **Step 1: Write failing backend tests**

Add tests proving that a bulk update stores only named roles, `/api/status` returns only the polling role's record, and `{role: null}` clears a record only when `run_id` still owns it.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run python -m pytest tests/test_server.py -k flow_status -q`

Expected: FAIL because the flow-status endpoint and poll field do not exist.

- [ ] **Step 3: Implement the minimal backend map**

Add a `FlowStatusRequest` containing `run_id` and `updates: Dict[str, Optional[Dict[str, Any]]]`, plus `DiagnosticState.update_flow_statuses()`. Normalize role keys, accept only `RUNNING` and `WAITING`, store `run_id`, and compare the stored owner before a null update clears it. Add one `POST /api/admin/flow-status` endpoint and return `flow_status` from `/api/status`.

- [ ] **Step 4: Add and test the bridge publisher**

Add `BridgeClient.update_flow_statuses(run_id, updates)` as a thin JSON request wrapper. Verify its exact request payload with a focused unit test.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run: `uv run python -m pytest tests/test_server.py tests/test_main_flow.py -k flow_status -q`

Expected: PASS.

### Task 2: `main.py` orchestration transitions

**Files:**
- Modify: `apps/coordinator.py`
- Modify: `apps/route_executor.py`
- Test: `tests/test_main_flow.py`

- [ ] **Step 1: Write failing transition tests**

Add tests with roles `A,B,C` mapped to physical tabs. Assert initial A is `RUNNING / From: User`, B/C are `WAITING`, A-to-B produces `A WAITING / Routed: B` and `B RUNNING / From: A`, an unreached C remains plain `WAITING`, and no update names a nonparticipant such as `DEV`.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run python -m pytest tests/test_main_flow.py -k flow_ui -q`

Expected: FAIL because `Coordinator` does not publish flow state.

- [ ] **Step 3: Implement run-scoped lifecycle helpers**

Create one `flow_run_id` per coordinator. At `run()` entry publish initial membership, before dispatch publish the target as running, and when a valid route is known publish the source as waiting with `Routed` plus each actual target as running with `From`. In `finally`, publish null only for physical roles registered by this coordinator and this `run_id`. Publication errors are logged and do not alter orchestration.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `uv run python -m pytest tests/test_main_flow.py -k flow_ui -q`

Expected: PASS.

### Task 3: Direct `role.py` lifecycle

**Files:**
- Modify: `role.py`
- Test: `tests/test_role_cli.py`

- [ ] **Step 1: Write failing role lifecycle tests**

Extend the fake bridge to capture flow updates. Assert a direct TEST1 request publishes only TEST1 as `RUNNING / From: User` and clears only TEST1 on both success and runtime failure.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run python -m pytest tests/test_role_cli.py -k flow_status -q`

Expected: FAIL because `role.py` does not publish flow state.

- [ ] **Step 3: Implement publish/cleanup around active browser work**

Generate a run identifier from the existing `run_id`, publish after the bridge client is available, and clear from a `finally` block. Do not name or mutate any other role.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `uv run python -m pytest tests/test_role_cli.py -k flow_status -q`

Expected: PASS.

### Task 4: Compact Tampermonkey rendering

**Files:**
- Modify: `tampermonkey.js`
- Modify: `tests/test_tampermonkey_contract.mjs`

- [ ] **Step 1: Write failing userscript contract tests**

Assert the poll response stores `flow_status`, `updateUI()` renders `RUNNING`, `WAITING`, `From:` and `Routed:` using dedicated compact elements, and absent status removes/hides the block. Assert the literal `QUEUED` is absent.

- [ ] **Step 2: Run the contract test and verify RED**

Run: `node tests/test_tampermonkey_contract.mjs`

Expected: FAIL because flow-status rendering is absent.

- [ ] **Step 3: Implement the two-line UI**

Keep one local `flowStatus` value from `/api/status`. Add a tight block below the role line: small bold red `RUNNING` or amber `WAITING`, followed only when present by a smaller muted `From: X` or `Routed: X`. A missing/invalid record renders the original two-line panel only.

- [ ] **Step 4: Run JavaScript checks and verify GREEN**

Run: `node --check tampermonkey.js`

Run: `node tests/test_tampermonkey_contract.mjs`

Expected: both PASS.

### Task 5: Regression and isolated live verification

**Files:**
- Modify only if a test exposes a defect in the feature files above.

- [ ] **Step 1: Run the full automated suite**

Run: `uv run python -m pytest -q`

Run: `node --check tampermonkey.js`

Run: `node tests/test_tampermonkey_contract.mjs`

Expected: all PASS.

- [ ] **Step 2: Verify backend isolation before live use**

Read status snapshots for `DEV`, `PLAN`, and `REVIEW` without creating commands. Record them only as a before/after guard; do not publish flow state to those roles.

- [ ] **Step 3: Live-test only TEST1 and TEST2**

Run a bounded TEST1/TEST2 flow and verify TEST1 starts `RUNNING / From: User`, TEST2 starts `WAITING`, and route transitions update only those two tabs. Do not reload or command any other role.

- [ ] **Step 4: Recheck nonparticipant isolation and commit**

Confirm `DEV`, `PLAN`, and `REVIEW` command/state records were not changed by the live test. Stage only intended feature/test/plan files and commit the implementation.
