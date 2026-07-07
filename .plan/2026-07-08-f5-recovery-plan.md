# F5 / Mid-Response Reload Recovery Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make response completion detection more skeptical after F5 or mid-response reload so `role.py` does not accept a stale or prematurely-finished result when the browser UI briefly looks done but the assistant response has not truly stabilized.

**Architecture:** Keep the primary fix in `apps/bridge.py`, because that layer owns response completion and reload recovery semantics for both `role.py` and coordinator flows. Strengthen `wait_assistant_done()` and `wait_for_current_response()` to require stronger post-reload stabilization signals instead of trusting `stop_visible == false` alone, then add narrowly scoped tests that reproduce the stale-after-reload cases.

**Tech Stack:** Python 3.12+, pytest, existing `BridgeClient` recovery flow, Tampermonkey bridge snapshots.

---

## File Structure

- Modify: `apps/bridge.py`
  - Add a small helper for post-reload / post-active stabilization decisions.
  - Tighten the acceptance rules in `wait_assistant_done()` and `wait_for_current_response()`.
  - Preserve existing manual-input and choice-prompt protections.
- Modify: `tests/test_main_flow.py`
  - Add failing tests for F5-like stale completion and post-reload false-done behavior.
- Optional modify: `tests/test_role_cli.py`
  - Add or adjust one CLI-level regression test only if the bridge-layer behavior needs an end-to-end assertion through `role.py`.
- Optional modify: `apps/selftest.py`
  - Add one light selftest only if the new helper is important enough to protect outside pytest.

## Design Notes To Preserve During Implementation

- Do **not** move the main fix into `role.py` unless bridge-layer hardening proves insufficient.
- The root problem is not "F5 exists" but "response completion was inferred from a weak signal after reload".
- Keep the runtime conservative: if unsure, wait/sync/recover more rather than returning a stale answer.
- Do not regress existing recovery features:
  - incomplete JSON/code block detection,
  - safe choice prompt clicking,
  - manual composer input protection,
  - one-time active reload when a response is stuck.
- Prefer a small helper with a precise name over spreading more ad-hoc conditionals through the wait loop.

## Proposed Behavioral Rule

After reload or any recovery path where a response may have been interrupted mid-generation, the bridge should not immediately accept "response exists + stop hidden" as done unless at least one of these is true:

1. the response changed after recovery and is not incomplete,
2. the response is observed as stable across at least one additional transcript sync,
3. the UI has clearly returned to a clean composer state and the response is complete enough to trust.

In practice, this means the bridge needs a "skeptical after reload" phase that forces one extra confirmation pass before returning stale text.

---

### Task 1: Add failing bridge tests for stale-after-reload false completion

**Files:**
- Modify: `tests/test_main_flow.py`
- Test: `tests/test_main_flow.py`

- [ ] **Step 1: Add a failing test for stale text immediately after reload**

Add a test near the existing `wait_for_current_response` recovery tests that simulates:
- streaming partial response,
- forced reload path,
- first post-reload snapshot shows old/stale response with `stop_visible=False`,
- next sync produces the real final response.

The expected behavior should be: `wait_for_current_response()` returns the later final response, not the first stale one.

Suggested shape:

```python
def test_wait_for_current_response_does_not_accept_first_stale_response_after_reload() -> None:
    bridge = FakeBridge([
        response_snapshot("partial answer", True),
        response_snapshot("old stale answer", False),
        response_snapshot("final recovered answer", False),
    ])

    response = bridge.wait_for_current_response(
        "DEV",
        timeout_s=2.0,
        active_wait_s=0.0,
        page_wait_s=0.01,
        poll_s=0.01,
    )

    assert response == "final recovered answer"
    assert "RELOAD_PAGE" in bridge.commands
```

- [ ] **Step 2: Add a failing test for `ASSISTANT_DONE` returning stale-looking done state after reload**

Add a test that drives `call_browser_role()` through `WAIT_ASSISTANT_DONE`, where the command path says done but transcript recovery still advances afterward. The bridge should resync and return the recovered final answer.

Suggested shape:

```python
def test_call_browser_role_resyncs_when_done_signal_arrives_before_recovered_response_stabilizes() -> None:
    bridge = FakeBridge(
        [
            response_snapshot("old response", False),
            response_snapshot("old response", False),
            response_snapshot("stale post-reload answer", False),
            response_snapshot("final recovered route", False),
        ],
        command_results={
            "WAIT_ASSISTANT_DONE": [
                {"done": True, "status": "ASSISTANT_DONE", "result": {"text": "stale post-reload answer"}},
            ],
        },
    )

    response = bridge.call_browser_role("PLAN", "automated prompt", timeout_s=2.0)

    assert response == "final recovered route"
    assert "SYNC_TRANSCRIPT" in bridge.commands
```

- [ ] **Step 3: Run only the new/nearby tests and verify they fail for the intended reason**

Run:

```bash
cd /e/python_project/tampermonkey_auto
pytest tests/test_main_flow.py -k "stale_response_after_reload or stabilizes" -v
```

Expected: FAIL because the current bridge returns the stale first post-reload answer too early.

- [ ] **Step 4: Commit the red tests**

```bash
git add tests/test_main_flow.py
git commit -m "test: cover stale response recovery after reload"
```

---

### Task 2: Harden `wait_for_current_response()` with a post-reload stabilization pass

**Files:**
- Modify: `apps/bridge.py`
- Test: `tests/test_main_flow.py`

- [ ] **Step 1: Add a tiny helper to express the new trust rule**

In `apps/bridge.py`, add a small helper near `looks_incomplete_response()` or the response-activity helpers. Suggested intent:

```python
def should_accept_recovered_response(
    self,
    activity: ResponseActivity,
    *,
    last_response: str,
    recovery_suspected: bool,
    stable_done_samples: int,
) -> bool:
    ...
```

The helper should encode a narrow rule set, for example:
- never accept incomplete response,
- if not in recovery-suspected mode, current done logic is fine,
- if in recovery-suspected mode and response equals the last known response, require one extra stable observation before accepting,
- if in recovery-suspected mode and response advanced to a new complete value, allow acceptance.

Keep the helper simple. No large refactor.

- [ ] **Step 2: Track whether the current wait loop is in a skeptical post-reload phase**

Inside `wait_for_current_response()` add minimal state such as:
- `recovery_suspected = False`
- `stable_done_samples = 0`

Set `recovery_suspected = True` after the method triggers `RELOAD_PAGE`, and optionally also when the method sees an unexpectedly non-active but suspicious post-reload state.

- [ ] **Step 3: Change the done/has-response branches to require an extra confirmation in recovery-suspected mode**

Update the logic around:
- `if self.is_response_done(activity): return activity.response`
- `if not self.is_response_active(activity): ... if activity.has_response: return activity.response`

New behavior:
- If `recovery_suspected` is false, preserve current behavior.
- If `recovery_suspected` is true and the response is complete but could be stale, do **one more** `SYNC_TRANSCRIPT` cycle instead of returning immediately.
- Only return after the response either:
  - changes to a newer complete value, or
  - remains complete and stable for the required extra sample count.

This should specifically protect the F5 case where stop disappears before transcript recovery catches up.

- [ ] **Step 4: Keep existing guards intact**

Verify in code while editing that these branches still happen first and remain semantically unchanged:
- manual composer input blocks auto action,
- choice prompts get clicked,
- incomplete response keeps waiting,
- active stuck response still reloads once.

- [ ] **Step 5: Run the targeted tests and verify they now pass**

Run:

```bash
cd /e/python_project/tampermonkey_auto
pytest tests/test_main_flow.py -k "stale_response_after_reload or stabilizes or recovered_response or wait_for_current_response" -v
```

Expected: the new reload/F5 tests pass without regressing nearby recovery tests.

- [ ] **Step 6: Commit the bridge hardening**

```bash
git add apps/bridge.py tests/test_main_flow.py
git commit -m "fix: harden response recovery after reload"
```

---

### Task 3: Harden `wait_assistant_done()` against stale done signals

**Files:**
- Modify: `apps/bridge.py`
- Test: `tests/test_main_flow.py`

- [ ] **Step 1: Adjust `wait_assistant_done()` to distrust suspicious `ASSISTANT_DONE` payloads in recovery-like situations**

Right now `wait_assistant_done()` only falls back when:
- status is timeout / not done,
- command errors,
- response text is empty or structurally incomplete.

Extend it slightly so that after a done result it can optionally force transcript re-validation before returning when the done text is complete-looking but may still be stale.

Suggested strategy:
- if `ASSISTANT_DONE` text is empty or incomplete, keep current behavior,
- if text is complete-looking, compare it against the current synced transcript state via `wait_for_current_response(..., require_response=True)` or a lighter helper,
- return the newer of the validated transcript result and the direct done text,
- keep this logic conservative and local so the method does not become tangled.

The simplest acceptable version is to call the current recovery wait path whenever the done text looks plausible but there is reason to suspect reload/lag ambiguity.

- [ ] **Step 2: Keep the normal fast path fast**

Do not turn every healthy `ASSISTANT_DONE` into a long wait. The extra validation should be narrow and targeted to ambiguous completion cases.

- [ ] **Step 3: Run the focused `call_browser_role` tests**

Run:

```bash
cd /e/python_project/tampermonkey_auto
pytest tests/test_main_flow.py -k "assistant_done or call_browser_role" -v
```

Expected: the new stale-done regression passes and the existing empty/incomplete-response recovery tests continue to pass.

- [ ] **Step 4: Commit the assistant-done validation**

```bash
git add apps/bridge.py tests/test_main_flow.py
git commit -m "fix: validate assistant done after reload ambiguity"
```

---

### Task 4: Decide whether a `role.py` regression test is needed

**Files:**
- Optional modify: `tests/test_role_cli.py`

- [ ] **Step 1: Review whether bridge-layer tests already fully protect the bug**

If the bug is entirely inside `BridgeClient` and no `role.py` branching changes were made, you may skip this task.

- [ ] **Step 2: If needed, add exactly one CLI-level regression test**

Only if `role.py` behavior changed indirectly or a previous false-complete path was observable at the CLI level, add one concise test that proves `role.main()` returns the recovered final answer rather than the stale post-reload answer.

Keep it small. Do not duplicate all bridge tests at the CLI layer.

- [ ] **Step 3: Run the specific CLI test subset**

Run:

```bash
cd /e/python_project/tampermonkey_auto
pytest tests/test_role_cli.py -k "recovered or stale or reload" -v
```

- [ ] **Step 4: Commit only if this task was needed**

```bash
git add tests/test_role_cli.py
git commit -m "test: cover role cli stale recovery path"
```

---

### Task 5: Full verification and cleanup

**Files:**
- Modify if needed: `apps/bridge.py`
- Modify if needed: `tests/test_main_flow.py`
- Optional modify: `tests/test_role_cli.py`

- [ ] **Step 1: Run the full targeted recovery suite**

Run:

```bash
cd /e/python_project/tampermonkey_auto
pytest tests/test_main_flow.py tests/test_role_cli.py -v
```

Expected:
- all new reload/F5 regression tests pass,
- no regressions in manual-input, choice-prompt, upload recovery, or role unhealthy flows.

- [ ] **Step 2: If any test fails, fix one root cause at a time**

Do not layer speculative changes. Re-run the narrowest failing subset first, then the full targeted suite again.

- [ ] **Step 3: Optionally run broader repo checks if the human wants extra confidence**

Suggested broader checks:

```bash
cd /e/python_project/tampermonkey_auto
uv run python -m pytest
node --check ./tampermonkey.js
node ./tests/test_tampermonkey_contract.mjs
```

- [ ] **Step 4: Review the final diff for scope control**

Confirm the diff is limited to:
- response recovery skepticism after reload,
- tests proving the stale/done ambiguity is fixed,
- no unrelated refactor.

- [ ] **Step 5: Create the final implementation commit**

If there were follow-up polish edits after prior commits:

```bash
git add apps/bridge.py tests/test_main_flow.py tests/test_role_cli.py
 git commit -m "chore: finalize reload recovery hardening"
```

Use this commit only if there are real staged changes left.

---

## Risks and Edge Cases To Watch During Implementation

- Returning too slowly in the healthy non-reload path because the new skepticism was applied too broadly.
- Infinite waiting if the stabilization rule requires a new response that never arrives; prefer “one extra confirmation sample” over unbounded skepticism.
- Regressing the case where the final response legitimately finishes and the composer is temporarily absent.
- Treating a stable but valid final response as stale forever because the acceptance rule is too strict.
- Breaking the manual-input rule by letting recovery logic overwrite user-steered composer content.

## Acceptance Criteria

The implementation is complete when all of the following are true:

1. A test reproduces the F5-like case where the first post-reload snapshot looks done but still contains stale text.
2. The bridge no longer returns that stale text prematurely.
3. Existing incomplete-response and timeout recovery tests still pass.
4. Manual composer input and choice prompt protections still behave as before.
5. `role.py` benefits from the fix without needing broad orchestration changes.
