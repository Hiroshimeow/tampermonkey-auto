# Changelog

All notable changes to the Tampermonkey bridge and its role orchestration flow are documented here.

## [1.0.6] - 2026-07-19

Version 1.0.6 turns the read-only flow page into a durable Kanban task control plane while preserving the accepted userscript transport and role-flow behavior.

### Durable tasks and scheduling

- Add atomic, fail-closed `.role_state/tasks.json` persistence with schema validation, optimistic per-task revisions, bounded event history, archive semantics, and six explicit Kanban states.
- Add controller ownership and reservation checks across RUNNING/REVIEW, active requests, and wake dispatch so one physical controller cannot own two active conversational workflows.
- Add manual, one-time, interval, and five-field cron schedules with explicit timezone validation, UTC persistence, missed-run coalescing, pause/resume, and lifecycle-managed scheduler health.
- Add restart-safe wake stages. Ambiguous or old-server dispatches become `UNCERTAIN` instead of replaying.

### Safe controller wakeups

- Add task CRUD, move, wake, pause, resume, generic role inventory, and scheduler health APIs.
- Keep wake prompts role-parameterized and non-executable; controllers must re-read and claim their exact task before starting work.
- Reuse the accepted userscript transport only after proving controller presence, no active command, idle assistant, exact clean composer ownership, and zero attachments.
- Issue exactly one server-side `SET_PROMPT` and one `CLICK_SEND`; the dashboard never calls the browser command endpoint directly.

### Kanban dashboard

- Replace the Phase 03 read-only cards with responsive `BACKLOG`, `READY`, `RUNNING`, `REVIEW`, `BLOCKED`, and `DONE` columns.
- Add create/edit/detail dialogs, drag/drop and keyboard move controls, archive, filters, result/blocker editing, schedule controls, manual wake, pause/resume, and evidence-based uncertain-wake resolution.
- Poll tasks, role inventory, and durable flow independently while retaining last-good data and exposing store, scheduler, connection, and revision conflicts.
- Expose userscript transport and future target metadata as display-only seams; Phase 06 browser-target controls remain absent.

### Documentation and verification

- Update `skills/ORCHESTRATOR.md` with exact task claim, mutation, conflict, duplicate-flow, and wake-resolution rules.
- Add focused task store, scheduler, server API, and dashboard contract coverage.

## [1.0.5] - 2026-07-18

Version 1.0.5 adds read-only visibility for durable semantic flow without changing physical role ownership or command behavior.

### Logical-role hydration

- Hydrate only validated `RUNNING`, `WAITING`, and `DONE` status records returned for the exact physical role poll.
- Whitelist bounded display fields and normalize the logical role for presentation only.
- Show `LOGICAL · STATE` when one physical tab is acting as a different logical role, while avoiding duplicate labels when both roles match.
- Clear stale or invalid local flow display state without deriving or publishing semantic state in the userscript.

### Read-only dashboard

- Serve dependency-free `dashboard.html` at `/dashboard`.
- Read durable flow from `/api/admin/flow` and fan out concurrent GET requests to `/api/admin/role/{role}` every two seconds.
- Show semantic flow, presence, sessions, active command, compact DOM observation, load errors, partial role-detail failures, and stale retained data.
- Add no aggregate JSON API, writer endpoint, control button, external dependency, websocket, or event stream.

### Operator checkpoint

- Restart `server.py` before opening `/dashboard` so the new route is loaded.
- Save/reload `tampermonkey.js` and reload managed role tabs before validating logical-role presentation.
- Live-smoke NEW_CHAT recovery and the dashboard only after both server and userscript reloads.

## [1.0.4] - 2026-07-18

Version 1.0.4 reduces composer-watchdog sampling overhead while preserving the existing stale-draft safety window.

### Composer watchdog

- Add `composer_watchdog_poll_ms` with a default interval of 20 seconds.
- Run the composer watchdog independently every 20 seconds instead of every second.
- Keep the stale composer timeout at 60 seconds.
- Continue comparing the normalized composer text and real attachment list with the previous watchdog signature.
- Restart the 60-second stale window whenever text or attachments change.
- Re-read the composer immediately before cleanup and cancel deletion if the signature changed.
- Continue deferring cleanup while prompt paste, upload, or send transport owns the composer.

### Version presentation

- Bump userscript metadata and runtime identity to dotted version `1.0.4`.

### Verification

- Tampermonkey userscript syntax check passed.
- Tampermonkey contract tests passed.
- Server tests: 40 passed, with 1 warning and 3 subtests.
- Full suite: 290 tests passed, with 1 warning and 3 subtests.
- `git diff --check` passed.
## [1.0.3] - 2026-07-18

Version 1.0.3 hardens role identity and ownership, and makes direct role calls cheaper and easier to coordinate.

### Role ownership lifecycle

- Enforce one current owner for each exact role using a stable per-tab owner ID and a per-assignment claim ID.
- Preserve legitimate ownership across a reload without allowing a previously displaced tab to reclaim the role.
- Reserve claim generations on the server so stale or delayed claims cannot replace newer assignments.
- Reject stale-owner status, report, sync, command, and release operations.
- Release the previous role during intentional role replacement without risking removal of a newer claim.
- Ignore stale assignment responses and schedule exactly one next poll for the current assignment.
- Fail closed when ownership reservation cannot be confirmed.

### Direct role calls and context

- Let `role.py` bootstrap the configured role prompt and skill context once per conversation and context hash.
- Use a compact prompt on later calls in the same valid context.
- Re-bootstrap after a new chat, a context change, or when existing context cannot be verified safely.
- Treat the latest same-role context marker as authoritative, including role context changes such as A -> B -> A.
- Keep the context marker visible when large prompts are spilled to files.
- Leave arbitrary unconfigured role names on the existing lightweight path.

### Workflow guidance

- Clarify `coder.md` as the implementation flow: mandatory PLAN, implementation by the current agent, then the validators named by the user.
- Clarify `orches.md` as the stable orchestration loop: PLAN -> DEV -> REVIEW -> optional user-requested validators; blockers return to PLAN.
- Remove duplicated Mode 1/Mode 2 wording from role prompts and supporting guidance.
- Allow a role to call other roles with `role.py` during its own turn, including parallel independent calls, while the parent still returns the final routing JSON required by `main.py`.

### Version presentation

- Bump userscript metadata and runtime identity to dotted version `1.0.3`.
- Keep the assigned header compact, without `Ver:` or `Role:` prefixes.

### Verification

- Full suite: 290 tests passed, with 1 warning and 3 subtests.
- Direct-role CLI suite: 50 tests passed.

## [1.0.2] - 2026-07-17

Version 1.0.2 introduced visible end-to-end role-flow status and stabilized routed completion.

### Global flow status

- Add shared `RUNNING`, `WAITING`, and `DONE` lifecycle status for flows started by both `main.py` and `role.py`.
- Show the flow origin and handoff using `From`, `Done From`, and `Sent to` across the global UI.
- Publish coordinator routing and parallel fan-in progress through the server-backed status contract.
- Add direct `role.py` lifecycle reporting so one-shot role calls remain attributable to their caller.

### Routed completion

- Stabilize routed flow completion so a flow finishes only after receiving the expected valid routing payload.
- Harden stale and invalid completion handling to prevent an older response from completing the active flow.
- Improve slow-composer handling with a watchdog and a longer send-acceptance window.

### Version presentation

- Bump userscript metadata and runtime identity to dotted version `1.0.2`.
- Add the compact live role-flow header used by the browser bridge.
