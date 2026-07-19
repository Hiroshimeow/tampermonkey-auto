from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import time

import pytest

from apps.task_scheduler import TaskScheduler, build_wake_prompt
from apps.task_store import TaskStore, TaskStoreMutationError, next_after_success, normalize_schedule


class Clock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, **kwargs):
        self.value += timedelta(**kwargs)


class Transport:
    def __init__(self):
        self.commands = []
        self.results = {}
        self.snapshots = {}
        self.before_create = None

    def readiness(self, role: str):
        return self.snapshots.get(role, {
            "online": True,
            "active_command": None,
            "composer": True,
            "stop_visible": False,
            "composer_text": "",
            "attachment_count": 0,
            "manual_input": False,
        })

    def create(self, role: str, action: str, payload: dict, command_id: str = ""):
        resolved_command_id = command_id or f"cmd-{len(self.commands)+1}"
        if self.before_create:
            self.before_create(role, action, payload, resolved_command_id)
        command = {"command_id": resolved_command_id, "role": role, "action": action, "payload": payload}
        self.commands.append(command)
        return command

    def result(self, command_id: str):
        return self.results.get(command_id)


def task_payload(**overrides):
    data = {
        "title": "Wake arbitrary controller",
        "target_root": r"E:\target",
        "branch": "feat/task",
        "prompt": "Inspect current evidence and execute only the authorized task.",
        "skill_path": "skills/ORCHESTRATOR.md",
        "controller_role": "control-x9",
        "logical_roles": ["dev1", "review2", "plan_z"],
        "physical_role_map": {"dev1": "worker-a", "review2": "worker-b", "plan_z": "worker-a"},
        "finish_roles": ["plan_z"],
        "status": "READY",
        "schedule": {"kind": "manual"},
    }
    data.update(overrides)
    return data


def scheduler(tmp_path: Path, clock: Clock, transport: Transport):
    store = TaskStore(tmp_path / "tasks.json", clock=clock)
    worker = TaskScheduler(
        store=store,
        server_instance_id="server-current",
        readiness=transport.readiness,
        create_command=transport.create,
        command_result=transport.result,
        clock=clock,
        poll_interval_s=0.01,
        retry_delay_s=30,
    )
    return store, worker


def complete_wake(store: TaskStore, worker: TaskScheduler, transport: Transport, role: str = "CONTROL-X9"):
    worker.tick()
    set_command = transport.commands[-1]
    assert set_command["action"] == "SET_PROMPT"
    prompt = set_command["payload"]["text"]
    current = store.list_tasks(include_archived=True)[0]
    assert current["wake"]["state"] == "SET_PROMPT_PENDING"
    assert current["wake"]["command_id"] == set_command["command_id"]
    transport.results[set_command["command_id"]] = {"state": "PASTE_CONFIRMED"}
    transport.snapshots[role] = {
        "online": True,
        "active_command": None,
        "composer": True,
        "stop_visible": False,
        "composer_text": prompt,
        "attachment_count": 0,
        "manual_input": False,
    }
    worker.tick()
    click = transport.commands[-1]
    assert click["action"] == "CLICK_SEND"
    current = store.list_tasks(include_archived=True)[0]
    assert current["wake"]["state"] == "CLICK_SEND_PENDING"
    assert current["wake"]["command_id"] == click["command_id"]
    worker.tick()
    assert transport.commands[-1] == click
    transport.results[click["command_id"]] = {"state": "SEND_ACCEPTED"}
    worker.tick()
    return store.list_tasks(include_archived=True)[0]


def test_schedule_math_manual_once_interval_cron_and_coalescing():
    now = datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc)
    manual, next_run = normalize_schedule({"kind": "manual"}, now)
    assert next_run is None
    once, next_run = normalize_schedule({"kind": "once", "run_at": "2026-07-20T09:00:00+09:00"}, now)
    assert next_after_success(once, now) == (False, None)
    interval, _ = normalize_schedule({"kind": "interval", "minutes": 15, "start_at": "2026-07-19T00:00:00Z"}, now)
    assert next_after_success(interval, now + timedelta(minutes=63))[1] == "2026-07-19T01:15:00Z"
    cron, next_cron = normalize_schedule({"kind": "cron", "expression": "0 9 * * 1-5", "timezone": "Asia/Tokyo"}, now)
    assert next_cron == "2026-07-20T00:00:00Z"
    assert next_after_success(cron, datetime(2026, 7, 20, 1, tzinfo=timezone.utc))[1] == "2026-07-21T00:00:00Z"


def test_due_order_and_one_controller_reservation(tmp_path: Path):
    clock = Clock(datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc))
    store = TaskStore(tmp_path / "tasks.json", clock=clock)
    later = store.create(task_payload(title="later", schedule={"kind": "once", "run_at": "2026-07-19T00:02:00Z"}))
    first = store.create(task_payload(title="first", controller_role="another", schedule={"kind": "once", "run_at": "2026-07-19T00:01:00Z"}))
    clock.advance(minutes=3)
    assert [task["task_id"] for task in store.due_tasks(clock())] == [first["task_id"], later["task_id"]]


def test_offline_busy_active_dirty_manual_and_attachment_defer_without_command(tmp_path: Path):
    reasons = [
        ({"online": False}, "controller_offline"),
        ({"online": True, "active_command": {"command_id": "busy"}}, "controller_busy"),
        ({"online": True, "active_command": None, "composer": True, "stop_visible": True}, "assistant_active"),
        ({"online": True, "active_command": None, "composer": True, "stop_visible": False, "composer_text": "manual"}, "composer_dirty"),
        ({"online": True, "active_command": None, "composer": True, "stop_visible": False, "composer_text": "", "manual_input": True}, "manual_input"),
        ({"online": True, "active_command": None, "composer": True, "stop_visible": False, "composer_text": "", "manual_input": False, "attachment_count": 1}, "attachment_pending"),
    ]
    for index, (snapshot, reason) in enumerate(reasons):
        case = tmp_path / str(index)
        clock = Clock(datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc))
        transport = Transport()
        transport.snapshots["CONTROL-X9"] = snapshot
        store, worker = scheduler(case, clock, transport)
        task = store.create(task_payload())
        claimed = worker.request_manual(task["task_id"], task["revision"])
        worker.tick()
        current = store.get(task["task_id"])
        assert current["wake"]["state"] == "DEFERRED"
        assert current["wake"]["error"] == reason
        assert current["wake"]["scheduled_for"] == claimed["wake"]["scheduled_for"]
        assert transport.commands == []


def test_exact_prompt_and_set_then_single_click_payloads(tmp_path: Path):
    clock = Clock(datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc))
    transport = Transport()
    store, worker = scheduler(tmp_path, clock, transport)
    task = store.create(task_payload())
    observed_issuing = []

    def assert_durable_linkage(role, action, payload, command_id):
        current = store.get(task["task_id"])
        expected_state = "ISSUING_SET_PROMPT" if action == "SET_PROMPT" else "ISSUING_CLICK_SEND"
        assert role == "CONTROL-X9"
        assert current["wake"]["state"] == expected_state
        assert current["wake"]["command_id"] == command_id
        assert current["wake"]["command_action"] == action
        observed_issuing.append((action, command_id))

    transport.before_create = assert_durable_linkage
    worker.request_manual(task["task_id"], task["revision"])

    worker.tick()
    assert len(transport.commands) == 1
    pending = store.get(task["task_id"])
    assert pending["wake"]["state"] == "SET_PROMPT_PENDING"
    set_command = transport.commands[0]
    assert pending["wake"]["command_id"] == set_command["command_id"]
    assert set_command["role"] == "CONTROL-X9"
    assert set_command["action"] == "SET_PROMPT"
    prompt = set_command["payload"]["text"]
    assert set_command["payload"] == {"text": prompt, "method": "auto", "expected_text": prompt}
    for expected in [task["task_id"], "CONTROL-X9", "skills/ORCHESTRATOR.md", r"E:\target", "feat/task", "DEV1", "WORKER-A", "PLAN_Z", "authorized task", "GET the exact task", "claim"]:
        assert expected in prompt
    assert "Wake snapshot/audit revision:" in prompt
    assert "scheduler wake stages may advance the task revision" in prompt
    assert "fresh GET" in prompt
    assert "Expected task revision:" not in prompt
    assert "uv run" not in prompt and "powershell" not in prompt.lower()

    transport.results[set_command["command_id"]] = {"state": "PASTE_CONFIRMED"}
    transport.snapshots["CONTROL-X9"] = {
        "online": True,
        "active_command": None,
        "composer": True,
        "stop_visible": False,
        "composer_text": prompt,
        "attachment_count": 0,
        "manual_input": False,
    }
    worker.tick()
    assert len(transport.commands) == 2
    click_pending = store.get(task["task_id"])
    assert click_pending["wake"]["state"] == "CLICK_SEND_PENDING"
    click = transport.commands[1]
    assert click_pending["wake"]["command_id"] == click["command_id"]
    assert click["role"] == "CONTROL-X9"
    assert click["action"] == "CLICK_SEND"
    assert click["payload"] == {"expected_text": prompt}
    worker.tick()
    assert len(transport.commands) == 2

    transport.results[click["command_id"]] = {"state": "SEND_ACCEPTED"}
    worker.tick()
    assert store.get(task["task_id"])["wake"]["state"] == "SENT"
    assert [action for action, _ in observed_issuing] == ["SET_PROMPT", "CLICK_SEND"]
    assert observed_issuing[0][1] != observed_issuing[1][1]


def test_set_prompt_acceptance_then_pending_persist_failure_is_uncertain_and_not_replayed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    clock = Clock(datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc))
    transport = Transport()
    store, worker = scheduler(tmp_path, clock, transport)
    task = store.create(task_payload())
    original_update_wake = store.update_wake
    failed_pending_persist = False

    def fail_pending_persist(task_id, expected_revision, **kwargs):
        nonlocal failed_pending_persist
        if kwargs.get("state") == "SET_PROMPT_PENDING" and not failed_pending_persist:
            failed_pending_persist = True
            raise TaskStoreMutationError("simulated pending persistence failure")
        return original_update_wake(task_id, expected_revision, **kwargs)

    monkeypatch.setattr(store, "update_wake", fail_pending_persist)
    worker.request_manual(task["task_id"], task["revision"])
    worker.tick()

    assert len(transport.commands) == 1
    accepted = transport.commands[0]
    current = store.get(task["task_id"])
    assert current["wake"]["state"] == "UNCERTAIN"
    assert current["wake"]["command_id"] == accepted["command_id"]
    assert current["wake"]["command_action"] == "SET_PROMPT"
    assert current["blocker"]

    worker.tick()
    assert len(transport.commands) == 1


def test_set_prompt_precreate_failure_with_clean_composer_defers_and_clears_linkage(
    tmp_path: Path,
):
    clock = Clock(datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc))
    transport = Transport()
    store, worker = scheduler(tmp_path, clock, transport)
    task = store.create(task_payload())

    def reject_before_create(role, action, payload, command_id):
        raise RuntimeError("transport rejected before create")

    transport.before_create = reject_before_create
    worker.request_manual(task["task_id"], task["revision"])
    worker.tick()

    current = store.get(task["task_id"])
    assert transport.commands == []
    assert current["wake"]["state"] == "DEFERRED"
    assert current["wake"]["command_id"] is None
    assert current["wake"]["command_action"] is None


def test_command_failure_dirty_composer_becomes_uncertain(tmp_path: Path):
    clock = Clock(datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc))
    transport = Transport()
    store, worker = scheduler(tmp_path, clock, transport)
    task = store.create(task_payload())
    worker.request_manual(task["task_id"], task["revision"])
    worker.tick()
    command = transport.commands[0]
    transport.results[command["command_id"]] = {"state": "PASTE_FAILED"}
    transport.snapshots["CONTROL-X9"] = {"online": True, "active_command": None, "composer": True, "stop_visible": False, "composer_text": "partial", "attachment_count": 0, "manual_input": False}
    worker.tick()
    current = store.get(task["task_id"])
    assert current["wake"]["state"] == "UNCERTAIN"
    assert current["blocker"]


def test_restart_uncertainty_is_never_replayed(tmp_path: Path):
    clock = Clock(datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc))
    transport = Transport()
    store, worker = scheduler(tmp_path, clock, transport)
    task = store.create(task_payload())
    claimed = store.claim_wake(task["task_id"], task["revision"], source="manual", scheduled_for=None, server_instance_id="old-server")
    pending = store.update_wake(claimed["task_id"], claimed["revision"], state="SET_PROMPT_PENDING", command_id="lost", command_action="SET_PROMPT")

    restarted = TaskStore(tmp_path / "tasks.json", clock=clock)
    recovered = restarted.recover_for_server("new-server")
    assert recovered[0]["wake"]["state"] == "UNCERTAIN"
    assert recovered[0]["wake"]["command_id"] == "lost"
    assert recovered[0]["wake"]["command_action"] == "SET_PROMPT"
    new_transport = Transport()
    new_worker = TaskScheduler(store=restarted, server_instance_id="new-server", readiness=new_transport.readiness, create_command=new_transport.create, command_result=new_transport.result, clock=clock, poll_interval_s=0.01)
    new_worker.tick()
    assert new_transport.commands == []


def test_scheduled_once_success_disables_and_interval_missed_run_coalesces(tmp_path: Path):
    once_clock = Clock(datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc))
    once_transport = Transport()
    once_store, once_worker = scheduler(tmp_path / "once", once_clock, once_transport)
    once_task = once_store.create(task_payload(schedule={"kind": "once", "run_at": "2026-07-19T00:00:00Z"}))
    once_done = complete_wake(once_store, once_worker, once_transport)
    assert once_done["task_id"] == once_task["task_id"]
    assert once_done["wake"]["source"] == "scheduled"
    assert once_done["wake"]["scheduled_for"] == "2026-07-19T00:00:00Z"
    assert once_done["wake"]["state"] == "SENT"
    assert once_done["status"] == "READY"
    assert once_done["enabled"] is False
    assert once_done["next_run_at"] is None

    interval_clock = Clock(datetime(2026, 7, 19, 1, 7, tzinfo=timezone.utc))
    interval_transport = Transport()
    interval_store, interval_worker = scheduler(tmp_path / "interval", interval_clock, interval_transport)
    interval_store.create(task_payload(schedule={"kind": "interval", "minutes": 15, "start_at": "2026-07-19T00:00:00Z"}))
    interval_done = complete_wake(interval_store, interval_worker, interval_transport)
    assert interval_done["wake"]["scheduled_for"] == "2026-07-19T00:00:00Z"
    assert interval_done["next_run_at"] == "2026-07-19T01:15:00Z"
    assert len(interval_transport.commands) == 2


def test_same_controller_due_tasks_issue_only_first_occurrence(tmp_path: Path):
    clock = Clock(datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc))
    transport = Transport()
    store, worker = scheduler(tmp_path, clock, transport)
    first = store.create(task_payload(title="first", schedule={"kind": "once", "run_at": "2026-07-19T00:00:00Z"}))
    clock.advance(microseconds=1)
    second = store.create(task_payload(title="second", schedule={"kind": "once", "run_at": "2026-07-19T00:00:00Z"}))
    worker.tick()
    assert len(transport.commands) == 1
    assert first["task_id"] in transport.commands[0]["payload"]["text"]
    assert store.get(first["task_id"])["wake"]["state"] == "SET_PROMPT_PENDING"
    assert store.get(second["task_id"])["wake"]["state"] == "IDLE"
    assert store.get(second["task_id"])["next_run_at"] == "2026-07-19T00:00:00Z"


def test_deferred_retry_preserves_occurrence_and_rechecks_controller_reservation(tmp_path: Path):
    clock = Clock(datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc))
    transport = Transport()
    transport.snapshots["CONTROL-X9"] = {"online": False}
    store, worker = scheduler(tmp_path, clock, transport)
    task = store.create(task_payload())
    claimed = worker.request_manual(task["task_id"], task["revision"])
    worker.tick()
    deferred = store.get(task["task_id"])
    attempt_id = deferred["wake"]["attempt_id"]
    scheduled_for = deferred["wake"]["scheduled_for"]
    deferral_events = [event for event in deferred["events"] if event["type"] == "wake_deferred"]

    active = store.create(task_payload(title="active", status="RUNNING"))
    transport.snapshots["CONTROL-X9"] = {
        "online": True, "active_command": None, "composer": True, "stop_visible": False,
        "composer_text": "", "attachment_count": 0, "manual_input": False,
    }
    clock.advance(seconds=31)
    worker.tick()
    still_deferred = store.get(task["task_id"])
    assert still_deferred["wake"]["state"] == "DEFERRED"
    assert still_deferred["wake"]["attempt_id"] == attempt_id
    assert still_deferred["wake"]["scheduled_for"] == scheduled_for
    assert still_deferred["wake"]["error"] == "controller_busy"
    assert transport.commands == []

    store.move(active["task_id"], active["revision"], "DONE", actor_role="CONTROL-X9")
    clock.advance(seconds=31)
    worker.tick()
    retried = store.get(task["task_id"])
    assert retried["wake"]["attempt_id"] == attempt_id
    assert retried["wake"]["state"] == "SET_PROMPT_PENDING"
    assert len(transport.commands) == 1
    repeated_offline = [event for event in retried["events"] if event["type"] == "wake_deferred" and event["summary"].endswith("controller_offline")]
    assert len(repeated_offline) == len(deferral_events)


def test_sent_restart_is_not_replayed_and_claim_acknowledges_wake(tmp_path: Path):
    clock = Clock(datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc))
    transport = Transport()
    store, worker = scheduler(tmp_path, clock, transport)
    task = store.create(task_payload())
    worker.request_manual(task["task_id"], task["revision"])
    sent = complete_wake(store, worker, transport)
    assert sent["wake"]["state"] == "SENT"

    restarted_store = TaskStore(tmp_path / "tasks.json", clock=clock)
    restarted_transport = Transport()
    restarted_worker = TaskScheduler(store=restarted_store, server_instance_id="server-new", readiness=restarted_transport.readiness, create_command=restarted_transport.create, command_result=restarted_transport.result, clock=clock, poll_interval_s=0.01)
    restarted_worker.tick()
    assert restarted_transport.commands == []
    current = restarted_store.get(task["task_id"])
    running = restarted_store.move(current["task_id"], current["revision"], "RUNNING", actor_role="CONTROL-X9")
    assert running["wake"]["state"] == "IDLE"
    assert any(event["type"] == "wake_acknowledged" for event in running["events"])


def test_manual_run_does_not_shift_schedule(tmp_path: Path):
    clock = Clock(datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc))
    transport = Transport()
    store, worker = scheduler(tmp_path, clock, transport)
    task = store.create(task_payload(schedule={"kind": "interval", "minutes": 15, "start_at": "2026-07-19T01:00:00Z"}))
    scheduled = task["next_run_at"]
    claimed = worker.request_manual(task["task_id"], task["revision"])
    assert claimed["next_run_at"] == scheduled


def test_start_stop_idempotent_and_health_error_projection(tmp_path: Path):
    clock = Clock(datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc))
    transport = Transport()
    store, worker = scheduler(tmp_path, clock, transport)
    worker.start()
    worker.start()
    assert worker.health()["running"] is True
    assert worker.health()["server_instance_id"] == "server-current"
    original_due_tasks = store.due_tasks

    def fail_due_tasks(_now):
        raise RuntimeError("scheduler boom")

    store.due_tasks = fail_due_tasks
    worker.wake()
    deadline = time.monotonic() + 0.5
    while not worker.health()["last_error"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert "scheduler boom" in worker.health()["last_error"]
    store.due_tasks = original_due_tasks
    worker.wake()
    deadline = time.monotonic() + 0.5
    while worker.health()["last_error"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert worker.health()["last_error"] == ""
    worker.stop()
    worker.stop()
    assert worker.health()["running"] is False
