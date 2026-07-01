from __future__ import annotations

import json
import re
import time
import urllib.error
from dataclasses import dataclass
import urllib.parse
import urllib.request
from typing import Any

from apps.constants import (
    DEFAULT_RESPONSE_ACTIVE_WAIT_BEFORE_RELOAD_S,
    DEFAULT_RESPONSE_RECOVERY_PAGE_WAIT_S,
    DEFAULT_RESPONSE_RECOVERY_POLL_S,
    DEFAULT_RESPONSE_RECOVERY_RELOAD_DELAY_S,
)


class ManualInputPendingError(Exception):
    pass


@dataclass(frozen=True)
class ResponseActivity:
    response: str
    stop_visible: bool
    has_response: bool
    changed: bool
    composer_exists: bool
    composer_text_len: int
    composer_text: str
    composer_attachment_count: int
    send_enabled: bool | None
    user_count: int
    assistant_count: int
    image_count: int

    @property
    def response_len(self) -> int:
        return len(self.response)

    @property
    def active(self) -> bool:
        return self.stop_visible

    @property
    def streaming(self) -> bool:
        return self.active and self.changed

    @property
    def manual_input_pending(self) -> bool:
        return self.composer_text_len > 0 or bool(self.composer_text.strip()) or self.composer_attachment_count > 0

    @property
    def clean_ready(self) -> bool:
        return self.composer_exists and not self.active and not self.manual_input_pending

    @property
    def done(self) -> bool:
        return bool(self.response) and not self.stop_visible and not self.manual_input_pending


class BridgeClient:
    def __init__(self, base_url: str, request_timeout_s: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.request_timeout_s = request_timeout_s
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def json_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = {"Accept": "application/json"}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with self.opener.open(req, timeout=timeout_s or self.request_timeout_s) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:1500]
            raise RuntimeError(f"HTTP {exc.code} {exc.reason} {method} {path}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Cannot connect to {url}: {exc.reason}") from exc

    def call_browser_role(self, browser_role: str, prompt: str, timeout_s: float) -> str:
        self.wait_until_clean_ready(browser_role, timeout_s)
        self._run_command(browser_role, "SET_PROMPT", {"text": prompt, "method": "auto"}, timeout_s, "PASTE_CONFIRMED")
        self._run_command(browser_role, "CLICK_SEND", {}, timeout_s, "SEND_ACCEPTED")
        final = self.run_command(browser_role, "WAIT_ASSISTANT_DONE", {}, timeout_s)
        status = str(final.get("status") or "")
        if status != "ASSISTANT_DONE":
            if status == "ASSISTANT_TIMEOUT" or not final.get("done"):
                return self.recover_response_after_reload(browser_role, timeout_s)
            raise RuntimeError(f"{browser_role} WAIT_ASSISTANT_DONE failed: expected ASSISTANT_DONE, got {status or 'timeout'}")
        result = final.get("result") or {}
        return str(result.get("text") or "").strip()

    def _run_command(
        self,
        role: str,
        action: str,
        payload: dict[str, Any],
        timeout_s: float,
        expected_status: str,
    ) -> dict[str, Any]:
        result = self.run_command(role, action, payload, timeout_s)
        status = str(result.get("status") or "")
        if status != expected_status:
            raise RuntimeError(f"{role} {action} failed: expected {expected_status}, got {status or 'timeout'}")
        return result

    def run_command(self, role: str, action: str, payload: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        command_id = self.create_command(role, action, payload)
        if not command_id:
            raise RuntimeError(f"{role} {action} returned no command id")
        return self.wait_command(command_id, timeout_s)

    def create_command(self, role: str, action: str, payload: dict[str, Any] | None = None) -> str:
        data = self.json_request("POST", "/api/admin/command", {"role": role, "action": action, "payload": payload or {}})
        return str((data.get("command") or {}).get("command_id") or "")

    def wait_command(self, command_id: str, timeout_s: float) -> dict[str, Any]:
        deadline = time.time() + timeout_s
        last = {}
        while time.time() < deadline:
            last = self.json_request("GET", f"/api/admin/command/{urllib.parse.quote(command_id)}")
            status = str(last.get("status") or "")
            if last.get("done") or re.search(r"DONE|FAILED|ERROR|UNKNOWN|RELOADING|NAVIGATING|SAVED", status):
                return last
            time.sleep(0.5)
        return last

    def command_roundtrip(self, role: str, action: str, timeout_s: float = 20.0) -> dict[str, Any]:
        command_id = self.create_command(role, action, {"source": "main_preflight"})
        if not command_id:
            return {"ok": False, "status": "NO_COMMAND_ID", "done": False}
        result = self.wait_command(command_id, timeout_s)
        return {"ok": bool(result.get("done")), "command_id": command_id, **result}

    def role_snapshot(self, role: str) -> dict[str, Any]:
        return self.json_request("GET", f"/api/admin/role/{urllib.parse.quote(role)}")

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    @staticmethod
    def _int_value(value: Any, default: int = 0) -> int:
        try:
            return int(value or default)
        except (TypeError, ValueError):
            return default

    @classmethod
    def response_activity(cls, snapshot: dict[str, Any], previous_response: str = "") -> ResponseActivity:
        dom_info = snapshot.get("dom_info") or {}
        messages = dom_info.get("messages") or {}
        counts = messages.get("counts") or {}
        response = str(snapshot.get("last_response") or "").strip()
        composer_text = str(dom_info.get("composer_text") or "")
        composer_text_len = cls._int_value(dom_info.get("composer_text_len"), len(composer_text))
        attachments = dom_info.get("composer_attachments") or []
        return ResponseActivity(
            response=response,
            stop_visible=bool(dom_info.get("stop_visible")),
            has_response=bool(response),
            changed=bool(response and response != previous_response),
            composer_exists=bool(dom_info.get("composer")),
            composer_text_len=composer_text_len,
            composer_text=composer_text,
            composer_attachment_count=len(attachments) if isinstance(attachments, list) else 0,
            send_enabled=dom_info.get("send_enabled") if isinstance(dom_info.get("send_enabled"), bool) else None,
            user_count=cls._int_value(counts.get("user")),
            assistant_count=cls._int_value(counts.get("assistant")),
            image_count=cls._int_value(counts.get("images")),
        )

    @staticmethod
    def is_manual_input_pending(activity: ResponseActivity) -> bool:
        return activity.manual_input_pending

    @staticmethod
    def is_response_active(activity: ResponseActivity) -> bool:
        return activity.active

    @staticmethod
    def is_response_streaming(activity: ResponseActivity) -> bool:
        return activity.streaming

    @staticmethod
    def is_clean_ready(activity: ResponseActivity) -> bool:
        return activity.clean_ready

    @staticmethod
    def is_response_done(activity: ResponseActivity) -> bool:
        return activity.done

    @staticmethod
    def is_response_stuck(activity: ResponseActivity, elapsed_s: float, active_wait_s: float) -> bool:
        return activity.active and elapsed_s >= max(0.0, active_wait_s)

    def wait_for_current_response(
        self,
        role: str,
        timeout_s: float,
        active_wait_s: float = DEFAULT_RESPONSE_ACTIVE_WAIT_BEFORE_RELOAD_S,
        page_wait_s: float = DEFAULT_RESPONSE_RECOVERY_PAGE_WAIT_S,
        poll_s: float = DEFAULT_RESPONSE_RECOVERY_POLL_S,
    ) -> str:
        deadline = time.time() + max(1.0, timeout_s)
        cycle_started = time.time()
        last_response = ""
        last_logged_bucket = -1
        last_activity: ResponseActivity | None = None

        while time.time() < deadline:
            self.command_roundtrip(role, "SYNC_TRANSCRIPT", timeout_s=20.0)
            activity = self.response_activity(self.role_snapshot(role), previous_response=last_response)
            last_activity = activity
            last_response = activity.response or last_response

            if self.is_manual_input_pending(activity):
                print(
                    f"[response-watch] role={role} manual_input_pending=true "
                    f"composer_len={activity.composer_text_len} attachments={activity.composer_attachment_count}; "
                    "waiting without send/reload",
                    flush=True,
                )
                self.sleep(max(0.1, poll_s))
                continue

            if self.is_response_done(activity):
                return activity.response
            if not self.is_response_active(activity):
                if activity.has_response:
                    return activity.response
                return ""

            elapsed_in_cycle = time.time() - cycle_started
            bucket = int(elapsed_in_cycle // max(1.0, poll_s * 5))
            if bucket != last_logged_bucket:
                state = "streaming" if self.is_response_streaming(activity) else "active"
                print(
                    f"[response-watch] role={role} state={state} stop_visible=true "
                    f"response_len={len(last_response)} elapsed={elapsed_in_cycle:.1f}s/{active_wait_s:.1f}s",
                    flush=True,
                )
                last_logged_bucket = bucket

            if self.is_response_stuck(activity, elapsed_in_cycle, active_wait_s):
                print(
                    f"[response-watch] role={role} still active after {active_wait_s:.1f}s; reloading page",
                    flush=True,
                )
                self.command_roundtrip(role, "RELOAD_PAGE", timeout_s=20.0)
                self.sleep(max(0.0, page_wait_s))
                cycle_started = time.time()
                last_logged_bucket = -1
                continue

            self.sleep(max(0.1, poll_s))

        if last_activity and self.is_manual_input_pending(last_activity):
            raise ManualInputPendingError(
                f"{role} composer still has manual input after waiting; not sending automated prompt",
            )
        if last_activity and self.is_response_active(last_activity):
            raise RuntimeError(f"{role} response still active after timeout; last_response_len={len(last_response)}")
        if last_response:
            return last_response
        raise RuntimeError(f"{role} response wait timed out; last_response_len={len(last_response)}")

    def wait_until_clean_ready(
        self,
        role: str,
        timeout_s: float,
        poll_s: float = DEFAULT_RESPONSE_RECOVERY_POLL_S,
    ) -> ResponseActivity:
        deadline = time.time() + max(1.0, timeout_s)
        while time.time() < deadline:
            activity = self.response_activity(self.role_snapshot(role))
            if self.is_manual_input_pending(activity):
                raise ManualInputPendingError(
                    f"{role} composer has manual input; not replacing it with an automated prompt",
                )
            if self.is_clean_ready(activity):
                return activity
            if self.is_response_active(activity):
                self.wait_for_current_response(role, timeout_s=max(1.0, deadline - time.time()))
                continue
            if not activity.composer_exists:
                print(f"[ready-check] role={role} composer not found; waiting", flush=True)
            self.sleep(max(0.1, poll_s))
        raise RuntimeError(f"{role} did not become clean-ready before timeout")

    def recover_response_after_reload(
        self,
        role: str,
        timeout_s: float,
        reload_delay_s: float = DEFAULT_RESPONSE_RECOVERY_RELOAD_DELAY_S,
        page_wait_s: float = DEFAULT_RESPONSE_RECOVERY_PAGE_WAIT_S,
        poll_s: float = DEFAULT_RESPONSE_RECOVERY_POLL_S,
    ) -> str:
        return self.wait_for_current_response(
            role,
            timeout_s=timeout_s,
            active_wait_s=reload_delay_s,
            page_wait_s=page_wait_s,
            poll_s=poll_s,
        )

    def new_chat(self, role: str, timeout_s: float = 25.0) -> dict[str, Any]:
        return self.command_roundtrip(role, "NEW_CHAT", timeout_s)
