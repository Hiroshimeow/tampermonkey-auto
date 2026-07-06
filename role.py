#!/usr/bin/env python3
"""Send one prompt to one browser role with durable retry/upload handling."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any

import agents
from apps.bridge import BridgeClient, ManualInputPendingError, RoleHealth, UploadReadiness
from apps.constants import DEFAULT_BASE_URL
from apps.role_renderer import render_direct_role_prompt, rendered_hash_source
from apps.text import normalize_role

STATE_DIR = Path(".role_state")
REQUESTS_DIR = STATE_DIR / "requests"
RESPONSES_DIR = STATE_DIR / "responses"
UPLOADS_DIR = STATE_DIR / "uploads"
LOGS_DIR = STATE_DIR / "logs"
PROMPT_SPILL_THRESHOLD = 8000
REQUEST_MARKER = "ROLE_REQUEST_ID"
RECOVERY_LEDGER_STATUSES = {
    "uploading",
    "upload_ready",
    "sending",
    "uploaded",
    "sent",
    "failed_retryable",
}


@dataclass(frozen=True, slots=True)
class ComposerState:
    text_len: int
    marker_present: bool
    attachment_count: int
    expected_attachment_count: int
    send_enabled: bool | None

    def as_payload(self) -> dict[str, Any]:
        return {
            "text_len": self.text_len,
            "marker_present": self.marker_present,
            "attachment_count": self.attachment_count,
            "expected_attachment_count": self.expected_attachment_count,
            "send_enabled": self.send_enabled,
        }


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    response: str
    status: str
    state: str
    action: str
    recoverable: bool
    role_health: str
    composer: ComposerState
    send_allowed: bool = False


@dataclass(frozen=True, slots=True)
class RoleRequestStateError(Exception):
    outcome: RecoveryOutcome

    def __str__(self) -> str:
        return f"{self.outcome.status}:{self.outcome.state}:{self.outcome.action}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send a single prompt to a single MAuto browser role and wait for the response.",
    )
    parser.add_argument("--role", default="", help="Target browser role, for example DEV, REVIEW, PLAN, or a custom role.")
    parser.add_argument("--prompt", default="", help="Prompt text to send. If omitted, stdin is used.")
    parser.add_argument("--upload", action="append", default=[], help="Local file path to upload before sending. Repeat for multiple files.")
    parser.add_argument("--request-id", default="", help="Resume or run an exact durable request id.")
    parser.add_argument("--new-request", action="store_true", help="Force a new logical request even when the same prompt/files were used before.")
    parser.add_argument("--resp-from", default="", help="Optional source role. Prefix the prompt with up to 3 latest assistant responses from that role.")
    parser.add_argument("--new-chat", action="store_true", help="Open a new chat for the target role before sending the prompt.")
    parser.add_argument("--restart", action="store_true", help="Reload the target role browser tab before sending the prompt.")
    parser.add_argument("--base-url", default=os.environ.get("MAUTO_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--timeout", type=float, default=1800.0, help="Max seconds to wait for browser readiness and assistant completion.")
    parser.add_argument("--request-timeout", type=float, default=1200.0, help="HTTP request timeout for bridge calls.")
    return parser.parse_args(argv)


def configure_stdio_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not reconfigure:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_state_dirs() -> None:
    for path in (REQUESTS_DIR, RESPONSES_DIR, UPLOADS_DIR, LOGS_DIR):
        path.mkdir(parents=True, exist_ok=True)


def sha256_text(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def short_hash(text: str, length: int = 12) -> str:
    return sha256_text(text)[:length]


def make_run_id() -> str:
    return f"run_{datetime.now().strftime('%Y%m%d%H%M%S')}_{short_hash(str(time.time_ns()), 6)}"


def make_error_id(request_id: str, error: BaseException | str) -> str:
    return f"err_{request_id}_{short_hash(str(error) + str(time.time_ns()), 6)}"


def read_prompt(args: argparse.Namespace) -> str:
    if args.prompt:
        return str(args.prompt).strip()
    if sys.stdin.isatty():
        return ""

    result: list[str] = []

    def read_stdin() -> None:
        try:
            result.append(sys.stdin.read())
        except OSError:
            result.append("")

    reader = threading.Thread(target=read_stdin, daemon=True)
    reader.start()
    reader.join(1.0)
    if reader.is_alive() or not result:
        return ""
    return result[0].strip()


def validate_upload_paths(paths: list[str]) -> tuple[list[Path], list[str]]:
    valid: list[Path] = []
    errors: list[str] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.exists():
            errors.append(f"upload path does not exist: {raw}")
            continue
        if not path.is_file():
            errors.append(f"upload path is not a file: {raw}")
            continue
        valid.append(path.resolve())
    return valid, errors


def upload_metadata(paths: list[Path]) -> list[dict[str, str | int]]:
    return [
        {
            "path": str(path),
            "filename": path.name,
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
        }
        for path in paths
    ]


def assistant_responses_from_snapshot(snapshot: dict, limit: int = 3) -> list[str]:
    dom_info = snapshot.get("dom_info") or {}
    messages_payload = dom_info.get("messages") or {}
    messages = messages_payload.get("messages") or []
    responses = []
    if isinstance(messages, list):
        for item in messages:
            if not isinstance(item, dict):
                continue
            if str(item.get("role") or "").lower() != "assistant":
                continue
            text = str(item.get("text") or "").strip()
            if text:
                responses.append(text)
    if not responses:
        last_response = str(snapshot.get("last_response") or "").strip()
        if last_response:
            responses.append(last_response)
    return responses[-limit:]


def build_prompt(prompt: str, source_role: str, source_responses: list[str]) -> str:
    if not source_role or not source_responses:
        return prompt
    parts = [f"RESPONSES_FROM {source_role} (latest {len(source_responses)}):"]
    for index, response in enumerate(source_responses, start=1):
        parts.append(f"--- RESPONSE {index} ---\n{response}")
    parts.append("PROMPT:")
    parts.append(prompt)
    return "\n\n".join(parts)


def fetch_source_responses(client: BridgeClient, source_role: str) -> list[str]:
    snapshot = client.role_snapshot(normalize_role(source_role))
    return assistant_responses_from_snapshot(snapshot, limit=3)


def idempotency_key(role: str, prompt: str, source_role: str, role_prompt_hash: str, uploads: list[dict[str, Any]]) -> str:
    payload = {
        "role": role,
        "prompt": " ".join(prompt.split()),
        "source_role": source_role,
        "role_prompt_hash": role_prompt_hash,
        "uploads": [(item.get("path"), item.get("sha256"), item.get("size")) for item in uploads],
    }
    return sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def request_path(request_id: str) -> Path:
    return REQUESTS_DIR / f"{request_id}.json"


def response_path(request_id: str) -> Path:
    return RESPONSES_DIR / f"{request_id}.md"


def find_existing_request(key: str) -> dict[str, Any] | None:
    if not REQUESTS_DIR.exists():
        return None
    for path in sorted(REQUESTS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("idempotency_key") == key and data.get("status") != "failed_final":
            return data
    return None


def make_request_id(role: str, key: str) -> str:
    return f"req_{role}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{key[:8]}"


def load_ledger(request_id: str) -> dict[str, Any] | None:
    path = request_path(request_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_ledger(data: dict[str, Any]) -> None:
    ensure_state_dirs()
    data["updated_at"] = utc_now()
    request_path(str(data["request_id"])).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def init_or_load_ledger(
    *,
    explicit_request_id: str,
    force_new: bool,
    role: str,
    key: str,
    prompt_hash: str,
    role_prompt_hash: str,
    uploads: list[dict[str, Any]],
) -> dict[str, Any]:
    ensure_state_dirs()
    if explicit_request_id:
        existing = load_ledger(explicit_request_id)
        if existing:
            return existing
        request_id = explicit_request_id
    else:
        existing = None if force_new else find_existing_request(key)
        if existing:
            return existing
        request_id = make_request_id(role, key)
    ledger = {
        "request_id": request_id,
        "idempotency_key": key,
        "role": role,
        "status": "new",
        "prompt_hash": prompt_hash,
        "role_prompt_hash": role_prompt_hash,
        "role_context_hash": role_prompt_hash,
        "uploads": uploads,
        "response_path": str(response_path(request_id)),
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    save_ledger(ledger)
    return ledger


def command_failed(result: dict) -> bool:
    status = str(result.get("status") or "")
    if not result.get("done"):
        return True
    return any(marker in status for marker in ("FAILED", "ERROR", "UNKNOWN"))


def run_pre_send_actions(client: BridgeClient, role: str, *, restart: bool, new_chat: bool, timeout_s: float) -> None:
    action_timeout = max(1.0, min(float(timeout_s), 60.0))
    if restart:
        result = client.command_roundtrip(role, "RELOAD_PAGE", timeout_s=action_timeout)
        if command_failed(result):
            raise RuntimeError(f"restart failed for role {role}: {result}")
        if hasattr(client, "wait_until_clean_ready"):
            client.wait_until_clean_ready(role, min(float(timeout_s), 30.0))
    if new_chat:
        result = client.new_chat(role, timeout_s=action_timeout)
        if command_failed(result):
            raise RuntimeError(f"new-chat failed for role {role}: {result}")
        if hasattr(client, "wait_until_clean_ready"):
            client.wait_until_clean_ready(role, min(float(timeout_s), 30.0))

def snapshot_messages(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    dom_info = snapshot.get("dom_info") or {}
    messages_payload = dom_info.get("messages") or {}
    messages = messages_payload.get("messages") or []
    return messages if isinstance(messages, list) else []


def find_response_for_marker(snapshot: dict[str, Any], request_id: str) -> tuple[str, bool, bool]:
    marker = f"{REQUEST_MARKER}: {request_id}"
    messages = snapshot_messages(snapshot)
    marker_index = -1
    for index, item in enumerate(messages):
        if not isinstance(item, dict):
            continue
        if marker in str(item.get("text") or ""):
            marker_index = index
    if marker_index < 0:
        composer_text = str((snapshot.get("dom_info") or {}).get("composer_text") or "")
        return "", False, marker in composer_text
    assistant_text = ""
    for item in messages[marker_index + 1:]:
        if isinstance(item, dict) and str(item.get("role") or "").lower() == "assistant":
            text = str(item.get("text") or "").strip()
            if text:
                assistant_text = text
    return assistant_text, True, False


def composer_state_from_activity(activity: Any, request_id: str, expected_attachment_count: int) -> ComposerState:
    return ComposerState(
        text_len=int(activity.composer_text_len),
        marker_present=f"{REQUEST_MARKER}: {request_id}" in str(activity.composer_text or ""),
        attachment_count=int(activity.composer_attachment_count),
        expected_attachment_count=expected_attachment_count,
        send_enabled=activity.send_enabled,
    )


def empty_composer_state(expected_attachment_count: int) -> ComposerState:
    return ComposerState(
        text_len=0,
        marker_present=False,
        attachment_count=0,
        expected_attachment_count=expected_attachment_count,
        send_enabled=None,
    )


def outcome_from_upload_readiness(readiness: UploadReadiness) -> RecoveryOutcome:
    composer = ComposerState(
        text_len=readiness.composer_text_len,
        marker_present=readiness.marker_present,
        attachment_count=readiness.composer_attachment_count,
        expected_attachment_count=readiness.expected_attachment_count,
        send_enabled=readiness.send_enabled,
    )
    if readiness.state == "role_unhealthy":
        return RecoveryOutcome(
            response="",
            status="role_unhealthy",
            state="role_tab_unhealthy",
            action="fresh_tab_or_rerole_required",
            recoverable=False,
            role_health=readiness.role_health,
            composer=composer,
        )
    state_map = {
        "upload_text_missing": ("unfinished_upload_send", "composer_prompt_missing_attachments", "reload_then_reupload", True),
        "upload_attachments_missing": ("unfinished_upload_send", "composer_prompt_missing_attachments", "reload_then_reupload", True),
        "upload_composer_missing": ("unfinished_upload_send", "composer_prompt_missing_attachments", "reload_then_reupload", True),
        "upload_choice_prompt": ("unfinished_upload_send", "composer_prompt_missing_attachments", "reload_then_reupload", True),
        "upload_waiting": ("unfinished_upload_send", "composer_prompt_missing_attachments", "reload_then_reupload", True),
    }
    status, state, action, recoverable = state_map.get(
        readiness.state,
        ("failed_retryable", readiness.state, "retry_same_request_or_new_request", True),
    )
    return RecoveryOutcome(
        response="",
        status=status,
        state=state,
        action=action,
        recoverable=recoverable,
        role_health=readiness.role_health,
        composer=composer,
    )


def recover_role_tab(client: BridgeClient, role: str, timeout_s: float, *, allow_new_chat: bool) -> tuple[dict[str, Any] | None, RoleHealth]:
    action_timeout = max(1.0, min(float(timeout_s), 20.0))
    snapshot = client.role_snapshot(role)
    health = client.role_health(snapshot)
    if health.healthy:
        return snapshot, health

    reload_result = client.command_roundtrip(role, "RELOAD_PAGE", timeout_s=action_timeout)
    if not command_failed(reload_result):
        client.sleep(1.0)
        snapshot = client.role_snapshot(role)
        health = client.role_health(snapshot)
        if health.healthy:
            return snapshot, health

    if allow_new_chat:
        new_chat_result = client.new_chat(role, timeout_s=action_timeout)
        if not command_failed(new_chat_result):
            client.sleep(1.0)
            snapshot = client.role_snapshot(role)
            health = client.role_health(snapshot)
            if health.healthy:
                return snapshot, health

    return None, RoleHealth(
        healthy=False,
        state="role_tab_unhealthy",
        action="fresh_tab_or_rerole_required",
        status=health.status,
        session_count=health.session_count,
    )


def recover_existing_response(
    client: BridgeClient,
    role: str,
    request_id: str,
    timeout_s: float,
    *,
    expected_attachment_count: int,
    ledger_status: str,
    allow_new_chat: bool,
) -> RecoveryOutcome:
    snapshot, initial_health = recover_role_tab(client, role, timeout_s, allow_new_chat=allow_new_chat)
    if snapshot is None:
        return RecoveryOutcome(
            response="",
            status="role_unhealthy",
            state="role_tab_unhealthy",
            action="fresh_tab_or_rerole_required",
            recoverable=False,
            role_health=initial_health.state,
            composer=empty_composer_state(expected_attachment_count),
        )
    deadline = time.time() + min(timeout_s, 30.0)
    while time.time() < deadline:
        try:
            client.command_roundtrip(role, "SYNC_TRANSCRIPT", timeout_s=20.0)
            snapshot = client.role_snapshot(role)
            health = client.role_health(snapshot)
            if not health.healthy:
                return RecoveryOutcome(
                    response="",
                    status="role_unhealthy",
                    state="role_tab_unhealthy",
                    action="fresh_tab_or_rerole_required",
                    recoverable=False,
                    role_health=health.state,
                    composer=empty_composer_state(expected_attachment_count),
                )
        except Exception:
            time.sleep(0.5)
            continue
        response, marker_found, composer_marker = find_response_for_marker(snapshot, request_id)
        activity = client.response_activity(snapshot)
        composer = composer_state_from_activity(activity, request_id, expected_attachment_count)
        if response:
            return RecoveryOutcome(
                response=response,
                status="completed",
                state="completed",
                action="none",
                recoverable=False,
                role_health="healthy",
                composer=composer,
            )
        if marker_found and activity.stop_visible:
            return RecoveryOutcome(
                response="",
                status="unfinished_upload_send",
                state="sent_waiting_response",
                action="wait",
                recoverable=True,
                role_health="healthy",
                composer=composer,
            )
        if composer.marker_present and composer.attachment_count >= expected_attachment_count:
            state = "upload_ready_not_sent" if ledger_status in {"upload_ready", "uploaded", "failed_retryable"} else "composer_prompt_and_attachments_pending"
            return RecoveryOutcome(
                response="",
                status="unfinished_upload_send",
                state=state,
                action="safe_click_send",
                recoverable=True,
                role_health="healthy",
                composer=composer,
                send_allowed=activity.send_enabled is not False,
            )
        if composer.marker_present and expected_attachment_count == 0:
            return RecoveryOutcome(
                response="",
                status="unfinished_upload_send",
                state="composer_prompt_only_pending",
                action="safe_click_send",
                recoverable=True,
                role_health="healthy",
                composer=composer,
                send_allowed=activity.send_enabled is not False,
            )
        if composer.marker_present:
            return RecoveryOutcome(
                response="",
                status="unfinished_upload_send",
                state="composer_prompt_missing_attachments",
                action="reload_then_reupload",
                recoverable=True,
                role_health="healthy",
                composer=composer,
            )
        if composer.attachment_count > 0 or composer_marker:
            return RecoveryOutcome(
                response="",
                status="unfinished_upload_send",
                state="composer_attachments_without_marker",
                action="new_chat_and_reupload",
                recoverable=True,
                role_health="healthy",
                composer=composer,
            )
        if activity.composer_text.strip():
            return RecoveryOutcome(
                response="",
                status="manual_input_pending",
                state="manual_composer_dirty",
                action="manual_clear_required",
                recoverable=False,
                role_health="healthy",
                composer=composer,
            )
        time.sleep(0.5)
    if ledger_status in {"sent", "sending"}:
        return RecoveryOutcome(
            response="",
            status="unfinished_upload_send",
            state="sent_marker_missing",
            action="retry_same_request_or_new_request",
            recoverable=True,
            role_health="healthy",
            composer=empty_composer_state(expected_attachment_count),
        )
    return RecoveryOutcome(
        response="",
        status="failed_retryable",
        state="recovery_marker_not_found",
        action="retry_same_request_or_new_request",
        recoverable=True,
        role_health="healthy",
        composer=empty_composer_state(expected_attachment_count),
    )


def save_response(request_id: str, response: str) -> Path:
    ensure_state_dirs()
    path = response_path(request_id)
    path.write_text(str(response or "").strip() + "\n", encoding="utf-8")
    return path


def write_error_log(error_id: str, payload: dict[str, Any]) -> Path:
    ensure_state_dirs()
    path = LOGS_DIR / f"{error_id}.log"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def emit_json(payload: dict) -> None:
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    try:
        stdout_buffer = getattr(sys.stdout, "buffer", None)
        if stdout_buffer is not None:
            stdout_buffer.write(line.encode("utf-8"))
            stdout_buffer.flush()
            return
        sys.stdout.write(line)
        sys.stdout.flush()
    except UnicodeEncodeError:
        fallback = json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n"
        sys.stdout.write(fallback)
        sys.stdout.flush()


def success_payload(*, request_id: str, run_id: str, role: str, response_file: Path, uploaded: int, source_role: str, source_response_count: int, recovered: bool = False) -> dict[str, Any]:
    return {
        "ok": True,
        "status": "completed",
        "exit_code": 0,
        "request_id": request_id,
        "run_id": run_id,
        "role": role,
        "resp_from": source_role or None,
        "source_response_count": source_response_count,
        "response_path": str(response_file),
        "uploaded": uploaded,
        "recovered": recovered,
        "error": None,
    }


def recovery_payload(*, exit_code: int, request_id: str, run_id: str, role: str, outcome: RecoveryOutcome) -> dict[str, Any]:
    return {
        "ok": False,
        "status": outcome.status,
        "exit_code": exit_code,
        "request_id": request_id or None,
        "run_id": run_id,
        "role": role,
        "state": outcome.state,
        "action": outcome.action,
        "recoverable": outcome.recoverable,
        "role_health": outcome.role_health,
        "composer": outcome.composer.as_payload(),
        "message": outcome.state,
        "error": None,
    }


def fail_payload(*, exit_code: int, status: str, request_id: str, run_id: str, role: str, error: BaseException | str, message: str = "") -> dict[str, Any]:
    error_id = make_error_id(request_id or "no_request", error)
    error_payload = {
        "type": type(error).__name__ if isinstance(error, BaseException) else "Error",
        "message": str(error),
    }
    payload = {
        "ok": False,
        "status": status,
        "exit_code": exit_code,
        "request_id": request_id or None,
        "run_id": run_id,
        "error_id": error_id,
        "role": role,
        "message": message or str(error),
        "error": error_payload,
    }
    payload["log_path"] = str(write_error_log(error_id, payload))
    return payload


def maybe_spill_prompt(request_id: str, prompt: str, uploads: list[Path]) -> tuple[str, list[Path]]:
    if len(prompt) <= PROMPT_SPILL_THRESHOLD:
        return prompt, uploads
    spill_dir = UPLOADS_DIR / request_id
    spill_dir.mkdir(parents=True, exist_ok=True)
    prompt_file = spill_dir / "prompt.md"
    prompt_file.write_text(prompt, encoding="utf-8")
    short_prompt = (
        f"{REQUEST_MARKER}: {request_id}\n\n"
        "Read attached prompt.md and any other attached files. "
        "Follow the role instructions inside prompt.md and return the requested result directly."
    )
    return short_prompt, [prompt_file, *uploads]


def run_role_request(client: BridgeClient, role: str, final_prompt: str, uploads: list[Path], ledger: dict[str, Any], timeout_s: float) -> str:
    if not hasattr(client, "set_prompt") and hasattr(client, "call_browser_role"):
        return client.call_browser_role(role, final_prompt, timeout_s)

    if uploads:
        ledger["status"] = "uploading"
        save_ledger(ledger)
        payload = agents.build_upload_files_payload(list(uploads), text=final_prompt, upload_wait_ms=15000)
        client.upload_files(role, payload, timeout_s)
        readiness = client.wait_upload_ready(
            role,
            request_id=str(ledger["request_id"]),
            expected_attachment_count=len(uploads),
            timeout_s=timeout_s,
        )
        if not readiness.ready:
            raise RoleRequestStateError(outcome_from_upload_readiness(readiness))
        ledger["status"] = "upload_ready"
        save_ledger(ledger)
        ledger["status"] = "sending"
        save_ledger(ledger)
        response = client.send_current_prompt_and_wait(role, timeout_s)
        ledger["status"] = "sent"
        save_ledger(ledger)
        return response

    client.set_prompt(role, final_prompt, timeout_s)
    ledger["status"] = "prompt_set"
    save_ledger(ledger)
    ledger["status"] = "sending"
    save_ledger(ledger)
    response = client.send_current_prompt_and_wait(role, timeout_s)
    ledger["status"] = "sent"
    save_ledger(ledger)
    return response


def main(argv: list[str] | None = None) -> int:
    configure_stdio_utf8()
    args = parse_args(argv)
    run_id = make_run_id()
    role = normalize_role(args.role)
    source_role = normalize_role(args.resp_from)
    prompt = read_prompt(args)
    if not role:
        emit_json(fail_payload(exit_code=2, status="failed_final", request_id="", run_id=run_id, role="", error="--role is required"))
        return 2
    if not prompt:
        emit_json(fail_payload(exit_code=2, status="failed_final", request_id=f"req_{role}_{short_hash('missing_prompt', 8)}", run_id=run_id, role=role, error="--prompt or stdin prompt text is required"))
        return 2

    upload_paths, upload_errors = validate_upload_paths(list(args.upload or []))
    raw_uploads_for_key = [{"path": str(path), "sha256": sha256_file(path), "size": path.stat().st_size} for path in upload_paths]
    context_rendered = render_direct_role_prompt(
        role=role,
        user_prompt="USER_PROMPT_PLACEHOLDER",
        request_id="REQUEST_ID_PLACEHOLDER",
        request_marker=REQUEST_MARKER,
    )
    role_context_hash = sha256_text(rendered_hash_source(context_rendered))
    source_responses_for_key: list[str] = []
    client: BridgeClient | None = None
    if source_role and not args.request_id:
        try:
            with redirect_stdout(sys.stderr):
                client = BridgeClient(args.base_url, args.request_timeout)
                source_responses_for_key = fetch_source_responses(client, source_role)
        except Exception as exc:
            request_id_for_fail = make_request_id(role, idempotency_key(role, prompt, source_role, role_context_hash, raw_uploads_for_key))
            emit_json(fail_payload(exit_code=3, status="failed_retryable", request_id=request_id_for_fail, run_id=run_id, role=role, error=exc, message=f"failed to read responses from {source_role}"))
            return 3
    base_prompt_for_key = build_prompt(prompt, source_role, source_responses_for_key)
    key = idempotency_key(role, base_prompt_for_key, source_role, role_context_hash, raw_uploads_for_key)
    request_id_for_fail = args.request_id or make_request_id(role, key)
    if upload_errors:
        emit_json(fail_payload(exit_code=2, status="failed_final", request_id=request_id_for_fail, run_id=run_id, role=role, error="; ".join(upload_errors)))
        return 2

    upload_meta = upload_metadata(upload_paths)
    ledger = init_or_load_ledger(
        explicit_request_id=str(args.request_id or "").strip(),
        force_new=bool(args.new_request),
        role=role,
        key=key,
        prompt_hash=sha256_text(prompt),
        role_prompt_hash=role_context_hash,
        uploads=upload_meta,
    )
    request_id = str(ledger["request_id"])

    existing_response_path = Path(str(ledger.get("response_path") or response_path(request_id)))
    if ledger.get("status") == "completed" and existing_response_path.exists():
        emit_json(success_payload(request_id=request_id, run_id=run_id, role=role, response_file=existing_response_path, uploaded=len(upload_paths), source_role=source_role, source_response_count=len(source_responses_for_key) if source_role else 0, recovered=True))
        return 0

    recovered = False
    source_responses: list[str] = []
    try:
        with redirect_stdout(sys.stderr):
            if client is None:
                client = BridgeClient(args.base_url, args.request_timeout)
            if str(ledger.get("status") or "") in RECOVERY_LEDGER_STATUSES:
                recovery = recover_existing_response(
                    client,
                    role,
                    request_id,
                    args.timeout,
                    expected_attachment_count=len(upload_paths),
                    ledger_status=str(ledger.get("status") or ""),
                    allow_new_chat=not args.new_chat,
                )
                if recovery.response:
                    response = recovery.response
                    recovered = True
                elif recovery.send_allowed and recovery.action == "safe_click_send":
                    ledger["status"] = "sending"
                    save_ledger(ledger)
                    response = client.send_current_prompt_and_wait(role, args.timeout)
                    ledger["status"] = "sent"
                    save_ledger(ledger)
                    recovered = True
                else:
                    raise RoleRequestStateError(recovery)

            if not recovered:
                run_pre_send_actions(client, role, restart=args.restart, new_chat=args.new_chat, timeout_s=args.timeout)
                source_responses = source_responses_for_key if source_role and not args.request_id else (fetch_source_responses(client, source_role) if source_role else [])
                user_prompt = build_prompt(prompt, source_role, source_responses)
                rendered = render_direct_role_prompt(
                    role=role,
                    user_prompt=user_prompt,
                    request_id=request_id,
                    request_marker=REQUEST_MARKER,
                )
                final_prompt, final_uploads = maybe_spill_prompt(request_id, rendered.text, upload_paths)
                response = run_role_request(client, role, final_prompt, final_uploads, ledger, args.timeout)
    except RoleRequestStateError as exc:
        ledger["status"] = "failed_retryable"
        save_ledger(ledger)
        exit_code = 6 if exc.outcome.status == "manual_input_pending" else 5 if exc.outcome.status == "role_unhealthy" else 4
        emit_json(recovery_payload(exit_code=exit_code, request_id=request_id, run_id=run_id, role=role, outcome=exc.outcome))
        return exit_code
    except ManualInputPendingError as exc:
        ledger["status"] = "failed_retryable"
        save_ledger(ledger)
        emit_json(recovery_payload(
            exit_code=6,
            request_id=request_id,
            run_id=run_id,
            role=role,
            outcome=RecoveryOutcome(
                response="",
                status="manual_input_pending",
                state="manual_composer_dirty",
                action="manual_clear_required",
                recoverable=False,
                role_health="healthy",
                composer=empty_composer_state(len(upload_paths)),
            ),
        ))
        return 6
    except Exception as exc:
        ledger["status"] = "failed_retryable"
        save_ledger(ledger)
        emit_json(fail_payload(exit_code=3, status="failed_retryable", request_id=request_id, run_id=run_id, role=role, error=exc, message=f"runtime failed for {role}"))
        return 3

    response = str(response or "").strip()
    if response:
        path = save_response(request_id, response)
        ledger["status"] = "completed"
        ledger["response_path"] = str(path)
        save_ledger(ledger)
        emit_json(success_payload(request_id=request_id, run_id=run_id, role=role, response_file=path, uploaded=len(upload_paths), source_role=source_role, source_response_count=len(source_responses) if source_role else 0, recovered=recovered))
        return 0
    ledger["status"] = "failed_retryable"
    save_ledger(ledger)
    emit_json(fail_payload(exit_code=3, status="failed_retryable", request_id=request_id, run_id=run_id, role=role, error="empty response", message=f"empty response from {role}"))
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
