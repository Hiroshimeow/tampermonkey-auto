from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from typing import Any

from apps.models import FlowState, FlowStopError, TurnResult
from apps.text import normalize_role


class BrowserLifecycleMixin:
    args: object
    background_tasks: list[threading.Thread]
    dry_run: bool
    lock: threading.Lock
    reload_generation: dict[str, int]

    def should_reload_previous_role_after_route(self, result: TurnResult, targets: dict[str, str]) -> bool:
        if self.args.reload_after <= 0:
            return False
        current_physical = normalize_role(result.browser_role)
        return any(self.pick_browser_role(role) != current_physical for role in targets)

    def schedule_previous_role_reload_after_route(self, result: TurnResult, targets: dict[str, str]) -> None:
        if not self.should_reload_previous_role_after_route(result, targets):
            return
        browser_role = result.browser_role
        delay_s = max(0.0, float(self.args.reload_after))
        token = self.mark_reload_scheduled(browser_role)
        if self.dry_run:
            print(f"[route-reload] dry-run browser_role={browser_role} delay={delay_s:.1f}s", flush=True)
            return
        thread = threading.Thread(
            target=self.reload_browser_after_delay,
            args=(browser_role, delay_s, token),
            name=f"reload-after-route-{browser_role}",
        )
        thread.start()
        self.background_tasks.append(thread)

    def mark_reload_scheduled(self, browser_role: str) -> int:
        browser_role = normalize_role(browser_role)
        with self.lock:
            token = self.reload_generation.get(browser_role, 0) + 1
            self.reload_generation[browser_role] = token
            return token

    def cancel_pending_reload(self, browser_role: str) -> None:
        browser_role = normalize_role(browser_role)
        with self.lock:
            self.reload_generation[browser_role] = self.reload_generation.get(browser_role, 0) + 1

    def is_reload_current(self, browser_role: str, token: int) -> bool:
        browser_role = normalize_role(browser_role)
        with self.lock:
            return self.reload_generation.get(browser_role, 0) == token

    def reload_browser_after_delay(self, browser_role: str, delay_s: float, token: int) -> None:
        time.sleep(delay_s)
        if not self.is_reload_current(browser_role, token):
            print(f"[route-reload] skipped browser_role={browser_role} reason=reused_before_delay", flush=True)
            return
        try:
            with self.sessions.locked(browser_role):
                if not self.is_reload_current(browser_role, token):
                    print(f"[route-reload] skipped browser_role={browser_role} reason=reused_before_lock", flush=True)
                    return
                result = self.client.command_roundtrip(browser_role, "RELOAD_PAGE", timeout_s=self.args.preflight_timeout)
            print(f"[route-reload] browser_role={browser_role} status={result.get('status')} done={result.get('done')}", flush=True)
        except RuntimeError as exc:
            print(f"[route-reload] failed browser_role={browser_role}: {exc}", flush=True)

    def wait_background_tasks(self) -> None:
        for thread in list(self.background_tasks):
            thread.join()

    def _perform_browser_reset(self, physical_role: str) -> dict[str, Any]:
        if self.dry_run:
            print(f"[new-chat] dry-run browser_role={physical_role}", flush=True)
            return {"done": True, "status": "DRY_RUN_NEW_CHAT"}
        before_snapshot = self.client.role_snapshot(physical_role)
        acknowledgement = self.client.new_chat(physical_role, timeout_s=self.args.preflight_timeout)
        acknowledgement_status = str(acknowledgement.get("status") or "")
        print(
            f"[new-chat] browser_role={physical_role} acknowledgement={acknowledgement_status or 'none'} "
            f"done={acknowledgement.get('done')}",
            flush=True,
        )
        if acknowledgement_status not in {"NEW_CHAT_NAVIGATING", "NEW_CHAT_DONE", "NAVIGATE_NEW_DONE"}:
            raise RuntimeError(
                f"{physical_role} NEW_CHAT did not acknowledge navigation: "
                f"status={acknowledgement_status or 'none'} done={acknowledgement.get('done')}"
            )
        ready = self.client.wait_new_chat_ready(
            physical_role,
            before_snapshot,
            timeout_s=self.args.preflight_timeout,
        )
        print(
            f"[new-chat] browser_role={physical_role} terminal_status={ready.get('status')} "
            f"generation={ready.get('page_instance_id')} path={ready.get('page_path')}",
            flush=True,
        )
        return {"done": True, "status": "NEW_CHAT_READY", "acknowledgement": acknowledgement, **ready}

    def _invalidate_physical_bootstrap(self, physical_role: str) -> None:
        session = self.sessions.get(physical_role)
        session.invalidate()
        logical_roles = self.runtime_config.logical_roles_for(physical_role)
        self.system_sent.difference_update(logical_roles)

    def reset_roles_for_handoff(self, roles: Iterable[str], state: FlowState) -> dict[str, Any]:
        logical_roles = [normalize_role(role) for role in roles if normalize_role(role)]
        physical_roles: list[str] = []
        for logical in logical_roles:
            physical = self.pick_browser_role(logical)
            if physical not in physical_roles:
                physical_roles.append(physical)
        if not physical_roles:
            return {"done": True, "succeeded": [], "failed": {}}

        succeeded: list[str] = []
        failed: dict[str, str] = {}
        with self.sessions.locked_many(physical_roles):
            for physical in physical_roles:
                try:
                    result = self._perform_browser_reset(physical)
                except RuntimeError as exc:
                    failed[physical] = str(exc)
                    continue
                if not result.get("done"):
                    failed[physical] = f"status={result.get('status') or 'unknown'} done=false"
                    continue
                succeeded.append(physical)

            if failed:
                raise FlowStopError(
                    "reset_failed",
                    "one or more browser resets failed; workflow stopped without advancing phase",
                    requested=physical_roles,
                    succeeded=succeeded,
                    failed=failed,
                )

            for physical in physical_roles:
                self._invalidate_physical_bootstrap(physical)
            state.advance_phase()

        return {"done": True, "succeeded": succeeded, "failed": {}}

    def reset_browser_for(self, prompt_role: str) -> dict[str, Any]:
        physical = self.pick_browser_role(prompt_role)
        with self.sessions.locked(physical):
            try:
                result = self._perform_browser_reset(physical)
            except RuntimeError as exc:
                raise FlowStopError(
                    "reset_failed",
                    f"browser reset failed for {physical}: {exc}",
                    requested=[physical],
                    succeeded=[],
                    failed={physical: str(exc)},
                ) from exc
            if not result.get("done"):
                reason = f"status={result.get('status') or 'unknown'} done=false"
                raise FlowStopError(
                    "reset_failed",
                    f"browser reset failed for {physical}: {reason}",
                    requested=[physical],
                    succeeded=[],
                    failed={physical: reason},
                )
            self._invalidate_physical_bootstrap(physical)
            return result

    def preflight(self) -> list[dict[str, Any]]:
        actions = ["PROBE"] if self.args.resume else ["PROBE", "RELOAD_PAGE", "NEW_CHAT"]
        completed: list[dict[str, Any]] = []
        for role in self.runtime_config.physical_roles:
            with self.sessions.locked(role):
                for action in actions:
                    try:
                        result = (
                            self._perform_browser_reset(role)
                            if action == "NEW_CHAT"
                            else self.client.command_roundtrip(role, action, timeout_s=self.args.preflight_timeout)
                        )
                    except RuntimeError as exc:
                        raise FlowStopError(
                            "preflight_failed",
                            f"preflight failed for {role} action={action}: {exc}",
                            role=role,
                            action=action,
                            completed=completed,
                        ) from exc
                    print(
                        f"[preflight] role={role} action={action} status={result.get('status')} done={result.get('done')}",
                        flush=True,
                    )
                    if not result.get("done"):
                        raise FlowStopError(
                            "preflight_failed",
                            f"preflight returned done=false for {role} action={action}",
                            role=role,
                            action=action,
                            result=result,
                            completed=completed,
                        )
                    completed.append({"role": role, "action": action, "status": result.get("status")})
                    if action == "RELOAD_PAGE":
                        time.sleep(2.0)
                if not self.args.resume and "NEW_CHAT" in actions:
                    self._invalidate_physical_bootstrap(role)
        return completed
