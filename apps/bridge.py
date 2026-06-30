from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from apps.constants import (
    DEFAULT_RESPONSE_RECOVERY_PAGE_WAIT_S,
    DEFAULT_RESPONSE_RECOVERY_POLL_S,
    DEFAULT_RESPONSE_RECOVERY_RELOAD_DELAY_S,
)


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

    def recover_response_after_reload(
        self,
        role: str,
        timeout_s: float,
        reload_delay_s: float = DEFAULT_RESPONSE_RECOVERY_RELOAD_DELAY_S,
        page_wait_s: float = DEFAULT_RESPONSE_RECOVERY_PAGE_WAIT_S,
        poll_s: float = DEFAULT_RESPONSE_RECOVERY_POLL_S,
    ) -> str:
        print(f"[response-recovery] role={role} wait={reload_delay_s:.1f}s before deciding reload", flush=True)
        self.sleep(max(0.0, reload_delay_s))
        self.command_roundtrip(role, "SYNC_TRANSCRIPT", timeout_s=20.0)
        pre_reload_snapshot = self.role_snapshot(role)
        pre_reload_dom = pre_reload_snapshot.get("dom_info") or {}
        pre_reload_response = str(pre_reload_snapshot.get("last_response") or "").strip()
        if not pre_reload_dom.get("stop_visible"):
            return pre_reload_response
        print(f"[response-recovery] role={role} still responding after initial wait; reloading", flush=True)
        self.command_roundtrip(role, "RELOAD_PAGE", timeout_s=20.0)
        self.sleep(max(0.0, page_wait_s))

        deadline = time.time() + max(1.0, timeout_s)
        last_response = ""
        stable_response = ""
        stable_count = 0
        while time.time() < deadline:
            self.command_roundtrip(role, "SYNC_TRANSCRIPT", timeout_s=20.0)
            snapshot = self.role_snapshot(role)
            dom_info = snapshot.get("dom_info") or {}
            last_response = str(snapshot.get("last_response") or "").strip()
            if not dom_info.get("stop_visible") and last_response:
                return last_response
            if dom_info.get("stop_visible"):
                print(f"[response-recovery] role={role} still responding; waiting", flush=True)
                if last_response and last_response == stable_response:
                    stable_count += 1
                else:
                    stable_response = last_response
                    stable_count = 1 if last_response else 0
                if stable_count >= 2 and stable_response:
                    print(f"[response-recovery] role={role} accepting stable response while stop is still visible", flush=True)
                    return stable_response
            self.sleep(max(0.1, poll_s))

        raise RuntimeError(f"{role} response recovery timed out; last_response_len={len(last_response)}")

    def new_chat(self, role: str, timeout_s: float = 25.0) -> dict[str, Any]:
        return self.command_roundtrip(role, "NEW_CHAT", timeout_s)
