from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.flow_store import FlowStore, FlowStoreMutationError


def patch(
    store: FlowStore,
    request_id: str = "req-1",
    *,
    run_id: str = "run-1",
    updates: dict | None = None,
    activate: bool = False,
    parent_request_id: str | None = None,
    goal_hash: str | None = None,
    terminal_status: str | None = None,
) -> dict:
    return store.patch(
        request_id=request_id,
        run_id=run_id,
        updates=updates or {},
        activate=activate,
        parent_request_id=parent_request_id,
        goal_hash=goal_hash,
        terminal_status=terminal_status,
    )


def test_missing_file_loads_empty_version_one_document(tmp_path: Path) -> None:
    store = FlowStore(tmp_path / ".role_state" / "flow.json")

    assert store.document == {
        "version": 1,
        "revision": 0,
        "updated_at": "",
        "active_request_id": "",
        "requests": {},
    }
    assert store.load_error is None
    assert store.active_projection() == {}


def test_patch_creates_file_merges_roles_and_preserves_metadata(tmp_path: Path) -> None:
    path = tmp_path / ".role_state" / "flow.json"
    store = FlowStore(path)

    first = patch(
        store,
        activate=True,
        parent_request_id="parent-1",
        goal_hash="a" * 64,
        updates={"TAB1": {"state": "RUNNING", "logical_role": "DEV", "from_role": "USER"}},
    )
    second = patch(store, updates={"TAB2": {"state": "WAITING", "logical_role": "REVIEW"}})

    assert path.exists()
    assert first["revision"] == 1
    assert second["revision"] == 2
    request = second["requests"]["req-1"]
    assert request["request_id"] == "req-1"
    assert request["run_id"] == "run-1"
    assert request["activation_order"] == 1
    assert request["parent_request_id"] == "parent-1"
    assert request["goal_hash"] == "a" * 64
    assert request["roles"]["TAB1"]["logical_role"] == "DEV"
    assert request["roles"]["TAB2"]["state"] == "WAITING"
    assert json.loads(path.read_text(encoding="utf-8")) == second


def test_null_role_removes_only_addressed_role_and_request(tmp_path: Path) -> None:
    store = FlowStore(tmp_path / "flow.json")
    patch(store, activate=True, updates={"TAB1": {"state": "RUNNING"}, "TAB2": {"state": "WAITING"}})
    patch(store, request_id="req-2", run_id="run-2", updates={"TAB1": {"state": "DONE"}})

    document = patch(store, updates={"TAB1": None})

    assert "TAB1" not in document["requests"]["req-1"]["roles"]
    assert document["requests"]["req-1"]["roles"]["TAB2"]["state"] == "WAITING"
    assert document["requests"]["req-2"]["roles"]["TAB1"]["state"] == "DONE"


def test_delayed_nonactivating_patch_cannot_steal_active_request(tmp_path: Path) -> None:
    store = FlowStore(tmp_path / "flow.json")
    patch(store, request_id="old", run_id="old", activate=True, updates={"TAB": {"state": "RUNNING"}})
    patch(store, request_id="new", run_id="new", activate=True, updates={"TAB": {"state": "RUNNING"}})

    document = patch(store, request_id="old", run_id="old", activate=False, updates={"TAB": {"state": "DONE"}})

    assert document["active_request_id"] == "new"
    assert store.active_projection()["TAB"]["run_id"] == "new"


def test_delayed_old_activation_cannot_reclaim_newer_request(tmp_path: Path) -> None:
    store = FlowStore(tmp_path / "flow.json")
    patch(store, request_id="old", run_id="old", activate=True, updates={"TAB": {"state": "RUNNING"}})
    patch(store, request_id="new", run_id="new", activate=True, updates={"TAB": {"state": "RUNNING"}})

    document = patch(store, request_id="old", run_id="old", activate=True, updates={"TAB": {"state": "DONE"}})

    assert document["active_request_id"] == "new"
    assert document["requests"]["old"]["activation_order"] == 1
    assert document["requests"]["new"]["activation_order"] == 2
    assert store.active_projection()["TAB"]["run_id"] == "new"


def test_precreated_inactive_request_gets_first_later_activation(tmp_path: Path) -> None:
    store = FlowStore(tmp_path / "flow.json")
    patch(store, request_id="old", run_id="old", activate=True, updates={"TAB": {"state": "RUNNING"}})
    created = patch(store, request_id="future", run_id="future", activate=False, updates={"TAB": {"state": "WAITING"}})

    assert created["requests"]["future"]["activation_order"] == 0
    assert created["active_request_id"] == "old"

    activated = patch(store, request_id="future", run_id="future", activate=True, updates={"TAB": {"state": "RUNNING"}})

    assert activated["active_request_id"] == "future"
    assert activated["requests"]["future"]["activation_order"] == 2


def test_restart_preserves_activation_order_against_delayed_old_takeover(tmp_path: Path) -> None:
    path = tmp_path / "flow.json"
    store = FlowStore(path)
    patch(store, request_id="old", run_id="old", activate=True, updates={"TAB": {"state": "RUNNING"}})
    patch(store, request_id="new", run_id="new", activate=True, updates={"TAB": {"state": "RUNNING"}})

    reloaded = FlowStore(path)
    document = patch(reloaded, request_id="old", run_id="old", activate=True, updates={"TAB": {"state": "DONE"}})

    assert document["active_request_id"] == "new"
    assert reloaded.active_projection()["TAB"]["run_id"] == "new"


def test_missing_activation_order_is_accepted_and_migrated_stale_safe(tmp_path: Path) -> None:
    path = tmp_path / "flow.json"
    store = FlowStore(path)
    patch(store, request_id="old", run_id="old", activate=True, updates={"TAB": {"state": "RUNNING"}})
    document = json.loads(path.read_text(encoding="utf-8"))
    document["requests"]["old"].pop("activation_order")
    path.write_text(json.dumps(document), encoding="utf-8")

    reloaded = FlowStore(path)

    assert reloaded.load_error is None
    assert reloaded.document["requests"]["old"]["activation_order"] == 0

    patch(reloaded, request_id="new", run_id="new", activate=True, updates={"TAB": {"state": "RUNNING"}})
    delayed = patch(reloaded, request_id="old", run_id="old", activate=True, updates={"TAB": {"state": "DONE"}})

    assert delayed["active_request_id"] == "new"
    assert delayed["requests"]["old"]["activation_order"] == 1
    assert delayed["requests"]["new"]["activation_order"] == 2


def test_activation_order_write_failure_preserves_owner_memory_projection_and_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import apps.flow_store as flow_store_module

    path = tmp_path / "flow.json"
    store = FlowStore(path)
    patch(store, request_id="old", run_id="old", activate=True, updates={"TAB": {"state": "RUNNING"}})
    patch(store, request_id="future", run_id="future", activate=False, updates={"TAB": {"state": "WAITING"}})
    before_bytes = path.read_bytes()
    before_document = store.document
    before_projection = store.active_projection()

    def fail(*_args, **_kwargs):
        raise OSError("simulated activation replace failure")

    monkeypatch.setattr(flow_store_module.os, "replace", fail)

    with pytest.raises(FlowStoreMutationError, match="activation replace failure"):
        patch(store, request_id="future", run_id="future", activate=True, updates={"TAB": {"state": "RUNNING"}})

    assert path.read_bytes() == before_bytes
    assert store.document == before_document
    assert store.document["active_request_id"] == "old"
    assert store.active_projection() == before_projection


def test_finalization_retains_roles_and_sets_terminal_status(tmp_path: Path) -> None:
    store = FlowStore(tmp_path / "flow.json")
    patch(store, activate=True, updates={"TAB": {"state": "DONE", "done_from": "DEV"}})

    document = patch(store, terminal_status="complete")

    request = document["requests"]["req-1"]
    assert request["terminal_status"] == "complete"
    assert request["roles"]["TAB"]["state"] == "DONE"
    assert document["revision"] == 2


def test_temp_write_failure_preserves_prior_file_and_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import apps.flow_store as flow_store_module

    path = tmp_path / "flow.json"
    store = FlowStore(path)
    patch(store, activate=True, updates={"TAB": {"state": "RUNNING"}})
    before_bytes = path.read_bytes()
    before_document = store.document

    def fail(*_args, **_kwargs):
        raise OSError("simulated temp write failure")

    monkeypatch.setattr(flow_store_module.json, "dump", fail)

    with pytest.raises(FlowStoreMutationError, match="temp write failure"):
        patch(store, updates={"TAB": {"state": "DONE"}})

    assert path.read_bytes() == before_bytes
    assert store.document == before_document
    assert store.active_projection()["TAB"]["state"] == "RUNNING"


@pytest.mark.parametrize("failure_name", ["fsync", "replace"])
def test_atomic_failure_preserves_prior_file_and_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_name: str) -> None:
    import apps.flow_store as flow_store_module

    path = tmp_path / "flow.json"
    store = FlowStore(path)
    patch(store, activate=True, updates={"TAB": {"state": "RUNNING"}})
    before_bytes = path.read_bytes()
    before_document = store.document

    def fail(*_args, **_kwargs):
        raise OSError(f"simulated {failure_name} failure")

    monkeypatch.setattr(flow_store_module.os, failure_name, fail)

    with pytest.raises(FlowStoreMutationError, match=failure_name):
        patch(store, updates={"TAB": {"state": "DONE"}})

    assert path.read_bytes() == before_bytes
    assert store.document == before_document
    assert store.active_projection()["TAB"]["state"] == "RUNNING"


def test_fresh_store_hydrates_active_projection_exactly(tmp_path: Path) -> None:
    path = tmp_path / "flow.json"
    first = FlowStore(path)
    patch(
        first,
        activate=True,
        updates={"SHARED": {"state": "RUNNING", "logical_role": "PLAN", "from_role": "REVIEW"}},
    )

    reloaded = FlowStore(path)

    assert reloaded.load_error is None
    assert reloaded.document == first.document
    assert reloaded.active_projection() == {
        "SHARED": {
            "run_id": "run-1",
            "state": "RUNNING",
            "logical_role": "PLAN",
            "from_role": "REVIEW",
        }
    }


def test_corrupt_file_is_untouched_and_blocks_mutation(tmp_path: Path) -> None:
    path = tmp_path / "flow.json"
    corrupt = b'{"version":1,"requests":'
    path.write_bytes(corrupt)

    store = FlowStore(path)

    assert store.load_error
    assert store.document["requests"] == {}
    with pytest.raises(FlowStoreMutationError, match="load_error"):
        patch(store, activate=True, updates={"TAB": {"state": "RUNNING"}})
    assert path.read_bytes() == corrupt


@pytest.mark.parametrize(
    "document",
    [
        [],
        {"version": 2, "revision": 0, "updated_at": "", "active_request_id": "", "requests": {}},
        {"version": 1, "revision": "bad", "updated_at": "", "active_request_id": "", "requests": {}},
        {"version": 1, "revision": 0, "updated_at": "", "active_request_id": "missing", "requests": {}},
    ],
)
def test_invalid_existing_document_fails_closed(tmp_path: Path, document: object) -> None:
    path = tmp_path / "flow.json"
    original = json.dumps(document).encode("utf-8")
    path.write_bytes(original)

    store = FlowStore(path)

    assert store.load_error
    with pytest.raises(FlowStoreMutationError):
        patch(store, updates={"TAB": {"state": "RUNNING"}})
    assert path.read_bytes() == original


def test_invalid_timestamp_in_existing_document_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "flow.json"
    store = FlowStore(path)
    patch(store, activate=True, updates={"TAB": {"state": "RUNNING"}})
    document = json.loads(path.read_text(encoding="utf-8"))
    document["updated_at"] = "not-a-utc-timestamp"
    original = json.dumps(document).encode("utf-8")
    path.write_bytes(original)

    reloaded = FlowStore(path)

    assert reloaded.load_error
    with pytest.raises(FlowStoreMutationError):
        patch(reloaded, updates={"TAB": {"state": "DONE"}})
    assert path.read_bytes() == original


def test_invalid_patch_is_rejected_without_revision_or_file_change(tmp_path: Path) -> None:
    path = tmp_path / "flow.json"
    store = FlowStore(path)
    patch(store, activate=True, updates={"TAB": {"state": "RUNNING"}})
    before = store.document
    before_bytes = path.read_bytes()

    with pytest.raises(ValueError, match="state"):
        patch(store, updates={"TAB": {"state": "QUEUED"}})
    with pytest.raises(ValueError, match="goal_hash"):
        patch(store, goal_hash="not-a-sha256")

    assert store.document == before
    assert path.read_bytes() == before_bytes


def test_document_contains_only_semantic_flow_fields(tmp_path: Path) -> None:
    store = FlowStore(tmp_path / "flow.json")
    document = patch(
        store,
        activate=True,
        updates={"TAB": {"state": "RUNNING", "logical_role": "DEV", "from_role": "USER"}},
    )

    serialized = json.dumps(document)
    for forbidden in ("transcript", "dom_info", "command", "events", "config", "presence"):
        assert forbidden not in serialized
