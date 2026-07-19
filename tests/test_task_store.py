from __future__ import annotations

import builtins
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from apps.task_store import (
    TaskConflictError,
    TaskStore,
    TaskStoreMutationError,
    normalize_schedule,
)


def payload(**overrides):
    data = {
        "title": "Phase 05",
        "target_root": r"E:\repo",
        "branch": "feat/x",
        "prompt": "Implement the authorized objective.",
        "skill_path": "skills/ORCHESTRATOR.md",
        "controller_role": "control_a",
        "logical_roles": ["dev", "review", "plan"],
        "physical_role_map": {"dev": "worker_1", "review": "worker_1", "plan": "worker_1"},
        "finish_roles": ["plan"],
        "status": "BACKLOG",
        "enabled": True,
        "schedule": {"kind": "manual"},
    }
    data.update(overrides)
    return data


def test_empty_create_atomic_restart_and_deep_copy(tmp_path: Path):
    path = tmp_path / ".role_state" / "tasks.json"
    store = TaskStore(path)
    assert store.read()["revision"] == 0
    task = store.create(payload())
    assert path.exists()
    assert task["revision"] == 1
    assert store.read()["revision"] == 1
    assert not list(path.parent.glob("*.tmp"))

    projected = store.get(task["task_id"])
    projected["title"] = "mutated"
    assert store.get(task["task_id"])["title"] == "Phase 05"
    assert TaskStore(path).get(task["task_id"])["controller_role"] == "CONTROL_A"


def test_existing_corruption_preserves_bytes_and_blocks_mutation(tmp_path: Path):
    path = tmp_path / "tasks.json"
    original = b'{"version":1,"tasks":'
    path.write_bytes(original)
    store = TaskStore(path)
    assert store.load_error["code"] == "invalid_task_file"
    assert store.read()["tasks"] == []
    with pytest.raises(TaskStoreMutationError):
        store.create(payload())
    assert path.read_bytes() == original

    extra_path = tmp_path / "extra.json"
    extra_bytes = json.dumps({"version": 1, "revision": 0, "updated_at": "", "tasks": {}, "unexpected": True}).encode()
    extra_path.write_bytes(extra_bytes)
    extra = TaskStore(extra_path)
    assert extra.load_error and "unsupported fields" in extra.load_error["message"]
    with pytest.raises(TaskStoreMutationError):
        extra.create(payload())
    assert extra_path.read_bytes() == extra_bytes


def test_unicode_and_read_errors_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "tasks.json"
    path.write_bytes(b"\xff\xfe")
    store = TaskStore(path)
    assert store.load_error
    assert path.read_bytes() == b"\xff\xfe"

    valid_path = tmp_path / "valid.json"
    valid_path.write_text(json.dumps({"version": 1, "revision": 0, "updated_at": "", "tasks": {}}), encoding="utf-8")
    original = Path.read_text

    def fail_read(self, *args, **kwargs):
        if self == valid_path:
            raise PermissionError("denied")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_read)
    denied = TaskStore(valid_path)
    assert denied.load_error and "denied" in denied.load_error["message"]
    with pytest.raises(TaskStoreMutationError):
        denied.create(payload())


def test_failed_replace_leaves_memory_and_disk_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "tasks.json"
    store = TaskStore(path)
    task = store.create(payload())
    before_bytes = path.read_bytes()
    before = store.document

    import apps.task_store as module

    def fail_replace(src, dst):
        raise OSError("replace denied")

    monkeypatch.setattr(module.os, "replace", fail_replace)
    with pytest.raises(TaskStoreMutationError):
        store.patch(task["task_id"], task["revision"], {"title": "new"})
    assert path.read_bytes() == before_bytes
    assert store.document == before


def test_revisions_stale_conflict_and_no_write(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.json")
    task = store.create(payload())
    changed = store.patch(task["task_id"], 1, {"title": "Changed"})
    assert changed["revision"] == 2
    assert store.read()["revision"] == 2
    snapshot = store.document
    with pytest.raises(TaskConflictError, match="stale_revision"):
        store.patch(task["task_id"], 1, {"title": "stale"})
    assert store.document == snapshot


@pytest.mark.parametrize(
    ("source", "destination"),
    [
        ("BACKLOG", "READY"), ("BACKLOG", "BLOCKED"),
        ("READY", "BACKLOG"), ("READY", "RUNNING"), ("READY", "BLOCKED"),
        ("RUNNING", "REVIEW"), ("RUNNING", "BLOCKED"), ("RUNNING", "DONE"),
        ("REVIEW", "RUNNING"), ("REVIEW", "BLOCKED"), ("REVIEW", "DONE"),
        ("BLOCKED", "BACKLOG"), ("BLOCKED", "READY"), ("BLOCKED", "RUNNING"),
        ("BLOCKED", "REVIEW"), ("BLOCKED", "DONE"),
        ("DONE", "BACKLOG"), ("DONE", "READY"),
    ],
)
def test_allowed_transitions(tmp_path: Path, source: str, destination: str):
    store = TaskStore(tmp_path / f"{source}-{destination}.json")
    task = store.create(payload(status=source))
    actor = "CONTROL_A" if destination in {"RUNNING", "REVIEW", "DONE"} else ""
    moved = store.move(task["task_id"], task["revision"], destination, actor_role=actor)
    assert moved["status"] == destination


def test_rejected_transitions_and_exact_controller_claim(tmp_path: Path):
    allowed = {
        "BACKLOG": {"READY", "BLOCKED"},
        "READY": {"BACKLOG", "RUNNING", "BLOCKED"},
        "RUNNING": {"REVIEW", "BLOCKED", "DONE"},
        "REVIEW": {"RUNNING", "BLOCKED", "DONE"},
        "BLOCKED": {"BACKLOG", "READY", "RUNNING", "REVIEW", "DONE"},
        "DONE": {"BACKLOG", "READY"},
    }
    for source in allowed:
        for destination in allowed:
            if destination == source or destination in allowed[source]:
                continue
            store = TaskStore(tmp_path / f"reject-{source}-{destination}.json")
            task = store.create(payload(status=source))
            with pytest.raises(TaskConflictError, match="invalid_state_transition"):
                store.move(task["task_id"], task["revision"], destination, actor_role="CONTROL_A")

    store = TaskStore(tmp_path / "claim.json")
    task = store.create(payload(status="BACKLOG"))
    ready = store.move(task["task_id"], 1, "READY")
    with pytest.raises(TaskConflictError, match="controller_mismatch"):
        store.move(task["task_id"], ready["revision"], "RUNNING", actor_role="OTHER")
    running = store.move(task["task_id"], ready["revision"], "RUNNING", actor_role="CONTROL_A")
    assert running["status"] == "RUNNING"


def test_archive_reopen_generic_roles_and_role_map_validation(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.json")
    task = store.create(payload(controller_role="ops_a", logical_roles=["dev1", "review2"], physical_role_map={"dev1": "c-9", "review2": "box_x"}, finish_roles=["review2"], status="DONE"))
    archived = store.patch(task["task_id"], 1, {"archived": True})
    assert archived["archived_at"]
    assert store.list_tasks() == []
    assert store.list_tasks(include_archived=True)[0]["controller_role"] == "OPS_A"
    with pytest.raises(TaskConflictError, match="archived"):
        store.move(task["task_id"], archived["revision"], "READY")
    with pytest.raises(TaskConflictError, match="archived"):
        store.patch(task["task_id"], archived["revision"], {"title": "nope"})

    with pytest.raises(ValueError, match="keys"):
        store.create(payload(physical_role_map={"dev": "W"}))
    with pytest.raises(ValueError, match="subset"):
        store.create(payload(finish_roles=["audit"]))


def test_duplicate_controller_reservations_fail_closed_on_restart(tmp_path: Path):
    path = tmp_path / "tasks.json"
    store = TaskStore(path)
    first = store.create(payload(title="first", status="RUNNING"))
    second = store.create(payload(title="second", controller_role="other", status="RUNNING"))
    document = store.document
    document["tasks"][second["task_id"]]["controller_role"] = first["controller_role"]
    original = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    path.write_bytes(original)
    restarted = TaskStore(path)
    assert restarted.load_error and "reserved by both" in restarted.load_error["message"]
    with pytest.raises(TaskStoreMutationError):
        restarted.create(payload(title="blocked"))
    assert path.read_bytes() == original


def test_controller_reservation_across_status_request_and_wake(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.json")
    first = store.create(payload(title="one", status="RUNNING"))
    second = store.create(payload(title="two", status="READY"))
    with pytest.raises(TaskConflictError, match="controller_busy"):
        store.move(second["task_id"], second["revision"], "RUNNING", actor_role="CONTROL_A")
    with pytest.raises(TaskConflictError, match="controller_busy"):
        store.patch(second["task_id"], second["revision"], {"active_request_id": "req-second"}, actor_role="CONTROL_A")

    finished = store.move(first["task_id"], first["revision"], "DONE", actor_role="CONTROL_A")
    assert finished["active_request_id"] is None
    claimed = store.claim_wake(second["task_id"], second["revision"], source="manual", scheduled_for=None, server_instance_id="server-1")
    third = store.create(payload(title="three", status="READY"))
    with pytest.raises(TaskConflictError, match="controller_busy"):
        store.claim_wake(third["task_id"], third["revision"], source="manual", scheduled_for=None, server_instance_id="server-1")
    assert claimed["wake"]["state"] == "CLAIMED"


def test_schedule_validation_and_utc_normalization():
    now = datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc)
    once, next_run = normalize_schedule({"kind": "once", "run_at": "2026-07-20T09:00:00+09:00"}, now)
    assert once["run_at"] == "2026-07-20T00:00:00Z"
    assert next_run == "2026-07-20T00:00:00Z"
    interval, next_run = normalize_schedule({"kind": "interval", "minutes": 15, "start_at": "2026-07-20T09:00:00+09:00"}, now)
    assert interval["minutes"] == 15 and next_run == "2026-07-20T00:00:00Z"
    cron, next_run = normalize_schedule({"kind": "cron", "expression": "0 9 * * 1-5", "timezone": "Asia/Tokyo"}, now)
    assert cron["timezone"] == "Asia/Tokyo" and next_run
    with pytest.raises(ValueError):
        normalize_schedule({"kind": "interval", "minutes": 0}, now)
    with pytest.raises(ValueError):
        normalize_schedule({"kind": "cron", "expression": "0 0 9 * * *", "timezone": "Asia/Tokyo"}, now)
    with pytest.raises(ValueError):
        normalize_schedule({"kind": "cron", "expression": "0 9 * * *", "timezone": "Mars/Base"}, now)


def test_resume_skips_missed_interval_and_disables_expired_once(tmp_path: Path):
    class Clock:
        value = datetime(2026, 7, 19, 1, 7, tzinfo=timezone.utc)

        def __call__(self):
            return self.value

    clock = Clock()
    store = TaskStore(tmp_path / "tasks.json", clock=clock)
    interval = store.create(payload(schedule={"kind": "interval", "minutes": 15, "start_at": "2026-07-19T00:00:00Z"}, enabled=False))
    resumed = store.resume(interval["task_id"], interval["revision"])
    assert resumed["next_run_at"] == "2026-07-19T01:15:00Z"

    once = store.create(payload(title="once", controller_role="other", schedule={"kind": "once", "run_at": "2026-07-19T00:30:00Z"}, enabled=False))
    expired = store.resume(once["task_id"], once["revision"])
    assert expired["enabled"] is False
    assert expired["next_run_at"] is None


@pytest.mark.parametrize(
    ("pending_state", "command_id", "command_action"),
    [
        ("SET_PROMPT_PENDING", "cmd-set-1", "SET_PROMPT"),
        ("CLICK_SEND_PENDING", "cmd-send-1", "CLICK_SEND"),
    ],
)
def test_uncertain_transition_preserves_exact_command_provenance(
    tmp_path: Path,
    pending_state: str,
    command_id: str,
    command_action: str,
):
    store = TaskStore(tmp_path / f"{pending_state}.json")
    task = store.create(payload(status="READY"))
    claimed = store.claim_wake(
        task["task_id"],
        task["revision"],
        source="manual",
        scheduled_for=None,
        server_instance_id="server-old",
    )
    pending = store.update_wake(
        claimed["task_id"],
        claimed["revision"],
        state=pending_state,
        command_id=command_id,
        command_action=command_action,
    )

    uncertain = store.update_wake(
        pending["task_id"],
        pending["revision"],
        state="UNCERTAIN",
        error="ambiguous outcome",
        blocker="ambiguous outcome",
    )

    assert uncertain["wake"]["state"] == "UNCERTAIN"
    assert uncertain["wake"]["command_id"] == command_id
    assert uncertain["wake"]["command_action"] == command_action
    assert uncertain["events"][-1]["command_id"] == command_id


def test_bounded_events_and_uncertain_resolution(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks.json")
    task = store.create(payload(status="READY"))
    current = store.claim_wake(task["task_id"], 1, source="manual", scheduled_for=None, server_instance_id="old")
    current = store.update_wake(current["task_id"], current["revision"], state="UNCERTAIN", error="restart")
    with pytest.raises(TaskConflictError, match="duplicate_or_uncertain_wake"):
        store.claim_wake(current["task_id"], current["revision"], source="manual", scheduled_for=None, server_instance_id="new")
    resolved = store.patch(current["task_id"], current["revision"], {}, actor_role="CONTROL_A", wake_resolution="not_sent")
    assert resolved["wake"]["state"] == "IDLE"
    for index in range(60):
        resolved = store.patch(resolved["task_id"], resolved["revision"], {"last_result_summary": f"result {index}"}, actor_role="CONTROL_A")
    assert len(resolved["events"]) <= 50
    assert "transcript" not in json.dumps(resolved).lower()
