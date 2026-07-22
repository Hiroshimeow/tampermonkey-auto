# Role Flow Status UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show compact `RUNNING`/`WAITING`/`DONE` state only on participating roles and prevent premature flow termination on incomplete route rendering.

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

Add tests proving that a bulk update stores only named roles, including `DONE`, `/api/status` returns only the polling role's record, and `{role: null}` clears a record only when `run_id` still owns it.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run python -m pytest tests/test_server.py -k flow_status -q`

Expected: FAIL because the flow-status endpoint and poll field do not exist.

- [ ] **Step 3: Implement the minimal backend map**

Add a `FlowStatusRequest` containing `run_id` and `updates: Dict[str, Optional[Dict[str, Any]]]`, plus `DiagnosticState.update_flow_statuses()`. Normalize role keys, accept only `RUNNING`, `WAITING`, and `DONE`, store `run_id`, and compare the stored owner before a null update clears it. Add one `POST /api/admin/flow-status` endpoint and return `flow_status` from `/api/status`.

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

Add tests with roles `A,B,C` mapped to physical tabs. Assert initial A is `RUNNING / From: User`, B/C are `WAITING`, A-to-B produces `A DONE / From: A` and `B RUNNING / From: A`, an unreached C remains plain `WAITING`, and no update names a nonparticipant such as `DEV`.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `uv run python -m pytest tests/test_main_flow.py -k flow_ui -q`

Expected: FAIL because `Coordinator` does not publish flow state.

- [ ] **Step 3: Implement run-scoped lifecycle helpers**

Create one `flow_run_id` per coordinator. At `run()` entry publish initial membership, before dispatch publish the target as running, and when a valid route is known publish the source as done with `From: source` plus each actual target as running with `From: source`. In `finally`, publish null only for physical roles registered by this coordinator and this `run_id`. Publication errors are logged and do not alter orchestration.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `uv run python -m pytest tests/test_main_flow.py -k flow_ui -q`

Expected: PASS.

### Task 3: Direct `role.py` lifecycle

**Files:**
- Modify: `role.py`
- Test: `tests/test_role_cli.py`

- [ ] **Step 1: Write failing role lifecycle tests**

Extend the fake bridge to capture flow updates. Assert a direct TEST1 request publishes only TEST1 as `RUNNING`, with no route detail, and clears only TEST1 on both success and runtime failure.

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

Assert the poll response stores `flow_status`, `updateUI()` renders `RUNNING`, `WAITING`, `DONE`, and `From:` using dedicated compact elements, and absent status removes/hides the block. Assert the literal `QUEUED` is absent.

- [ ] **Step 2: Run the contract test and verify RED**

Run: `node tests/test_tampermonkey_contract.mjs`

Expected: FAIL because flow-status rendering is absent.

- [ ] **Step 3: Implement the two-line UI**

Keep one local `flowStatus` value from `/api/status`. Add a tight block below the role line: small bold red `RUNNING`, amber `WAITING`, or green `DONE`, followed only when present by a smaller muted `From: X`. A missing/invalid record renders the original two-line panel only.

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

### Task 6: Send readiness and independent stale-composer watchdog

**Files:**
- Modify: `tampermonkey.js`
- Modify: `tests/test_tampermonkey_contract.mjs`

- [ ] **Step 1: Write failing contract and behavior tests**

Test that route submission observes a missing/disabled Send button without clicking and succeeds only after the same owned prompt exposes a visible enabled button. Test the watchdog state machine: clean resets, unchanged dirty content clears at 60 seconds, and changed content restarts the full 60-second window.

- [ ] **Step 2: Run JavaScript tests and verify RED**

Run: `node tests/test_tampermonkey_contract.mjs`

Expected: FAIL because the independent watchdog and testable send-readiness wait do not exist.

- [ ] **Step 3: Implement the minimal independent watchdog**

Add one one-second interval and a small pure state transition helper. Signature normalized text and real attachment metadata. On stale unchanged input, clear composer text and click only composer-scoped remove-file/remove-attachment controls. Reset timer state after cleanup or whenever composer is clean.

- [ ] **Step 4: Reuse a testable readiness wait in CLICK_SEND**

Poll ownership, attachments, and the current Send button until it is visible and enabled or the existing send-accept deadline expires. Refresh all evidence immediately before clicking.

- [ ] **Step 5: Verify, live-test only TEST1/TEST2, and send to DEBATE**

Run JS checks and the full Python suite, F5 only `TEST1`, `TEST2`, and `DEBATE` after userscript changes, exercise only TEST1/TEST2 for live transport, then call `role.py --role DEBATE --timeout 3600` repeatedly until review returns PASS.

### Task 7: JSON-gated completion and long-command liveness

**Files:**
- Modify: `tampermonkey.js`
- Modify: `server.py`
- Modify: `apps/bridge.py`
- Modify: `apps/coordinator.py`
- Test: `tests/test_tampermonkey_contract.mjs`
- Test: `tests/test_server.py`
- Test: `tests/test_main_flow.py`

- [ ] **Step 1: Reproduce the observed premature completion**

Add tests proving bare `JSON` remains incomplete, an initially invalid response can become a valid route during bounded re-sync, and recent sync/report activity keeps a long-command tab live even when `/api/status` heartbeat is old.

- [ ] **Step 2: Verify RED**

Run the focused JavaScript, server, and coordinator tests. Expected: fail on bare-language completion, false-offline repair, and missing re-sync recovery.

- [ ] **Step 3: Implement the minimal completion guards**

Reject empty language-only placeholders in `looksIncompleteAssistantText`. Track recent browser activity on status, sync, and report endpoints. Before format repair, perform a bounded latest-response re-sync and re-parse; only send repair if the stable response is still invalid.

- [ ] **Step 4: Verify flow authority**

Add assertions that valid role JSON routes normally and only authorized valid `FINISH` JSON completes the overall flow. Invalid/prose output must remain non-terminal.

- [ ] **Step 5: Run live regression and review**

F5 only TEST1, TEST2, and DEBATE; reproduce a routed TEST1-to-TEST2 flow; run DEBATE until `VERDICT: PASS`; then commit and push only intended files.
