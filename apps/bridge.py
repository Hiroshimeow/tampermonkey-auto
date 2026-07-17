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
    choice_prompt_pending: bool
    choice_prompt_labels: tuple[str, ...]
    send_enabled: bool | None
    user_count: int
    assistant_count: int
    image_count: int
    last_user_text: str

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
    def blocked_by_choice_prompt(self) -> bool:
        return not self.composer_exists and self.choice_prompt_pending

    @property
    def done(self) -> bool:
        return bool(self.response) and self.composer_exists and not self.stop_visible and not self.manual_input_pending


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
        snapshot = self.role_snapshot(browser_role)
        self.ensure_role_online(browser_role, snapshot)
        baseline = self.response_activity(snapshot)
        self.set_prompt(browser_role, prompt, timeout_s)
        return self.send_current_prompt_and_wait(browser_role, timeout_s, prompt=prompt, baseline=baseline)

    @staticmethod
    def ensure_role_online(browser_role: str, snapshot: dict[str, Any]) -> None:
        status = str(snapshot.get("status") or "").upper()
        if snapshot.get("online") is False or status == "OFFLINE":
            age = snapshot.get("last_seen_age_s")
            age_text = "never seen" if age is None else f"last heartbeat {float(age):.1f}s ago"
            raise RuntimeError(f"physical role {browser_role} is offline ({age_text})")

    @staticmethod
    def normalize_composer_text(text: str) -> str:
        value = str(text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
        value = re.sub(r"\n{2,}", "\n\n", value)
        if value.endswith("\n"):
            value = value[:-1]
        return value

    @classmethod
    def composer_matches_prompt(cls, activity: ResponseActivity, prompt: str) -> bool:
        return cls.normalize_composer_text(activity.composer_text) == cls.normalize_composer_text(prompt)

    def set_prompt(self, browser_role: str, prompt: str, timeout_s: float, *, force_replace: bool = False) -> dict[str, Any]:
        del force_replace
        deadline = time.time() + max(1.0, timeout_s)
        activity = self.response_activity(self.role_snapshot(browser_role))
        while activity.active and time.time() < deadline:
            self.wait_for_current_response(browser_role, max(1.0, deadline - time.time()))
            activity = self.response_activity(self.role_snapshot(browser_role))

        if activity.composer_attachment_count > 0:
            raise ManualInputPendingError(
                f"{browser_role} composer has a manual attachment; automated prompt will not overwrite it",
            )
        if activity.composer_text and not self.composer_matches_prompt(activity, prompt):
            raise ManualInputPendingError(
                f"{browser_role} composer text differs from the expected automation prompt; ownership was not acquired",
            )

        result: dict[str, Any] = {"done": True, "status": "PASTE_REUSED"}
        if not self.composer_matches_prompt(activity, prompt):
            result = self._run_command(
                browser_role,
                "SET_PROMPT",
                {"text": prompt, "method": "auto", "expected_text": prompt},
                max(1.0, deadline - time.time()),
                "PASTE_CONFIRMED",
            )

        self.wait_for_stable_expected_prompt(
            browser_role,
            prompt,
            max(1.0, deadline - time.time()),
        )
        return result

    def wait_for_stable_expected_prompt(
        self,
        browser_role: str,
        prompt: str,
        timeout_s: float,
        poll_s: float = 0.1,
        stable_samples: int = 2,
    ) -> ResponseActivity:
        deadline = time.time() + max(1.0, timeout_s)
        stable = 0
        last_normalized: str | None = None
        last_activity: ResponseActivity | None = None
        while time.time() < deadline:
            activity = self.response_activity(self.role_snapshot(browser_role))
            last_activity = activity
            normalized = self.normalize_composer_text(activity.composer_text)
            expected = self.normalize_composer_text(prompt)
            if normalized != expected:
                if normalized:
                    raise ManualInputPendingError(
                        f"{browser_role} composer changed after paste; expected prompt ownership was lost",
                    )
                stable = 0
            elif activity.composer_attachment_count > 0:
                # ChatGPT can briefly expose an "uploading" attachment while it
                # processes an automation-owned prompt. Preserve ownership and
                # wait for the same prompt to become send-ready.
                stable = 0
            elif activity.composer_exists and activity.send_enabled is True:
                stable = stable + 1 if normalized == last_normalized else 1
                if stable >= max(1, stable_samples):
                    return activity
            else:
                stable = 0
            last_normalized = normalized
            self.sleep(max(0.01, poll_s))
        if last_activity and not self.composer_matches_prompt(last_activity, prompt):
            raise ManualInputPendingError(
                f"{browser_role} composer no longer contains the expected automation prompt",
            )
        if last_activity and last_activity.composer_attachment_count > 0:
            raise RuntimeError(f"{browser_role} automation prompt upload did not settle before timeout")
        raise RuntimeError(f"{browser_role} composer did not become stable and send-ready before timeout")

    def upload_files(self, browser_role: str, payload: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        self.wait_until_clean_ready(browser_role, timeout_s)
        return self._run_command(browser_role, "UPLOAD_FILES", payload, timeout_s, "UPLOAD_FILES_DONE")

    @classmethod
    def has_send_evidence(cls, activity: ResponseActivity, baseline: ResponseActivity) -> bool:
        return bool(
            activity.user_count > baseline.user_count
            or activity.assistant_count > baseline.assistant_count
            or activity.last_user_text != baseline.last_user_text
            or activity.response != baseline.response
        )

    @staticmethod
    def is_fresh_response(activity: ResponseActivity, baseline: ResponseActivity | None) -> bool:
        if baseline is None:
            return bool(activity.response)
        return bool(
            activity.assistant_count > baseline.assistant_count
            or (activity.response and activity.response != baseline.response)
        )

    def send_current_prompt_and_wait(
        self,
        browser_role: str,
        timeout_s: float,
        *,
        prompt: str | None = None,
        baseline: ResponseActivity | None = None,
    ) -> str:
        before_send = self.response_activity(self.role_snapshot(browser_role))
        response_baseline = baseline or before_send
        payload = {"expected_text": prompt or ""}
        try:
            self._run_command(browser_role, "CLICK_SEND", payload, timeout_s, "SEND_ACCEPTED")
        except RuntimeError as exc:
            activity = self.response_activity(self.role_snapshot(browser_role), previous_response=response_baseline.response)
            if prompt and self.has_send_evidence(activity, response_baseline):
                print(
                    f"[send-recover] role={browser_role} {exc}; send evidence detected, waiting for fresh response",
                    flush=True,
                )
                return self.wait_assistant_done(browser_role, timeout_s, baseline=response_baseline)
            if prompt and self.composer_matches_prompt(activity, prompt):
                print(
                    f"[send-recover] role={browser_role} {exc}; expected prompt still present, retrying click once in place",
                    flush=True,
                )
                self.wait_for_stable_expected_prompt(
                    browser_role,
                    prompt,
                    timeout_s,
                    stable_samples=1,
                )
                try:
                    self._run_command(browser_role, "CLICK_SEND", payload, timeout_s, "SEND_ACCEPTED")
                except RuntimeError as retry_exc:
                    final_activity = self.response_activity(
                        self.role_snapshot(browser_role),
                        previous_response=response_baseline.response,
                    )
                    if self.has_send_evidence(final_activity, response_baseline):
                        print(
                            f"[send-recover] role={browser_role} {retry_exc}; final send evidence detected after retry, waiting for fresh response",
                            flush=True,
                        )
                        return self.wait_assistant_done(browser_role, timeout_s, baseline=response_baseline)
                    raise
            elif prompt:
                raise ManualInputPendingError(
                    f"{browser_role} composer changed after send failure; automated prompt ownership was lost",
                ) from exc
            else:
                raise
        return self.wait_assistant_done(browser_role, timeout_s, baseline=response_baseline)

    def wait_assistant_done(
        self,
        browser_role: str,
        timeout_s: float,
        *,
        baseline: ResponseActivity | None = None,
    ) -> str:
        final = self.run_command(
            browser_role,
            "WAIT_ASSISTANT_DONE",
            {"timeout_ms": max(1_000, int(timeout_s * 1_000) - 1_000)},
            timeout_s,
        )
        status = str(final.get("status") or "")
        result = final.get("result") or {}
        if status != "ASSISTANT_DONE":
            if status == "ASSISTANT_TIMEOUT" or not final.get("done"):
                return self.recover_response_after_reload(
                    browser_role,
                    timeout_s,
                    require_response=True,
                    baseline=baseline,
                    require_fresh=baseline is not None,
                )
            if status == "ERROR_COMMAND":
                reason = str(result.get("reason") or "unknown")
                print(
                    f"[response-watch] role={browser_role} WAIT_ASSISTANT_DONE returned ERROR_COMMAND "
                    f"reason={reason}; recovering current response",
                    flush=True,
                )
                return self.wait_for_current_response(
                    browser_role,
                    timeout_s,
                    require_response=True,
                    baseline=baseline,
                    require_fresh=baseline is not None,
                )
            raise RuntimeError(f"{browser_role} WAIT_ASSISTANT_DONE failed: expected ASSISTANT_DONE, got {status or 'timeout'}")
        response = str(result.get("text") or "").strip()
        if self.looks_incomplete_response(response) or (baseline is not None and response == baseline.response):
            print(
                f"[response-watch] role={browser_role} assistant_done text is incomplete or stale; syncing transcript again",
                flush=True,
            )
            return self.wait_for_current_response(
                browser_role,
                timeout_s,
                require_response=True,
                baseline=baseline,
                require_fresh=baseline is not None,
            )
        return response

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

    def update_flow_statuses(
        self,
        run_id: str,
        updates: dict[str, dict[str, Any] | None],
    ) -> dict[str, Any]:
        return self.json_request(
            "POST",
            "/api/admin/flow-status",
            {"run_id": run_id, "updates": updates},
        )

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

    @staticmethod
    def _is_real_composer_attachment(meta: Any) -> bool:
        if not isinstance(meta, dict):
            return False
        label = " ".join(
            str(meta.get(key) or "")
            for key in ("label", "aria_label", "data_testid")
        ).lower()
        if not label:
            return False
        if "composer-plus-btn" in label or "add files and more" in label:
            return False
        return any(
            marker in label
            for marker in (
                "remove file",
                "open image",
                "attached",
                "file uploaded",
                "uploading",
                "remove attachment",
            )
        )

    @classmethod
    def _composer_attachment_count(cls, attachments: Any) -> int:
        if not isinstance(attachments, list):
            return 0
        return sum(1 for meta in attachments if cls._is_real_composer_attachment(meta))

    @classmethod
    def response_activity(cls, snapshot: dict[str, Any], previous_response: str = "") -> ResponseActivity:
        dom_info = snapshot.get("dom_info") or {}
        messages = dom_info.get("messages") or {}
        counts = messages.get("counts") or {}
        last_user = messages.get("last_user") or {}
        last_user_text = str(last_user.get("text") or "") if isinstance(last_user, dict) else ""
        response = str(snapshot.get("last_response") or "").strip()
        composer_text = str(dom_info.get("composer_text") or "")
        composer_text_len = cls._int_value(dom_info.get("composer_text_len"), len(composer_text))
        attachments = dom_info.get("composer_attachments") or []
        choice_candidates = dom_info.get("choice_prompt_candidates") or []
        choice_labels = tuple(
            label
            for label in (
                str(
                    (item.get("label") if isinstance(item, dict) else "")
                    or ((item.get("meta") or {}).get("label") if isinstance(item, dict) and isinstance(item.get("meta"), dict) else "")
                    or ((item.get("meta") or {}).get("aria_label") if isinstance(item, dict) and isinstance(item.get("meta"), dict) else "")
                    or ""
                ).strip()
                for item in choice_candidates
            )
            if label
        )
        return ResponseActivity(
            response=response,
            stop_visible=bool(dom_info.get("stop_visible")),
            has_response=bool(response),
            changed=bool(response and response != previous_response),
            composer_exists=bool(dom_info.get("composer")),
            composer_text_len=composer_text_len,
            composer_text=composer_text,
            composer_attachment_count=cls._composer_attachment_count(attachments),
            choice_prompt_pending=bool(dom_info.get("choice_prompt_pending")) or bool(choice_labels),
            choice_prompt_labels=choice_labels,
            send_enabled=dom_info.get("send_enabled") if isinstance(dom_info.get("send_enabled"), bool) else None,
            user_count=cls._int_value(counts.get("user")),
            assistant_count=cls._int_value(counts.get("assistant")),
            image_count=cls._int_value(counts.get("images")),
            last_user_text=last_user_text,
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

    @staticmethod
    def looks_incomplete_response(text: str) -> bool:
        value = str(text or "").strip()
        if not value:
            return True
        if value.count("```") % 2 == 1:
            return True
        without_language_label = re.sub(r"(?is)^json\s*", "", value).strip()
        if not without_language_label:
            return True
        if re.match(r"(?is)^(?:json\s*)?\{\s*$", value):
            return True
        if without_language_label.startswith("{") or re.search(r"(?is)```json", value):
            depth = 0
            in_string = False
            escape = False
            for char in without_language_label:
                if in_string:
                    if escape:
                        escape = False
                    elif char == "\\":
                        escape = True
                    elif char == '"':
                        in_string = False
                    continue
                if char == '"':
                    in_string = True
                elif char == "{":
                    depth += 1
                elif char == "}" and depth:
                    depth -= 1
            if depth > 0:
                return True
        return False

    def wait_for_current_response(
        self,
        role: str,
        timeout_s: float,
        active_wait_s: float = DEFAULT_RESPONSE_ACTIVE_WAIT_BEFORE_RELOAD_S,
        page_wait_s: float = DEFAULT_RESPONSE_RECOVERY_PAGE_WAIT_S,
        poll_s: float = DEFAULT_RESPONSE_RECOVERY_POLL_S,
        require_response: bool = False,
        baseline: ResponseActivity | None = None,
        require_fresh: bool = False,
    ) -> str:
        deadline = time.time() + max(1.0, timeout_s)
        cycle_started = time.time()
        last_response = ""
        last_logged_bucket = -1
        last_activity: ResponseActivity | None = None
        active_reload_used = False

        while time.time() < deadline:
            try:
                self.command_roundtrip(role, "SYNC_TRANSCRIPT", timeout_s=20.0)
                activity = self.response_activity(self.role_snapshot(role), previous_response=last_response)
            except Exception as exc:
                print(f"[response-watch] role={role} transient snapshot/sync failure: {exc}; waiting", flush=True)
                self.sleep(max(0.1, poll_s))
                continue
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

            if activity.blocked_by_choice_prompt:
                labels = ", ".join(activity.choice_prompt_labels[:5]) or "unknown"
                print(
                    f"[response-watch] role={role} choice prompt pending labels={labels}; clicking safe choice",
                    flush=True,
                )
                result = self.command_roundtrip(role, "CLICK_CHOICE_PROMPT", timeout_s=20.0)
                status = str(result.get("status") or "")
                if status != "CHOICE_PROMPT_CLICKED":
                    raise RuntimeError(
                        f"{role} choice prompt blocked response recovery and could not be resolved: "
                        f"status={status or 'timeout'} labels={labels}",
                    )
                self.sleep(max(0.5, poll_s))
                last_logged_bucket = -1
                continue

            if activity.has_response and self.looks_incomplete_response(activity.response):
                print(
                    f"[response-watch] role={role} response looks incomplete len={activity.response_len}; waiting",
                    flush=True,
                )
                self.sleep(max(0.1, poll_s))
                continue

            if self.is_response_done(activity):
                if not require_fresh or self.is_fresh_response(activity, baseline):
                    return activity.response
                self.sleep(max(0.1, poll_s))
                continue
            if not self.is_response_active(activity):
                if activity.has_response and not activity.composer_exists:
                    print(
                        f"[response-watch] role={role} response present but composer not ready after reload; waiting",
                        flush=True,
                    )
                    self.sleep(max(0.1, poll_s))
                    continue
                if activity.has_response:
                    if not require_fresh or self.is_fresh_response(activity, baseline):
                        return activity.response
                    self.sleep(max(0.1, poll_s))
                    continue
                if require_response:
                    self.sleep(max(0.1, poll_s))
                    continue
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

            if self.is_response_stuck(activity, elapsed_in_cycle, active_wait_s) and not active_reload_used:
                print(
                    f"[response-watch] role={role} still active after {active_wait_s:.1f}s; reloading page once",
                    flush=True,
                )
                active_reload_used = True
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
        if last_response and not self.looks_incomplete_response(last_response):
            if not require_fresh or (last_activity is not None and self.is_fresh_response(last_activity, baseline)):
                return last_response
            raise RuntimeError(f"{role} response wait timed out without a fresh assistant response")
        if last_response:
            raise RuntimeError(f"{role} response wait timed out with incomplete response; last_response_len={len(last_response)}")
        if require_response:
            raise RuntimeError(f"{role} response wait timed out while waiting for recovered response")
        raise RuntimeError(f"{role} response wait timed out; last_response_len={len(last_response)}")

    def wait_until_clean_ready(
        self,
        role: str,
        timeout_s: float,
        poll_s: float = DEFAULT_RESPONSE_RECOVERY_POLL_S,
    ) -> ResponseActivity:
        deadline = time.time() + max(1.0, timeout_s)
        last_activity: ResponseActivity | None = None
        last_logged_bucket = -1
        while time.time() < deadline:
            try:
                activity = self.response_activity(self.role_snapshot(role))
            except Exception as exc:
                print(f"[ready-check] role={role} transient snapshot failure: {exc}; waiting", flush=True)
                self.sleep(max(0.1, poll_s))
                continue
            last_activity = activity
            if self.is_clean_ready(activity):
                return activity
            if activity.blocked_by_choice_prompt:
                labels = ", ".join(activity.choice_prompt_labels[:5]) or "unknown"
                print(
                    f"[ready-check] role={role} choice prompt pending labels={labels}; clicking safe choice",
                    flush=True,
                )
                result = self.command_roundtrip(role, "CLICK_CHOICE_PROMPT", timeout_s=20.0)
                status = str(result.get("status") or "")
                if status != "CHOICE_PROMPT_CLICKED":
                    raise RuntimeError(
                        f"{role} choice prompt blocked composer and could not be resolved: "
                        f"status={status or 'timeout'} labels={labels}",
                    )
                self.sleep(max(0.5, poll_s))
                last_logged_bucket = -1
                continue
            remaining_s = max(0.0, deadline - time.time())
            bucket = int(remaining_s // max(1.0, poll_s * 5))
            if bucket != last_logged_bucket:
                if self.is_manual_input_pending(activity):
                    print(
                        f"[ready-check] role={role} manual_input_pending=true "
                        f"composer_len={activity.composer_text_len} attachments={activity.composer_attachment_count}; "
                        "waiting for the user to clear or send it",
                        flush=True,
                    )
                elif not activity.composer_exists:
                    labels = ", ".join(activity.choice_prompt_labels[:5])
                    suffix = f" choice_labels={labels}" if labels else ""
                    print(f"[ready-check] role={role} composer not found; waiting{suffix}", flush=True)
                elif self.is_response_active(activity):
                    print(f"[ready-check] role={role} response active; waiting", flush=True)
                else:
                    print(f"[ready-check] role={role} not clean-ready; waiting", flush=True)
                last_logged_bucket = bucket
            if self.is_response_active(activity):
                self.wait_for_current_response(role, timeout_s=max(1.0, deadline - time.time()))
                continue
            self.sleep(max(0.1, poll_s))
        if last_activity and self.is_manual_input_pending(last_activity):
            raise ManualInputPendingError(
                f"{role} composer still has manual input after waiting; not replacing it with an automated prompt",
            )
        raise RuntimeError(f"{role} did not become clean-ready before timeout")

    def recover_response_after_reload(
        self,
        role: str,
        timeout_s: float,
        reload_delay_s: float = DEFAULT_RESPONSE_RECOVERY_RELOAD_DELAY_S,
        page_wait_s: float = DEFAULT_RESPONSE_RECOVERY_PAGE_WAIT_S,
        poll_s: float = DEFAULT_RESPONSE_RECOVERY_POLL_S,
        require_response: bool = False,
        baseline: ResponseActivity | None = None,
        require_fresh: bool = False,
    ) -> str:
        return self.wait_for_current_response(
            role,
            timeout_s=timeout_s,
            active_wait_s=reload_delay_s,
            page_wait_s=page_wait_s,
            poll_s=poll_s,
            require_response=require_response,
            baseline=baseline,
            require_fresh=require_fresh,
        )

    @staticmethod
    def _snapshot_page_generation(snapshot: dict[str, Any]) -> str:
        dom_info = snapshot.get("dom_info") or {}
        return str(dom_info.get("page_instance_id") or "")

    @staticmethod
    def _snapshot_page_path(snapshot: dict[str, Any]) -> str:
        dom_info = snapshot.get("dom_info") or {}
        return str(dom_info.get("page_path") or "")

    @classmethod
    def is_clean_new_chat_snapshot(cls, snapshot: dict[str, Any], previous_generation: str) -> bool:
        dom_info = snapshot.get("dom_info") or {}
        messages = dom_info.get("messages") or {}
        counts = messages.get("counts") or {}
        generation = cls._snapshot_page_generation(snapshot)
        path = cls._snapshot_page_path(snapshot)
        activity = cls.response_activity(snapshot)
        return bool(
            generation
            and previous_generation
            and generation != previous_generation
            and path == "/"
            and activity.composer_exists
            and not activity.composer_text
            and activity.composer_attachment_count == 0
            and not activity.stop_visible
            and not activity.choice_prompt_pending
            and cls._int_value(counts.get("user")) == 0
            and cls._int_value(counts.get("assistant")) == 0
        )

    def wait_new_chat_ready(
        self,
        role: str,
        before_snapshot: dict[str, Any],
        timeout_s: float,
        poll_s: float = 0.5,
    ) -> dict[str, Any]:
        previous_generation = self._snapshot_page_generation(before_snapshot)
        if not previous_generation:
            raise RuntimeError(f"{role} reset readiness cannot be verified: missing pre-reset page_instance_id")
        deadline = time.time() + max(1.0, timeout_s)
        last_probe: dict[str, Any] = {}
        last_snapshot: dict[str, Any] = {}
        while time.time() < deadline:
            remaining = max(1.0, deadline - time.time())
            try:
                last_probe = self.command_roundtrip(role, "PROBE", timeout_s=min(20.0, remaining))
                if last_probe.get("done") and str(last_probe.get("status") or "") == "PROBE_DONE":
                    last_snapshot = self.role_snapshot(role)
                    if self.is_clean_new_chat_snapshot(last_snapshot, previous_generation):
                        return {
                            "done": True,
                            "status": "NEW_CHAT_READY",
                            "probe": last_probe,
                            "page_instance_id": self._snapshot_page_generation(last_snapshot),
                            "page_path": self._snapshot_page_path(last_snapshot),
                        }
            except RuntimeError:
                pass
            self.sleep(max(0.05, poll_s))
        raise RuntimeError(
            f"{role} new chat did not reach terminal clean readiness before timeout; "
            f"last_probe_status={last_probe.get('status') or 'none'} "
            f"last_generation={self._snapshot_page_generation(last_snapshot) or 'none'} "
            f"last_path={self._snapshot_page_path(last_snapshot) or 'none'}"
        )

    def new_chat(self, role: str, timeout_s: float = 25.0) -> dict[str, Any]:
        return self.command_roundtrip(role, "NEW_CHAT", timeout_s)
