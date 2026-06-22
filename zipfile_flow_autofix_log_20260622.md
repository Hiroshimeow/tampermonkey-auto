# zipfile_flow autofix log - 2026-06-22

## Scope
- Only valid agent roles are A/B/C/T.
- G/H are intentionally excluded from the flow.
- D does not exist and must not be targeted.

## Root cause found
The previous Tampermonkey bridge selected the first page-wide `button[data-testid="send-button"]` / `aria-label="Send prompt"` match before checking whether it was visible, enabled, and scoped to the composer. In the failing run, that selected a hidden/disabled Send button and ignored composer-scoped alternatives. The global send-score dump was also polluted by sidebar buttons whose labels contained the word "Send" from chat titles.

## Fixes applied to tampermonkey.js
1. Added explicit separation between visibility and disabled/actionability:
   - `isVisible(el)` now checks hidden state, CSS display/visibility, and non-zero bounding box.
   - `isDisabled(el)` handles disabled/readOnly/aria-disabled/disabled attribute.
   - `isClickableSendButton(button)` now requires both visible and not disabled.
2. Made `composerElement()` choose a visible, enabled composer candidate instead of the first global selector match.
3. Narrowed composer root selection to `form`, `[data-testid="composer"]`, or `[data-testid="composer-root"]`; it no longer escalates to broad `main` unless unavailable.
4. Changed scoped send candidate sorting to prioritize clickable buttons before raw score.
5. Rewrote `findSendButton()` to prefer composer-scoped clickable candidates before global direct selector candidates. Disabled/hidden candidates are returned only for diagnostics.
6. Reworked `handleClickSend()` so it waits for a clickable Send button before failing and logs composer/button diagnostics if it never becomes actionable.
7. Added form `requestSubmit()` fallback after a click attempt when accepted-state detection does not advance.

## Notebook role check
`zipfile_flow.ipynb` already has:

```python
ACTIVE_ROLES = ["C", "A", "T", "B"]
```

and later rejects any coordinator target not in `ACTIVE_ROLES`, so G/H/D will not run unless the notebook is edited incorrectly later.

## Execution status
- Attempted default project test runner: it executed `npm test` and failed because this project has no `package.json`. This is not a flow/runtime failure.
- Attempts to run explicit Python tests, JS syntax checks, ESLint, shell commands, or execute the notebook through the available gateway were blocked by the tool safety layer.
- The live Chrome tabs still need the updated `tampermonkey.js` pasted/refreshed. The currently loaded browser script is almost certainly the old one, because the failed notebook output still reports `selection_strategy: "direct_selector"`, which the fixed script no longer uses for the hidden disabled send-button case.

## Required next manual step
Paste the updated `tampermonkey.js` into Tampermonkey and refresh the A/B/C/T ChatGPT tabs. Then rerun `zipfile_flow.ipynb` from the first cell.

## Expected diagnostic after reload
For a valid composer Send button, `FIND_SEND`/`CLICK_SEND` should report one of:
- `selection_strategy: "composer_scoped_clickable"`
- `selection_strategy: "direct_selector_clickable"` only if the direct selector is visible and enabled

If it still reports `selection_strategy: "direct_selector"`, Chrome is still running the old script.
