from __future__ import annotations

import json
import math
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


EXPIRABLE_COMMANDS = {
    "PROBE",
    "SYNC_TRANSCRIPT",
    "CLICK_CHOICE_PROMPT",
    "NEW_CHAT",
    "NAVIGATE_NEW",
    "RESET_PAGE",
    "RELOAD_PAGE",
    "RELOAD",
    "HARD_RELOAD",
}


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
    page_instance_id: str
    observation_seq: int

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

    @staticmethod
    def _operation_deadline(timeout_s: float, deadline: float | None = None) -> float:
        if deadline is not None:
            return float(deadline)
        return time.monotonic() + max(0.1, float(timeout_s))

    @staticmethod
    def _remaining_time(deadline: float, *, context: str = "operation") -> float:
        remaining = float(deadline) - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(f"{context} exhausted remaining deadline budget")
        return remaining

    @classmethod
    def _bounded_timeout(
        cls,
        deadline: float,
        limit_s: float | None = None,
        *,
        context: str = "operation",
    ) -> float:
        remaining = cls._remaining_time(deadline, context=context)
        return remaining if limit_s is None else min(max(0.0, float(limit_s)), remaining)

    def _sleep_bounded(self, seconds: float, deadline: float, *, context: str = "operation") -> None:
        remaining = self._remaining_time(deadline, context=context)
        self.sleep(min(max(0.0, float(seconds)), remaining))

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
        deadline = self._operation_deadline(timeout_s)
        snapshot = self.role_snapshot(
            browser_role,
            timeout_s=self._bounded_timeout(deadline, context=f"{browser_role} preflight snapshot"),
        )
        self.ensure_role_online(browser_role, snapshot)
        baseline = self.response_activity(snapshot)
        self.set_prompt(browser_role, prompt, timeout_s, _deadline=deadline)
        return self.send_current_prompt_and_wait(
            browser_role,
            timeout_s,
            prompt=prompt,
            baseline=baseline,
            _deadline=deadline,
        )

    @classmethod
    def ensure_role_online(cls, browser_role: str, snapshot: dict[str, Any]) -> None:
        snapshot = cls._mapping(snapshot, "snapshot")
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

    def set_prompt(
        self,
        browser_role: str,
        prompt: str,
        timeout_s: float,
        *,
        force_replace: bool = False,
        _deadline: float | None = None,
    ) -> dict[str, Any]:
        del force_replace
        deadline = self._operation_deadline(timeout_s, _deadline)
        activity = self.response_activity(
            self.role_snapshot(
                browser_role,
                timeout_s=self._bounded_timeout(deadline, context=f"{browser_role} prompt snapshot"),
            )
        )
        while activity.active:
            self.wait_for_current_response(
                browser_role,
                timeout_s,
                _deadline=deadline,
            )
            activity = self.response_activity(
                self.role_snapshot(
                    browser_role,
                    timeout_s=self._bounded_timeout(deadline, context=f"{browser_role} prompt snapshot"),
                )
            )

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
                self._bounded_timeout(deadline, context=f"{browser_role} SET_PROMPT"),
                "PASTE_CONFIRMED",
                _deadline=deadline,
            )

        self.wait_for_stable_expected_prompt(
            browser_role,
            prompt,
            timeout_s,
            _deadline=deadline,
        )
        return result

    def wait_for_stable_expected_prompt(
        self,
        browser_role: str,
        prompt: str,
        timeout_s: float,
        poll_s: float = 0.1,
        stable_samples: int = 2,
        *,
        _deadline: float | None = None,
    ) -> ResponseActivity:
        deadline = self._operation_deadline(timeout_s, _deadline)
        stable = 0
        last_normalized: str | None = None
        last_activity: ResponseActivity | None = None
        while time.monotonic() < deadline:
            activity = self.response_activity(
                self.role_snapshot(
                    browser_role,
                    timeout_s=self._bounded_timeout(deadline, context=f"{browser_role} prompt readiness"),
                )
            )
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
                stable = 0
            elif activity.composer_exists and activity.send_enabled is True:
                stable = stable + 1 if normalized == last_normalized else 1
                if stable >= max(1, stable_samples):
                    return activity
            else:
                stable = 0
            last_normalized = normalized
            self._sleep_bounded(max(0.01, poll_s), deadline, context=f"{browser_role} prompt readiness")
        if last_activity and not self.composer_matches_prompt(last_activity, prompt):
            raise ManualInputPendingError(
                f"{browser_role} composer no longer contains the expected automation prompt",
            )
        if last_activity and last_activity.composer_attachment_count > 0:
            raise RuntimeError(f"{browser_role} automation prompt upload did not settle before timeout")
        raise RuntimeError(f"{browser_role} composer did not become stable and send-ready before timeout")

    def upload_files(self, browser_role: str, payload: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        deadline = self._operation_deadline(timeout_s)
        self.wait_until_clean_ready(browser_role, timeout_s, _deadline=deadline)
        return self._run_command(
            browser_role,
            "UPLOAD_FILES",
            payload,
            self._bounded_timeout(deadline, context=f"{browser_role} UPLOAD_FILES"),
            "UPLOAD_FILES_DONE",
            _deadline=deadline,
        )

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
        _deadline: float | None = None,
    ) -> str:
        deadline = self._operation_deadline(timeout_s, _deadline)
        before_send = self.response_activity(
            self.role_snapshot(
                browser_role,
                timeout_s=self._bounded_timeout(deadline, context=f"{browser_role} pre-send snapshot"),
            )
        )
        response_baseline = baseline or before_send
        payload = {"expected_text": prompt or ""}
        try:
            self._run_command(
                browser_role,
                "CLICK_SEND",
                payload,
                self._bounded_timeout(deadline, context=f"{browser_role} CLICK_SEND"),
                "SEND_ACCEPTED",
                _deadline=deadline,
            )
        except RuntimeError as exc:
            activity = self.response_activity(
                self.role_snapshot(
                    browser_role,
                    timeout_s=self._bounded_timeout(deadline, context=f"{browser_role} send recovery snapshot"),
                ),
                previous_response=response_baseline.response,
            )
            if prompt and self.has_send_evidence(activity, response_baseline):
                print(
                    f"[send-recover] role={browser_role} {exc}; send evidence detected, waiting for fresh response",
                    flush=True,
                )
                return self.wait_assistant_done(
                    browser_role,
                    timeout_s,
                    baseline=response_baseline,
                    _deadline=deadline,
                )
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
                    _deadline=deadline,
                )
                try:
                    self._run_command(
                        browser_role,
                        "CLICK_SEND",
                        payload,
                        self._bounded_timeout(deadline, context=f"{browser_role} CLICK_SEND retry"),
                        "SEND_ACCEPTED",
                        _deadline=deadline,
                    )
                except RuntimeError as retry_exc:
                    final_activity = self.response_activity(
                        self.role_snapshot(
                            browser_role,
                            timeout_s=self._bounded_timeout(deadline, context=f"{browser_role} final send snapshot"),
                        ),
                        previous_response=response_baseline.response,
                    )
                    if self.has_send_evidence(final_activity, response_baseline):
                        print(
                            f"[send-recover] role={browser_role} {retry_exc}; final send evidence detected after retry, waiting for fresh response",
                            flush=True,
                        )
                        return self.wait_assistant_done(
                            browser_role,
                            timeout_s,
                            baseline=response_baseline,
                            _deadline=deadline,
                        )
                    raise
            elif prompt:
                raise ManualInputPendingError(
                    f"{browser_role} composer changed after send failure; automated prompt ownership was lost",
                ) from exc
            else:
                raise
        return self.wait_assistant_done(
            browser_role,
            timeout_s,
            baseline=response_baseline,
            _deadline=deadline,
        )

    def wait_assistant_done(
        self,
        browser_role: str,
        timeout_s: float,
        *,
        baseline: ResponseActivity | None = None,
        _deadline: float | None = None,
    ) -> str:
        deadline = self._operation_deadline(timeout_s, _deadline)
        remaining = self._remaining_time(deadline, context=f"{browser_role} WAIT_ASSISTANT_DONE")
        if remaining < 2.0:
            raise RuntimeError(
                f"{browser_role} WAIT_ASSISTANT_DONE cannot fit the minimum browser timeout and outer grace "
                "in the remaining deadline budget"
            )
        browser_timeout_ms = max(1_000, math.ceil((remaining - 1.0) * 1_000))
        final = self.run_command(
            browser_role,
            "WAIT_ASSISTANT_DONE",
            {"timeout_ms": browser_timeout_ms},
            self._bounded_timeout(deadline, context=f"{browser_role} WAIT_ASSISTANT_DONE result"),
            _deadline=deadline,
        )
        status = str(final.get("status") or "")
        result = final.get("result") or {}
        command_reason = str((result.get("result") or {}).get("reason") or result.get("reason") or "")
        response = str(result.get("text") or "")
        result_dom_info = result.get("dom_info") if isinstance(result.get("dom_info"), dict) else {}
        result_meta = result.get("result") if isinstance(result.get("result"), dict) else {}
        result_snapshot = {
            "last_response": response,
            "dom_info": result_dom_info,
            "observation": {
                "page_instance_id": str(result_dom_info.get("page_instance_id") or ""),
                "observation_seq": result_dom_info.get("observation_seq") or 0,
            },
        }
        result_activity = self.response_activity(result_snapshot)
        browser_proof_complete = bool(
            status == "ASSISTANT_DONE"
            and not self.looks_incomplete_response(response)
            and self._int_value(result_meta.get("stable_samples")) >= 2
            and result_activity.composer_exists
            and not result_activity.stop_visible
            and not result_activity.manual_input_pending
        )
        if browser_proof_complete and (baseline is None or response != baseline.response):
            return response

        recoverable = bool(
            status in {"ASSISTANT_DONE", "ASSISTANT_TIMEOUT", "EXPIRED"}
            or not final.get("done")
            or (status == "CANCELLED" and command_reason == "owner_page_replaced")
            or status == "ERROR_COMMAND"
        )
        if not recoverable:
            raise RuntimeError(
                f"{browser_role} WAIT_ASSISTANT_DONE failed: expected recoverable terminal state, "
                f"got {status or 'timeout'} reason={command_reason or 'none'}"
            )

        print(
            f"[response-watch] role={browser_role} terminal={status or 'timeout'} "
            f"reason={command_reason or 'none'}; verifying stable current response",
            flush=True,
        )
        return self.wait_for_current_response(
            browser_role,
            timeout_s,
            require_response=True,
            baseline=baseline,
            require_fresh=baseline is not None,
            allow_reload=not (status == "CANCELLED" and command_reason == "owner_page_replaced"),
            _deadline=deadline,
        )

    def _run_command(
        self,
        role: str,
        action: str,
        payload: dict[str, Any],
        timeout_s: float,
        expected_status: str,
        *,
        _deadline: float | None = None,
    ) -> dict[str, Any]:
        result = self.run_command(role, action, payload, timeout_s, _deadline=_deadline)
        status = str(result.get("status") or "")
        if status != expected_status:
            raise RuntimeError(f"{role} {action} failed: expected {expected_status}, got {status or 'timeout'}")
        return result

    def run_command(
        self,
        role: str,
        action: str,
        payload: dict[str, Any],
        timeout_s: float,
        *,
        _deadline: float | None = None,
    ) -> dict[str, Any]:
        deadline = self._operation_deadline(timeout_s, _deadline)
        command_id = self.create_command(
            role,
            action,
            payload,
            timeout_s=self._bounded_timeout(deadline, context=f"{role} {action} create"),
        )
        if not command_id:
            raise RuntimeError(f"{role} {action} returned no command id")
        return self.wait_command(command_id, timeout_s, _deadline=deadline)

    def create_command(
        self,
        role: str,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
    ) -> str:
        data = self.json_request(
            "POST",
            "/api/admin/command",
            {"role": role, "action": action, "payload": payload or {}},
            timeout_s=timeout_s,
        )
        return str((data.get("command") or {}).get("command_id") or "")

    def update_flow_statuses(
        self,
        run_id: str,
        updates: dict[str, dict[str, Any] | None],
        *,
        request_id: str = "",
        parent_request_id: str | None = None,
        goal_hash: str | None = None,
        terminal_status: str | None = None,
        activate: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"run_id": run_id, "updates": updates}
        if request_id:
            payload["request_id"] = request_id
        if parent_request_id is not None:
            payload["parent_request_id"] = parent_request_id
        if goal_hash is not None:
            payload["goal_hash"] = goal_hash
        if terminal_status is not None:
            payload["terminal_status"] = terminal_status
        if activate:
            payload["activate"] = True
        return self.json_request("POST", "/api/admin/flow-status", payload)

    def send_flow_heartbeat(
        self,
        run_id: str,
        *,
        request_id: str = "",
        pid: int | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"run_id": run_id}
        if request_id:
            payload["request_id"] = request_id
        if pid is not None:
            payload["pid"] = pid
        return self.json_request("POST", "/api/admin/flow-heartbeat", payload, timeout_s=timeout_s)

    def cancel_command(
        self,
        command_id: str,
        state: str,
        reason: str,
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        return self.json_request(
            "POST",
            f"/api/admin/command/{urllib.parse.quote(command_id)}/cancel",
            {"state": state, "reason": reason},
            timeout_s=timeout_s,
        )

    def wait_command(
        self,
        command_id: str,
        timeout_s: float,
        *,
        expire_on_timeout: bool = False,
        expire_reason: str = "command_timeout",
        _deadline: float | None = None,
    ) -> dict[str, Any]:
        deadline = self._operation_deadline(timeout_s, _deadline)
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self.json_request(
                "GET",
                f"/api/admin/command/{urllib.parse.quote(command_id)}",
                timeout_s=self._bounded_timeout(deadline, context=f"command {command_id} poll"),
            )
            status = str(last.get("status") or "")
            if last.get("done") or re.search(
                r"DONE|FAILED|ERROR|UNKNOWN|RELOADING|NAVIGATING|SAVED|CANCELLED|EXPIRED",
                status,
            ):
                return last
            remaining = deadline - time.monotonic()
            if expire_on_timeout and remaining <= 0.5:
                return self.cancel_command(
                    command_id,
                    "EXPIRED",
                    expire_reason,
                    timeout_s=max(0.001, remaining),
                )
            self._sleep_bounded(0.5, deadline, context=f"command {command_id} poll")
        return last

    def command_roundtrip(
        self,
        role: str,
        action: str,
        timeout_s: float = 20.0,
        *,
        _deadline: float | None = None,
    ) -> dict[str, Any]:
        deadline = self._operation_deadline(timeout_s, _deadline)
        command_id = self.create_command(
            role,
            action,
            {"source": "main_preflight"},
            timeout_s=self._bounded_timeout(deadline, context=f"{role} {action} create"),
        )
        if not command_id:
            return {"ok": False, "status": "NO_COMMAND_ID", "done": False}
        normalized_action = str(action or "").upper()
        result = self.wait_command(
            command_id,
            timeout_s,
            expire_on_timeout=normalized_action in EXPIRABLE_COMMANDS,
            expire_reason=f"{normalized_action.lower()}_timeout",
            _deadline=deadline,
        )
        ok = bool(result.get("done")) and str(result.get("status") or "") not in {"CANCELLED", "EXPIRED"}
        return {"ok": ok, "command_id": command_id, **result}

    def role_snapshot(self, role: str, *, timeout_s: float | None = None) -> dict[str, Any]:
        return self.json_request(
            "GET",
            f"/api/admin/role/{urllib.parse.quote(role)}",
            timeout_s=timeout_s,
        )

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    @staticmethod
    def _mapping(value: Any, field: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError(f"{field} must be an object")
        return value

    @classmethod
    def _mapping_field(cls, parent: dict[str, Any], field: str, parent_name: str) -> dict[str, Any]:
        if field not in parent:
            return {}
        return cls._mapping(parent[field], f"{parent_name}.{field}")

    @staticmethod
    def _list_field(parent: dict[str, Any], field: str, parent_name: str) -> list[Any]:
        if field not in parent:
            return []
        value = parent[field]
        if not isinstance(value, list):
            raise ValueError(f"{parent_name}.{field} must be a list")
        return value

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
        snapshot = cls._mapping(snapshot, "snapshot")
        dom_info = cls._mapping_field(snapshot, "dom_info", "snapshot")
        messages = cls._mapping_field(dom_info, "messages", "snapshot.dom_info")
        counts = cls._mapping_field(messages, "counts", "snapshot.dom_info.messages")
        last_user = messages.get("last_user") or {}
        last_user_text = str(last_user.get("text") or "") if isinstance(last_user, dict) else ""
        observation = cls._mapping_field(snapshot, "observation", "snapshot")
        response = str(snapshot.get("last_response") or "")
        composer_text = str(dom_info.get("composer_text") or "")
        composer_text_len = cls._int_value(dom_info.get("composer_text_len"), len(composer_text))
        attachments = cls._list_field(dom_info, "composer_attachments", "snapshot.dom_info")
        choice_candidates = cls._list_field(dom_info, "choice_prompt_candidates", "snapshot.dom_info")
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
            page_instance_id=str(
                dom_info.get("page_instance_id")
                or observation.get("page_instance_id")
                or ""
            ),
            observation_seq=cls._int_value(observation.get("observation_seq")),
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
        without_language_label = re.sub(r"(?is)^json\b\s*", "", value, count=1).strip()
        if not without_language_label:
            return True
        if without_language_label.count("```") % 2 == 1:
            return True
        if re.fullmatch(r"(?is)```\s*(?:json)?\s*```", without_language_label):
            return True
        if re.match(r"(?is)^\{\s*$", without_language_label):
            return True
        if without_language_label.startswith("{") or re.search(r"(?is)```\s*json", without_language_label):
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
        allow_reload: bool = True,
        *,
        _deadline: float | None = None,
    ) -> str:
        deadline = self._operation_deadline(timeout_s, _deadline)
        cycle_started = time.monotonic()
        last_response = ""
        last_logged_bucket = -1
        last_activity: ResponseActivity | None = None
        active_reload_used = False
        stable_key: tuple[str, int, str] | None = None
        stable_samples = 0
        stable_last_seq = 0
        strongest_key: tuple[str, int, str] | None = None
        strongest_response = ""

        while time.monotonic() < deadline:
            try:
                self.command_roundtrip(
                    role,
                    "SYNC_TRANSCRIPT",
                    timeout_s=self._bounded_timeout(deadline, 20.0, context=f"{role} transcript sync"),
                    _deadline=deadline,
                )
                activity = self.response_activity(
                    self.role_snapshot(
                        role,
                        timeout_s=self._bounded_timeout(deadline, context=f"{role} response snapshot"),
                    ),
                    previous_response=last_response,
                )
            except Exception as exc:
                print(f"[response-watch] role={role} transient snapshot/sync failure: {exc}; waiting", flush=True)
                self._sleep_bounded(max(0.1, poll_s), deadline, context=f"{role} response recovery")
                continue

            last_activity = activity
            last_response = activity.response

            if self.is_manual_input_pending(activity):
                stable_key = None
                stable_samples = 0
                print(
                    f"[response-watch] role={role} manual_input_pending=true "
                    f"composer_len={activity.composer_text_len} attachments={activity.composer_attachment_count}; "
                    "waiting without send/reload",
                    flush=True,
                )
                self._sleep_bounded(max(0.1, poll_s), deadline, context=f"{role} manual input wait")
                continue

            if activity.blocked_by_choice_prompt:
                stable_key = None
                stable_samples = 0
                labels = ", ".join(activity.choice_prompt_labels[:5]) or "unknown"
                print(
                    f"[response-watch] role={role} choice prompt pending labels={labels}; clicking safe choice",
                    flush=True,
                )
                result = self.command_roundtrip(
                    role,
                    "CLICK_CHOICE_PROMPT",
                    timeout_s=self._bounded_timeout(deadline, 20.0, context=f"{role} choice prompt"),
                    _deadline=deadline,
                )
                status = str(result.get("status") or "")
                if status != "CHOICE_PROMPT_CLICKED":
                    raise RuntimeError(
                        f"{role} choice prompt blocked response recovery and could not be resolved: "
                        f"status={status or 'timeout'} labels={labels}",
                    )
                self._sleep_bounded(max(0.5, poll_s), deadline, context=f"{role} choice prompt settle")
                last_logged_bucket = -1
                continue

            fresh = not require_fresh or self.is_fresh_response(activity, baseline)
            complete = bool(
                activity.has_response
                and fresh
                and activity.composer_exists
                and not activity.stop_visible
                and not self.looks_incomplete_response(activity.response)
            )
            candidate_key = (activity.response, activity.assistant_count, activity.page_instance_id)
            shorter_prefix = bool(
                strongest_key
                and strongest_key[1:] == candidate_key[1:]
                and strongest_response.startswith(activity.response)
                and len(activity.response) < len(strongest_response)
            )

            if complete and not shorter_prefix:
                if (
                    strongest_key is None
                    or candidate_key == strongest_key
                    or len(activity.response) >= len(strongest_response)
                    or not activity.response.startswith(strongest_response)
                ):
                    strongest_key = candidate_key
                    strongest_response = activity.response

                seq_is_new = bool(
                    activity.observation_seq <= 0
                    or stable_last_seq <= 0
                    or activity.observation_seq > stable_last_seq
                )
                if candidate_key == stable_key and seq_is_new:
                    stable_samples += 1
                else:
                    stable_key = candidate_key
                    stable_samples = 1
                stable_last_seq = activity.observation_seq
                if stable_samples >= 2:
                    return activity.response
            else:
                stable_key = None
                stable_samples = 0
                stable_last_seq = 0

            if activity.has_response and self.looks_incomplete_response(activity.response):
                print(
                    f"[response-watch] role={role} response looks incomplete len={activity.response_len}; waiting",
                    flush=True,
                )

            if not self.is_response_active(activity):
                if not activity.has_response and not require_response:
                    return ""
                self._sleep_bounded(max(0.1, poll_s), deadline, context=f"{role} stable response wait")
                continue

            elapsed_in_cycle = time.monotonic() - cycle_started
            bucket = int(elapsed_in_cycle // max(1.0, poll_s * 5))
            if bucket != last_logged_bucket:
                state = "streaming" if self.is_response_streaming(activity) else "active"
                print(
                    f"[response-watch] role={role} state={state} stop_visible=true "
                    f"response_len={len(last_response)} elapsed={elapsed_in_cycle:.1f}s/{active_wait_s:.1f}s",
                    flush=True,
                )
                last_logged_bucket = bucket

            if (
                allow_reload
                and self.is_response_stuck(activity, elapsed_in_cycle, active_wait_s)
                and not active_reload_used
            ):
                print(
                    f"[response-watch] role={role} still active after {active_wait_s:.1f}s; reloading page once",
                    flush=True,
                )
                active_reload_used = True
                self.command_roundtrip(
                    role,
                    "RELOAD_PAGE",
                    timeout_s=self._bounded_timeout(deadline, 20.0, context=f"{role} response reload"),
                    _deadline=deadline,
                )
                self._sleep_bounded(max(0.0, page_wait_s), deadline, context=f"{role} response reload settle")
                cycle_started = time.monotonic()
                stable_key = None
                stable_samples = 0
                stable_last_seq = 0
                last_logged_bucket = -1
                continue

            self._sleep_bounded(max(0.1, poll_s), deadline, context=f"{role} response poll")

        if last_activity and self.is_manual_input_pending(last_activity):
            raise ManualInputPendingError(
                f"{role} composer still has manual input after waiting; not sending automated prompt",
            )
        if last_activity and self.is_response_active(last_activity):
            raise RuntimeError(f"{role} response still active after timeout; last_response_len={len(last_response)}")
        if strongest_response:
            raise RuntimeError(
                f"{role} response wait timed out before two stable complete observations; "
                f"strongest_response_len={len(strongest_response)}"
            )
        if last_response:
            state = "incomplete" if self.looks_incomplete_response(last_response) else "unstable"
            raise RuntimeError(f"{role} response wait timed out with {state} response; last_response_len={len(last_response)}")
        if require_response:
            raise RuntimeError(f"{role} response wait timed out while waiting for recovered response")
        raise RuntimeError(f"{role} response wait timed out; last_response_len={len(last_response)}")

    def wait_until_clean_ready(
        self,
        role: str,
        timeout_s: float,
        poll_s: float = DEFAULT_RESPONSE_RECOVERY_POLL_S,
        *,
        _deadline: float | None = None,
    ) -> ResponseActivity:
        deadline = self._operation_deadline(timeout_s, _deadline)
        last_activity: ResponseActivity | None = None
        last_logged_bucket = -1
        while time.monotonic() < deadline:
            try:
                activity = self.response_activity(
                    self.role_snapshot(
                        role,
                        timeout_s=self._bounded_timeout(deadline, context=f"{role} clean-ready snapshot"),
                    )
                )
            except Exception as exc:
                print(f"[ready-check] role={role} transient snapshot failure: {exc}; waiting", flush=True)
                self._sleep_bounded(max(0.1, poll_s), deadline, context=f"{role} clean-ready wait")
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
                result = self.command_roundtrip(
                    role,
                    "CLICK_CHOICE_PROMPT",
                    timeout_s=self._bounded_timeout(deadline, 20.0, context=f"{role} clean-ready choice"),
                    _deadline=deadline,
                )
                status = str(result.get("status") or "")
                if status != "CHOICE_PROMPT_CLICKED":
                    raise RuntimeError(
                        f"{role} choice prompt blocked composer and could not be resolved: "
                        f"status={status or 'timeout'} labels={labels}",
                    )
                self._sleep_bounded(max(0.5, poll_s), deadline, context=f"{role} clean-ready choice settle")
                last_logged_bucket = -1
                continue
            remaining_s = max(0.0, deadline - time.monotonic())
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
                self.wait_for_current_response(role, timeout_s, _deadline=deadline)
                continue
            self._sleep_bounded(max(0.1, poll_s), deadline, context=f"{role} clean-ready poll")
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
        *,
        _deadline: float | None = None,
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
            _deadline=_deadline,
        )

    @classmethod
    def _snapshot_page_generation(cls, snapshot: dict[str, Any]) -> str:
        snapshot = cls._mapping(snapshot, "snapshot")
        dom_info = cls._mapping_field(snapshot, "dom_info", "snapshot")
        return str(dom_info.get("page_instance_id") or "")

    @classmethod
    def _snapshot_page_path(cls, snapshot: dict[str, Any]) -> str:
        snapshot = cls._mapping(snapshot, "snapshot")
        dom_info = cls._mapping_field(snapshot, "dom_info", "snapshot")
        return str(dom_info.get("page_path") or "")

    @classmethod
    def is_clean_root_snapshot(cls, snapshot: dict[str, Any]) -> bool:
        snapshot = cls._mapping(snapshot, "snapshot")
        dom_info = cls._mapping_field(snapshot, "dom_info", "snapshot")
        messages = cls._mapping_field(dom_info, "messages", "snapshot.dom_info")
        counts = cls._mapping_field(messages, "counts", "snapshot.dom_info.messages")
        observation = cls._mapping_field(snapshot, "observation", "snapshot")
        generation = cls._snapshot_page_generation(snapshot)
        observed_generation = str(observation.get("page_instance_id") or "")
        path = cls._snapshot_page_path(snapshot)
        activity = cls.response_activity(snapshot)
        return bool(
            snapshot.get("online") is not False
            and not snapshot.get("active_command")
            and generation
            and (not observed_generation or observed_generation == generation)
            and path == "/"
            and activity.composer_exists
            and not activity.composer_text
            and activity.composer_attachment_count == 0
            and not activity.stop_visible
            and not activity.choice_prompt_pending
            and not activity.response
            and not activity.last_user_text
            and cls._int_value(counts.get("user")) == 0
            and cls._int_value(counts.get("assistant")) == 0
        )

    @classmethod
    def is_clean_new_chat_snapshot(cls, snapshot: dict[str, Any], previous_generation: str) -> bool:
        generation = cls._snapshot_page_generation(snapshot)
        return bool(
            previous_generation
            and generation != previous_generation
            and cls.is_clean_root_snapshot(snapshot)
        )

    def wait_new_chat_ready(
        self,
        role: str,
        before_snapshot: dict[str, Any],
        timeout_s: float,
        poll_s: float = 0.5,
        *,
        _deadline: float | None = None,
    ) -> dict[str, Any]:
        previous_generation = self._snapshot_page_generation(before_snapshot)
        if not previous_generation:
            raise RuntimeError(f"{role} reset readiness cannot be verified: missing pre-reset page_instance_id")
        deadline = self._operation_deadline(timeout_s, _deadline)
        last_probe: dict[str, Any] = {}
        last_snapshot: dict[str, Any] = {}
        while time.monotonic() < deadline:
            try:
                candidate = self.role_snapshot(
                    role,
                    timeout_s=self._bounded_timeout(deadline, context=f"{role} NEW_CHAT readiness snapshot"),
                )
                last_snapshot = candidate
                if not self.is_clean_new_chat_snapshot(candidate, previous_generation):
                    self._sleep_bounded(max(0.05, poll_s), deadline, context=f"{role} NEW_CHAT readiness")
                    continue
                candidate_generation = self._snapshot_page_generation(candidate)
                last_probe = self.command_roundtrip(
                    role,
                    "PROBE",
                    timeout_s=self._bounded_timeout(deadline, 20.0, context=f"{role} NEW_CHAT probe"),
                    _deadline=deadline,
                )
                if not (last_probe.get("done") and str(last_probe.get("status") or "") == "PROBE_DONE"):
                    self._sleep_bounded(max(0.05, poll_s), deadline, context=f"{role} NEW_CHAT probe settle")
                    continue
                last_snapshot = self.role_snapshot(
                    role,
                    timeout_s=self._bounded_timeout(deadline, context=f"{role} NEW_CHAT final snapshot"),
                )
                if (
                    self._snapshot_page_generation(last_snapshot) == candidate_generation
                    and self.is_clean_new_chat_snapshot(last_snapshot, previous_generation)
                ):
                    return {
                        "done": True,
                        "status": "NEW_CHAT_READY",
                        "probe": last_probe,
                        "page_instance_id": candidate_generation,
                        "page_path": self._snapshot_page_path(last_snapshot),
                    }
            except RuntimeError:
                pass
            self._sleep_bounded(max(0.05, poll_s), deadline, context=f"{role} NEW_CHAT readiness")
        raise RuntimeError(
            f"{role} new chat did not reach terminal clean readiness before timeout; "
            f"last_probe_status={last_probe.get('status') or 'none'} "
            f"last_generation={self._snapshot_page_generation(last_snapshot) or 'none'} "
            f"last_path={self._snapshot_page_path(last_snapshot) or 'none'}"
        )

    def new_chat(
        self,
        role: str,
        timeout_s: float = 25.0,
        *,
        _deadline: float | None = None,
    ) -> dict[str, Any]:
        deadline = self._operation_deadline(timeout_s, _deadline)
        return self.command_roundtrip(
            role,
            "NEW_CHAT",
            timeout_s=self._bounded_timeout(deadline, context=f"{role} NEW_CHAT"),
            _deadline=deadline,
        )
