from __future__ import annotations

import threading
import time
from collections.abc import Iterable

from apps.models import FlowState, TurnResult
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
        current_role = normalize_role(result.prompt_role)
        return any(normalize_role(role) != current_role for role in targets)

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
            result = self.client.command_roundtrip(browser_role, "RELOAD_PAGE", timeout_s=self.args.preflight_timeout)
            print(f"[route-reload] browser_role={browser_role} status={result.get('status')} done={result.get('done')}", flush=True)
        except RuntimeError as exc:
            print(f"[route-reload] failed browser_role={browser_role}: {exc}", flush=True)

    def wait_background_tasks(self) -> None:
        for thread in self.background_tasks:
            thread.join()

    def reset_roles_for_handoff(self, roles: Iterable[str], state: FlowState) -> None:
        did_reset = False
        for role in roles:
            self.reset_browser_for(role)
            did_reset = True
        if did_reset:
            state.phase += 1

    def reset_browser_for(self, prompt_role: str) -> None:
        self.system_sent.discard(normalize_role(prompt_role))
        if self.dry_run:
            print(f"[new-chat] dry-run reset for {prompt_role}", flush=True)
            return
        browser_role = self.pick_browser_role(prompt_role)
        try:
            result = self.client.new_chat(browser_role)
            print(
                f"[new-chat] prompt_role={prompt_role} browser_role={browser_role} status={result.get('status')} done={result.get('done')}",
                flush=True,
            )
        except RuntimeError as exc:
            print(f"[new-chat] failed prompt_role={prompt_role}: {exc}", flush=True)

    def preflight(self) -> None:
        for role in self.browser_roles:
            for action in ["PROBE", "RELOAD_PAGE", "NEW_CHAT"]:
                result = self.client.command_roundtrip(role, action, timeout_s=self.args.preflight_timeout)
                print(
                    f"[preflight] role={role} action={action} status={result.get('status')} done={result.get('done')}",
                    flush=True,
                )
                if not result.get("done"):
                    print(
                        f"[preflight] WARN role={role} did not complete {action}; avoid trusting this browser role",
                        flush=True,
                    )
                if action in {"RELOAD_PAGE", "NEW_CHAT"}:
                    time.sleep(2.0)
