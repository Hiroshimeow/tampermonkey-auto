# Role Flow Status UI Design

## Goal

Show the current orchestration state in each participating Tampermonkey role panel without changing the existing browser-command model.

## Visible behavior

The existing version and role lines stay unchanged. Participating tabs get a compact block directly underneath them:

- The active role shows `RUNNING` in red and a smaller second line such as `From: User` or `From: A`.
- Every other role configured for the same `main.py --role` flow shows `WAITING`.
- A role that just handed work off shows `WAITING` and a smaller second line such as `Routed: B`.
- A configured role that has not been reached yet shows only `WAITING`; the UI never predicts a future route.
- A role outside the flow shows the existing panel with no flow block.
- When a flow finishes or stops with an error, its flow block is removed and the existing panel remains.

For `--role A,B,C` starting at A, turn 1 is:

```text
A  RUNNING   From: User
B  WAITING
C  WAITING
```

After A routes to B:

```text
A  WAITING   Routed: B
B  RUNNING   From: A
C  WAITING
```

## Minimal architecture

The backend stores a small, in-memory status record per physical browser role. Each record contains a run identifier, `RUNNING` or `WAITING`, and optional `From` or `Routed` detail. Browser polling already uses `/api/status`, so that response will include only the status record for the polling role.

Python launchers publish state through a small bridge API:

- `main.py` registers only its configured physical roles, marks the starting role `RUNNING / From: User`, and updates the affected source and target roles after each real route.
- `role.py` marks only its target role `RUNNING` while that request is active.
- Other triggers may use the same bridge methods; no separate queue or tab-to-tab protocol is introduced.

Cleanup is scoped by both role and run identifier. A run may remove only records it created. Starting, transitioning, or cleaning a `TEST1,TEST2` flow must not modify records or browser commands for `DEV`, `PLAN`, `REVIEW`, or any other role.

Logical routing labels remain human-readable (`From: A`, `Routed: B`), while storage and browser delivery are keyed by the mapped physical role so the correct tab renders the state.

## State transitions

At flow start, `main.py` writes one bounded snapshot for its configured roles: the start role is running and all other roles are waiting. A sequential route changes only the source and target records. A parallel route may mark multiple actual targets running, but it still does not mark an unreached role as anything other than waiting.

Updates replace the status record for the same role and run. Cleanup uses compare-and-clear semantics: if another run has since replaced a role record, an older run cannot erase it.

## UI layout

The status block uses two tight lines below `Role: ...`. The state line is small and bold; `RUNNING` is red and `WAITING` is amber. The optional detail line is smaller, muted, and has minimal top margin. No `QUEUED` label is used.

## Failure handling

Flow-status publication is diagnostic UI, not transport authority. A transient failure to publish it must not paste, send, cancel, reload, or otherwise alter a browser command. The launcher logs the publication failure and continues its existing orchestration behavior. Cleanup runs from `finally` paths for success, timeout, loader errors, invalid routes, and runtime errors.

Backend status records are in memory and disappear on backend restart. The userscript treats a missing or invalid record as no flow state and renders the existing panel.

## Verification

Automated tests cover:

- backend isolation, per-role delivery, replacement, and run-scoped cleanup;
- initial `RUNNING / From: User` plus waiting membership;
- sequential and repeated A/B routing without predicting C;
- physical-role mapping and non-participant isolation;
- cleanup on normal and error exits;
- compact userscript rendering and missing-state fallback;
- `role.py` setting and clearing only its requested role.

Live verification is limited to `TEST1` and `TEST2`. It must not dispatch commands or flow-state mutations to currently running `DEV`, `PLAN`, or `REVIEW` tabs.
