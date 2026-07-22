from pathlib import Path
import tempfile
import threading
import unittest
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import server as controller


class DiagnosticControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.flow_path = Path(self.temp_dir.name) / ".role_state" / "flow.json"
        self.task_path = Path(self.temp_dir.name) / ".role_state" / "tasks.json"
        self.client = TestClient(controller.app)
        controller.state = controller.DiagnosticState(flow_path=self.flow_path, task_path=self.task_path)
        for role in ("DEV", "IMG", "REVIEW", "SOLO"):
            controller.state.status[role] = "ONLINE"
            controller.state.role_seen_at[role] = time.time()

    def tearDown(self):
        controller.state.task_scheduler.stop()
        self.temp_dir.cleanup()

    def test_status_report_and_sync_flow(self):
        command = controller.state.create_command("A", "PROBE", {"depth": 1})

        status_response = self.client.post(
            "/api/status",
            json={
                "role": "A",
                "session_id": "sess-1",
                "page_instance_id": "page-a",
                "dom_info": {"composer": True, "composer_text_len": 0},
            },
        )
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.json()["command"]["command_id"], command["command_id"])

        report_response = self.client.post(
            "/api/report",
            json={
                "role": "A",
                "session_id": "sess-1",
                "page_instance_id": "page-a",
                "command_id": command["command_id"],
                "state": "PROBE_DONE",
                "text": "probe ok",
                "result": {"composer": True},
                "dom_info": {"composer": True, "composer_text_len": 0},
            },
        )
        self.assertEqual(report_response.status_code, 200)
        self.assertEqual(controller.state.command_results[command["command_id"]]["state"], "PROBE_DONE")

        sync_response = self.client.post(
            "/api/sync",
            json={
                "role": "A",
                "session_id": "sess-1",
                "page_instance_id": "page-a",
                "reason": "manual",
                "transcript": {
                    "messages": [{"role": "user", "text": "hi"}],
                    "last_user": {"text": "hi"},
                    "last_assistant": {"text": "hello"},
                    "counts": {"user": 1, "assistant": 1},
                },
                "snapshot": {"composer": True, "composer_text_len": 0},
            },
        )
        self.assertEqual(sync_response.status_code, 200)
        self.assertEqual(controller.state.last_user_message["A"], "hi")
        self.assertEqual(controller.state.last_response["A"], "hello")

    def test_report_and_sync_refresh_liveness_during_long_browser_command(self):
        command = controller.state.create_command("A", "WAIT_ASSISTANT_DONE", {})
        self.client.post(
            "/api/status",
            json={"role": "A", "session_id": "sess-1", "page_instance_id": "page-a"},
        )
        controller.state.role_seen_at["A"] = time.time() - 460

        report_response = self.client.post(
            "/api/report",
            json={
                "role": "A",
                "session_id": "sess-1",
                "page_instance_id": "page-a",
                "command_id": command["command_id"],
                "state": "ASSISTANT_TEXT_CHANGED",
                "text": "JSON",
                "result": {},
                "dom_info": {},
            },
        )
        self.assertEqual(report_response.status_code, 200)
        self.assertTrue(self.client.get("/api/admin/role/A").json()["online"])

        controller.state.role_seen_at["A"] = time.time() - 460
        sync_response = self.client.post(
            "/api/sync",
            json={
                "role": "A",
                "session_id": "sess-1",
                "page_instance_id": "page-a",
                "reason": "mutation",
                "transcript": {"messages": [], "counts": {}},
                "snapshot": {},
            },
        )
        self.assertEqual(sync_response.status_code, 200)
        self.assertTrue(self.client.get("/api/admin/role/A").json()["online"])

    def test_stale_and_ignored_report_or_sync_do_not_refresh_liveness(self):
        self.client.post("/api/status", json={"role": "A", "session_id": "sess-current"})
        stale_seen_at = time.time() - 460
        controller.state.role_seen_at["A"] = stale_seen_at

        requests = [
            (
                "/api/report",
                {
                    "role": "A",
                    "session_id": "sess-stale",
                    "state": "ASSISTANT_PROGRESS",
                    "text": "still running",
                },
            ),
            (
                "/api/sync",
                {
                    "role": "A",
                    "session_id": "sess-stale",
                    "reason": "mutation",
                    "transcript": {},
                    "snapshot": {},
                },
            ),
            (
                "/api/report",
                {
                    "role": "A",
                    "session_id": "/backend-api/sentinel/frame.html",
                    "state": "ASSISTANT_PROGRESS",
                    "text": "ignored",
                },
            ),
            (
                "/api/sync",
                {
                    "role": "A",
                    "session_id": "/backend-api/sentinel/frame.html",
                    "reason": "sentinel",
                    "transcript": {},
                    "snapshot": {},
                },
            ),
        ]

        for path, payload in requests:
            response = self.client.post(path, json=payload)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(controller.state.role_seen_at["A"], stale_seen_at)

    def test_command_is_atomically_leased_to_one_page_instance(self):
        command = controller.state.create_command("A", "WAIT_ASSISTANT_DONE", {})

        missing = self.client.post(
            "/api/status",
            json={"role": "A", "session_id": "/c/missing", "dom_info": {"composer_text_len": 1}},
        ).json()
        owner = self.client.post(
            "/api/status",
            json={
                "role": "A",
                "session_id": "/c/owner",
                "page_instance_id": "page-a",
                "dom_info": {"composer_text_len": 2},
            },
        ).json()
        rival = self.client.post(
            "/api/status",
            json={
                "role": "A",
                "session_id": "/c/rival",
                "page_instance_id": "page-b",
                "dom_info": {"composer_text_len": 99},
            },
        ).json()
        owner_again = self.client.post(
            "/api/status",
            json={
                "role": "A",
                "session_id": "/c/owner-new-path",
                "page_instance_id": "page-a",
                "dom_info": {"composer_text_len": 3},
            },
        ).json()

        self.assertEqual(missing["command"]["action"], "WAIT")
        self.assertEqual(owner["command"]["command_id"], command["command_id"])
        self.assertEqual(rival["command"]["action"], "WAIT")
        self.assertEqual(owner_again["command"]["command_id"], command["command_id"])
        self.assertEqual(controller.state.commands["A"]["owner_page_instance_id"], "page-a")
        self.assertEqual(controller.state.commands["A"]["owner_session_id"], "/c/owner")
        self.assertEqual(controller.state.current_sessions["A"], "/c/owner-new-path")
        self.assertEqual(controller.state.dom_info["A"]["composer_text_len"], 3)

    def test_command_lease_is_atomic_under_concurrent_page_polling(self):
        command = controller.state.create_command("A", "PROBE", {})
        barrier = threading.Barrier(2)

        def poll(page_instance_id: str) -> dict:
            barrier.wait()
            return controller.state.get_command_for_role("A", f"/c/{page_instance_id}", page_instance_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(poll, ["page-a", "page-b"]))

        winners = [result for result in results if result.get("command_id") == command["command_id"]]
        losers = [result for result in results if result.get("action") == "WAIT"]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 1)
        self.assertIn(controller.state.commands["A"]["owner_page_instance_id"], {"page-a", "page-b"})

    def test_non_owner_cannot_steal_liveness_cache_or_terminal_result(self):
        command = controller.state.create_command("A", "WAIT_ASSISTANT_DONE", {})
        self.client.post(
            "/api/status",
            json={
                "role": "A",
                "session_id": "/c/owner",
                "page_instance_id": "page-a",
                "dom_info": {"composer_text_len": 1},
            },
        )
        owner_seen_at = time.time() - 460
        controller.state.role_seen_at["A"] = owner_seen_at

        for _ in range(2):
            rival_poll = self.client.post(
                "/api/status",
                json={
                    "role": "A",
                    "session_id": "/c/rival",
                    "page_instance_id": "page-b",
                    "dom_info": {"composer_text_len": 99},
                },
            ).json()
            self.assertEqual(rival_poll["command"]["action"], "WAIT")

        rival_report = self.client.post(
            "/api/report",
            json={
                "role": "A",
                "session_id": "/c/rival",
                "page_instance_id": "page-b",
                "command_id": command["command_id"],
                "state": "ASSISTANT_DONE",
                "text": "wrong",
                "dom_info": {"composer_text_len": 99},
            },
        ).json()
        stale_report = self.client.post(
            "/api/report",
            json={
                "role": "A",
                "session_id": "/c/owner",
                "page_instance_id": "page-a",
                "command_id": "stale-command",
                "state": "ASSISTANT_DONE",
                "text": "stale",
            },
        ).json()
        missing_page_report = self.client.post(
            "/api/report",
            json={
                "role": "A",
                "session_id": "/c/owner",
                "command_id": command["command_id"],
                "state": "ASSISTANT_DONE",
                "text": "missing page",
            },
        ).json()
        rival_sync = self.client.post(
            "/api/sync",
            json={
                "role": "A",
                "session_id": "/c/rival",
                "page_instance_id": "page-b",
                "reason": "mutation",
                "transcript": {"last_assistant": {"text": "wrong"}},
                "snapshot": {"composer_text_len": 99},
            },
        ).json()

        missing_page_sync = self.client.post(
            "/api/sync",
            json={
                "role": "A",
                "session_id": "/c/owner",
                "reason": "mutation",
                "transcript": {},
                "snapshot": {"composer_text_len": 88},
            },
        ).json()

        self.assertEqual(rival_report["status"], "IGNORED")
        self.assertEqual(stale_report["status"], "IGNORED")
        self.assertEqual(missing_page_report["status"], "IGNORED")
        self.assertEqual(rival_sync["status"], "IGNORED")
        self.assertEqual(missing_page_sync["status"], "IGNORED")
        self.assertEqual(controller.state.role_seen_at["A"], owner_seen_at)
        self.assertEqual(controller.state.current_sessions["A"], "/c/owner")
        self.assertEqual(controller.state.dom_info["A"]["composer_text_len"], 1)
        self.assertEqual(controller.state.command_status[command["command_id"]], "DELIVERED")
        self.assertEqual(controller.state.last_response["A"], "")
        self.assertNotIn(command["command_id"], controller.state.command_results)

        owner_report = self.client.post(
            "/api/report",
            json={
                "role": "A",
                "session_id": "/c/owner-new-path",
                "page_instance_id": "page-a",
                "command_id": command["command_id"],
                "state": "ASSISTANT_PROGRESS",
                "text": "working",
                "dom_info": {"composer_text_len": 2},
            },
        ).json()
        self.assertEqual(owner_report["status"], "OK")
        self.assertGreater(controller.state.role_seen_at["A"], owner_seen_at)
        self.assertEqual(controller.state.current_sessions["A"], "/c/owner-new-path")

        sync_seen_at = time.time() - 460
        controller.state.role_seen_at["A"] = sync_seen_at
        owner_sync = self.client.post(
            "/api/sync",
            json={
                "role": "A",
                "session_id": "/c/owner-sync-path",
                "page_instance_id": "page-a",
                "reason": "mutation",
                "transcript": {},
                "snapshot": {"composer_text_len": 4},
            },
        ).json()
        self.assertEqual(owner_sync["status"], "OK")
        self.assertGreater(controller.state.role_seen_at["A"], sync_seen_at)
        self.assertEqual(controller.state.current_sessions["A"], "/c/owner-sync-path")
        self.assertEqual(controller.state.dom_info["A"]["composer_text_len"], 4)

        terminal = self.client.post(
            "/api/report",
            json={
                "role": "A",
                "session_id": "/c/owner-sync-path",
                "page_instance_id": "page-a",
                "command_id": command["command_id"],
                "state": "ASSISTANT_DONE",
                "text": "done",
            },
        ).json()
        repeated = self.client.post(
            "/api/report",
            json={
                "role": "A",
                "session_id": "/c/owner-sync-path",
                "page_instance_id": "page-a",
                "command_id": command["command_id"],
                "state": "ASSISTANT_DONE",
                "text": "overwrite",
            },
        ).json()

        self.assertEqual(terminal["status"], "OK")
        self.assertEqual(repeated["status"], "IGNORED")
        self.assertEqual(controller.state.command_results[command["command_id"]]["text"], "done")

    def test_reload_page_identity_cannot_inherit_old_lease_and_new_command_can_lease(self):
        old_command = controller.state.create_command("A", "WAIT_ASSISTANT_DONE", {})
        first = self.client.post(
            "/api/status",
            json={"role": "A", "session_id": "/c/a", "page_instance_id": "page-a"},
        ).json()
        reloaded = self.client.post(
            "/api/status",
            json={"role": "A", "session_id": "/c/a", "page_instance_id": "page-a-reload"},
        ).json()

        self.assertEqual(first["command"]["command_id"], old_command["command_id"])
        self.assertEqual(reloaded["command"]["action"], "WAIT")

        controller.state.command_results[old_command["command_id"]] = {"state": "ASSISTANT_TIMEOUT"}
        new_command = controller.state.create_command("A", "PROBE", {})
        new_lease = self.client.post(
            "/api/status",
            json={"role": "A", "session_id": "/c/a", "page_instance_id": "page-a-reload"},
        ).json()

        self.assertEqual(new_lease["command"]["command_id"], new_command["command_id"])
        self.assertEqual(controller.state.commands["A"]["owner_page_instance_id"], "page-a-reload")

    def test_new_page_cancels_stranded_probe_and_accepts_clean_observation(self):
        claim = self.client.post("/api/reserve-role-claim", json={"role": "PLAN"}).json()["role_claim_id"]
        identity = {"role": "PLAN", "role_owner_id": "tab-1", "role_claim_id": claim}
        self.client.post(
            "/api/status",
            json={
                **identity,
                "session_id": "/c/old",
                "page_instance_id": "page-old",
                "claim_role": True,
                "observation_seq": 1,
                "dom_info": {"page_path": "/c/old", "marker": "old"},
            },
        )
        command = controller.state.create_command("PLAN", "PROBE", {})
        leased = self.client.post(
            "/api/status",
            json={**identity, "session_id": "/c/old", "page_instance_id": "page-old"},
        ).json()

        recovered = self.client.post(
            "/api/status",
            json={
                **identity,
                "session_id": "/",
                "page_instance_id": "page-new",
                "observation_seq": 1,
                "dom_info": {"page_path": "/", "marker": "new"},
            },
        ).json()

        self.assertEqual(leased["command"]["command_id"], command["command_id"])
        self.assertEqual(recovered["command"]["action"], "WAIT")
        self.assertFalse(recovered["clear_role"])
        self.assertTrue(recovered["observation_accepted"])
        self.assertEqual(controller.state.observation_pages["PLAN"]["page_instance_id"], "page-new")
        self.assertEqual(controller.state.dom_info["PLAN"]["marker"], "new")
        self.assertEqual(controller.state.command_status[command["command_id"]], "CANCELLED")
        self.assertEqual(controller.state.command_results[command["command_id"]]["result"]["reason"], "owner_page_replaced")

    def test_new_page_cancels_delivered_assistant_wait_once_and_leases_next_command(self):
        claim = self.client.post("/api/reserve-role-claim", json={"role": "PLAN"}).json()["role_claim_id"]
        identity = {"role": "PLAN", "role_owner_id": "tab-1", "role_claim_id": claim}
        self.client.post(
            "/api/status",
            json={
                **identity,
                "session_id": "/c/old",
                "page_instance_id": "page-old",
                "claim_role": True,
                "observation_seq": 1,
                "dom_info": {"page_path": "/c/old", "marker": "old"},
            },
        )
        command = controller.state.create_command("PLAN", "WAIT_ASSISTANT_DONE", {})
        leased = self.client.post(
            "/api/status",
            json={**identity, "session_id": "/c/old", "page_instance_id": "page-old"},
        ).json()

        replaced = self.client.post(
            "/api/status",
            json={
                **identity,
                "session_id": "/",
                "page_instance_id": "page-new",
                "observation_seq": 1,
                "dom_info": {"page_path": "/", "marker": "new"},
            },
        ).json()
        repeated = self.client.post(
            "/api/status",
            json={
                **identity,
                "session_id": "/",
                "page_instance_id": "page-new",
                "observation_seq": 2,
                "dom_info": {"page_path": "/", "marker": "newer"},
            },
        ).json()
        stale_report = self.client.post(
            "/api/report",
            json={
                **identity,
                "session_id": "/c/old",
                "page_instance_id": "page-old",
                "command_id": command["command_id"],
                "state": "ASSISTANT_DONE",
                "text": "late old-page response",
            },
        ).json()

        result = controller.state.command_results[command["command_id"]]
        self.assertEqual(leased["command"]["command_id"], command["command_id"])
        self.assertEqual(replaced["command"]["action"], "WAIT")
        self.assertTrue(replaced["observation_accepted"])
        self.assertTrue(repeated["observation_accepted"])
        self.assertEqual(result["state"], "CANCELLED")
        self.assertEqual(result["result"]["reason"], "owner_page_replaced")
        self.assertEqual(stale_report["status"], "IGNORED")
        self.assertEqual(controller.state.command_results[command["command_id"]], result)

        next_command = controller.state.create_command("PLAN", "PROBE", {})
        next_lease = self.client.post(
            "/api/status",
            json={**identity, "session_id": "/", "page_instance_id": "page-new"},
        ).json()
        self.assertEqual(next_lease["command"]["command_id"], next_command["command_id"])

    def test_expired_probe_is_terminal_and_unblocks_next_command(self):
        command = controller.state.create_command("A", "PROBE", {})
        leased = self.client.post(
            "/api/status",
            json={"role": "A", "session_id": "/c/old", "page_instance_id": "page-old"},
        ).json()
        expired = self.client.post(
            f"/api/admin/command/{command['command_id']}/cancel",
            json={"state": "EXPIRED", "reason": "bridge_timeout"},
        ).json()
        next_command = controller.state.create_command("A", "PROBE", {})
        next_lease = self.client.post(
            "/api/status",
            json={"role": "A", "session_id": "/", "page_instance_id": "page-new"},
        ).json()

        self.assertEqual(leased["command"]["command_id"], command["command_id"])
        self.assertTrue(expired["done"])
        self.assertEqual(expired["status"], "EXPIRED")
        self.assertEqual(expired["result"]["result"]["reason"], "bridge_timeout")
        self.assertEqual(next_lease["command"]["command_id"], next_command["command_id"])

    def test_superseded_probe_is_cancelled_instead_of_orphaned(self):
        first = controller.state.create_command("A", "PROBE", {})
        second = controller.state.create_command("A", "PROBE", {})

        self.assertEqual(controller.state.command_status[first["command_id"]], "CANCELLED")
        self.assertEqual(
            controller.state.command_results[first["command_id"]]["result"]["reason"],
            "superseded_by_new_command",
        )
        self.assertEqual(controller.state.active_command("A")["command_id"], second["command_id"])

    def test_admin_cannot_supersede_active_mutating_command(self):
        command = controller.state.create_command("A", "CLICK_SEND", {})
        self.client.post(
            "/api/status",
            json={"role": "A", "session_id": "/c/owner", "page_instance_id": "page-owner"},
        )

        rejected = self.client.post(
            "/api/admin/command",
            json={"role": "A", "action": "PROBE", "payload": {}},
        )

        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(controller.state.active_command("A")["command_id"], command["command_id"])
        self.assertEqual(controller.state.command_status[command["command_id"]], "DELIVERED")

    def test_new_page_cannot_steal_mutating_command_from_same_claim(self):
        claim = self.client.post("/api/reserve-role-claim", json={"role": "PLAN"}).json()["role_claim_id"]
        identity = {"role": "PLAN", "role_owner_id": "tab-1", "role_claim_id": claim}
        self.client.post(
            "/api/status",
            json={
                **identity,
                "session_id": "/c/old",
                "page_instance_id": "page-old",
                "claim_role": True,
                "observation_seq": 1,
                "dom_info": {"marker": "old"},
            },
        )
        command = controller.state.create_command("PLAN", "CLICK_SEND", {})
        leased = self.client.post(
            "/api/status",
            json={**identity, "session_id": "/c/old", "page_instance_id": "page-old"},
        ).json()
        rival = self.client.post(
            "/api/status",
            json={
                **identity,
                "session_id": "/",
                "page_instance_id": "page-new",
                "observation_seq": 1,
                "dom_info": {"marker": "new"},
            },
        ).json()

        self.assertEqual(leased["command"]["command_id"], command["command_id"])
        self.assertEqual(rival["command"]["action"], "WAIT")
        self.assertFalse(rival["observation_accepted"])
        self.assertEqual(controller.state.command_status[command["command_id"]], "DELIVERED")
        self.assertEqual(controller.state.observation_pages["PLAN"]["page_instance_id"], "page-old")
        self.assertEqual(controller.state.dom_info["PLAN"]["marker"], "old")

    def test_role_owner_claim_survives_reload_but_rejects_displaced_claim(self):
        first_claim = self.client.post("/api/reserve-role-claim", json={"role": "PLAN"}).json()["role_claim_id"]
        takeover_claim = self.client.post("/api/reserve-role-claim", json={"role": "PLAN"}).json()["role_claim_id"]
        first = self.client.post(
            "/api/status",
            json={"role": "PLAN", "session_id": "/c/same", "page_instance_id": "page-old",
                  "role_owner_id": "tab-current", "role_claim_id": first_claim, "claim_role": True},
        ).json()
        self.assertFalse(first["clear_role"])
        reloaded = self.client.post(
            "/api/status",
            json={"role": "PLAN", "session_id": "/c/same", "page_instance_id": "page-new",
                  "role_owner_id": "tab-current", "role_claim_id": first_claim},
        ).json()
        self.assertFalse(reloaded["clear_role"])
        takeover = self.client.post(
            "/api/status",
            json={"role": "PLAN", "session_id": "/c/other", "page_instance_id": "page-other",
                  "role_owner_id": "tab-other", "role_claim_id": takeover_claim, "claim_role": True},
        ).json()
        self.assertFalse(takeover["clear_role"])
        stale_reload = self.client.post(
            "/api/status",
            json={"role": "PLAN", "session_id": "/c/same", "page_instance_id": "page-after-reload",
                  "role_owner_id": "tab-current", "role_claim_id": first_claim},
        ).json()
        self.assertTrue(stale_reload["clear_role"])

    def test_stale_same_owner_claim_cannot_release_newer_claim(self):
        claim_ids = [self.client.post("/api/reserve-role-claim", json={"role": "PLAN"}).json()["role_claim_id"] for _ in range(2)]
        for claim_id in claim_ids:
            response = self.client.post(
                "/api/status",
                json={"role": "PLAN", "session_id": "/c/same", "page_instance_id": "page-a",
                      "role_owner_id": "tab-a", "role_claim_id": claim_id, "claim_role": True},
            ).json()
            self.assertFalse(response["clear_role"])
        stale_release = self.client.post(
            "/api/release-role",
            json={"role": "PLAN", "session_id": "/c/same", "page_instance_id": "page-a",
                  "role_owner_id": "tab-a", "role_claim_id": claim_ids[0]},
        ).json()
        self.assertFalse(stale_release["released"])
        self.assertEqual(controller.state.role_owners["PLAN"]["role_claim_id"], claim_ids[1])
        current_release = self.client.post(
            "/api/release-role",
            json={"role": "PLAN", "session_id": "/c/same", "page_instance_id": "page-a",
                  "role_owner_id": "tab-a", "role_claim_id": claim_ids[1]},
        ).json()
        self.assertTrue(current_release["released"])
        self.assertNotIn("PLAN", controller.state.role_owners)

    def test_admin_role_projects_configured_role_turn_responses_and_composer_counts(self):
        role = "C1"
        controller.state.role_seen_at[role] = time.time()
        controller.state.last_user_message[role] = 'PROMPT_ROLE: DEV\nRUNTIME_PROVENANCE_JSON: {"prompt_role":"DEV"}'
        controller.state.transcripts[role] = [
            {"role": "user", "text": "goal"},
            {"role": "assistant", "text": "first", "images": [{"src": "one"}]},
            {"role": "assistant", "text": "second"},
        ]
        controller.state.dom_info[role] = {
            "composer": True,
            "composer_text_len": 7,
            "composer_attachments": [{"label": "Remove file"}],
            "messages": {"counts": {"user": 1, "assistant": 2, "images": 1}},
            "page_path": "/c/example",
        }

        inventory = self.client.get("/api/admin/roles").json()["roles"]
        card = next(item for item in inventory if item["role"] == role)
        self.assertEqual(card["configured_role"], "DEV")
        self.assertEqual(card["turn"], 2)
        self.assertEqual(card["dom_summary"]["composer_attachment_count"], 1)

        detail = self.client.get(f"/api/admin/role/{role}").json()
        self.assertEqual(detail["configured_role"], "DEV")
        self.assertEqual(detail["turn"], 2)
        self.assertEqual([item["turn"] for item in detail["responses"]], [1, 2])
        self.assertEqual(detail["responses"][0]["image_count"], 1)
        self.assertEqual(detail["message_counts"]["assistant"], 2)

    def test_admin_role_response_limit_bounds_payload_without_changing_turn_count(self):
        role = "C1"
        controller.state.role_seen_at[role] = time.time()
        controller.state.transcripts[role] = [
            {"role": "assistant", "text": f"response {index}"}
            for index in range(15)
        ]

        detail = self.client.get(f"/api/admin/role/{role}?response_limit=10").json()

        self.assertEqual(detail["turn"], 15)
        self.assertEqual(len(detail["responses"]), 10)
        self.assertEqual(detail["responses"][0]["turn"], 6)
        self.assertEqual(detail["responses"][-1]["turn"], 15)

    def test_admin_roles_excludes_stale_historical_cache_and_removes_released_role_immediately(self):
        stale_role = "C3"
        controller.state.status[stale_role] = "OFFLINE"
        controller.state.role_seen_at[stale_role] = time.time() - 3600
        controller.state.sessions[stale_role].add("session-stale")
        controller.state.current_sessions[stale_role] = "session-stale"
        controller.state.dom_info[stale_role] = {"page_path": "/c/stale"}
        controller.state.transcripts[stale_role] = [{"role": "assistant", "text": "old"}]
        controller.state.last_response[stale_role] = "old"

        roles_before = {item["role"] for item in self.client.get("/api/admin/roles").json()["roles"]}
        self.assertNotIn(stale_role, roles_before)

        claim = self.client.post("/api/reserve-role-claim", json={"role": "TEMP"}).json()["role_claim_id"]
        identity = {
            "role": "TEMP",
            "session_id": "/c/temp",
            "page_instance_id": "page-temp",
            "role_owner_id": "tab-temp",
            "role_claim_id": claim,
        }
        claimed = self.client.post("/api/status", json={**identity, "claim_role": True}).json()
        self.assertFalse(claimed["clear_role"])
        roles_claimed = {item["role"] for item in self.client.get("/api/admin/roles").json()["roles"]}
        self.assertIn("TEMP", roles_claimed)
        temp_status = next(item for item in self.client.get("/api/admin/roles").json()["roles"] if item["role"] == "TEMP")
        self.assertIn("dom_summary", temp_status)
        self.assertIn("page_instance_id", temp_status)
        self.assertIn("observation_seq", temp_status)
        self.assertIn("bridge_version", temp_status)

        released = self.client.post("/api/release-role", json=identity).json()
        self.assertTrue(released["released"])
        roles_released = {item["role"] for item in self.client.get("/api/admin/roles").json()["roles"]}
        self.assertNotIn("TEMP", roles_released)

    def test_admin_roles_keeps_offline_operational_roles_and_recent_cached_evidence_bounded(self):
        now = time.time()
        controller.state.config["dashboard_role_retention_s"] = 60
        controller.state.create_command("COMMANDER", "PROBE", {})
        controller.state.update_flow_statuses(
            "run-active",
            {"FLOWER": {"state": "RUNNING", "logical_role": "PLAN"}},
            request_id="request-active",
            activate=True,
        )
        controller.state.role_seen_at["FLOWER"] = now
        controller.state.current_sessions["FLOWER"] = "session-flower"
        controller.state.dom_info["FLOWER"] = {"page_path": "/c/flower"}
        initially_online = {item["role"]: item for item in self.client.get("/api/admin/roles").json()["roles"]}
        self.assertTrue(initially_online["FLOWER"]["online"])
        self.assertEqual(initially_online["FLOWER"]["status"], "ONLINE")

        controller.state.role_seen_at["FLOWER"] = now - 3600
        controller.state.role_seen_at["RECENT"] = now - 30
        controller.state.current_sessions["RECENT"] = "session-recent"
        controller.state.dom_info["RECENT"] = {"page_path": "/c/recent-cached"}
        controller.state.role_seen_at["EXPIRED"] = now - 61
        controller.state.current_sessions["EXPIRED"] = "session-expired"
        controller.state.dom_info["EXPIRED"] = {"page_path": "/c/expired-cached"}

        inventory = {item["role"]: item for item in self.client.get("/api/admin/roles").json()["roles"]}

        self.assertEqual(inventory["COMMANDER"]["status"], "OFFLINE")
        self.assertEqual(inventory["FLOWER"]["status"], "OFFLINE")
        self.assertTrue(inventory["COMMANDER"]["evidence_cached"])
        self.assertEqual(inventory["RECENT"]["status"], "STALE")
        self.assertEqual(inventory["RECENT"]["page_path"], "/c/recent-cached")
        self.assertTrue(inventory["RECENT"]["evidence_cached"])
        self.assertNotIn("EXPIRED", inventory)

        controller.state.role_seen_at["FLOWER"] = time.time()
        controller.state.role_seen_at["RECENT"] = time.time()
        recovered = {item["role"]: item for item in self.client.get("/api/admin/roles").json()["roles"]}
        for role in ("FLOWER", "RECENT"):
            self.assertTrue(recovered[role]["online"])
            self.assertEqual(recovered[role]["status"], "ONLINE")
            self.assertFalse(recovered[role]["evidence_cached"])

    def test_current_claim_releases_after_normal_path_change(self):
        self.client.post(
            "/api/status",
            json={"role": "PLAN", "session_id": "/c/original", "page_instance_id": "page-a",
                  "role_owner_id": "tab-a", "role_claim_id": "claim-current", "claim_role": True},
        )
        released = self.client.post(
            "/api/release-role",
            json={"role": "PLAN", "session_id": "/c/new-path", "page_instance_id": "page-reloaded",
                  "role_owner_id": "tab-a", "role_claim_id": "claim-current"},
        ).json()
        self.assertTrue(released["released"])
        self.assertNotIn("PLAN", controller.state.role_owners)

    def test_delayed_older_claim_cannot_replace_newer_claim(self):
        newer = self.client.post(
            "/api/status",
            json={"role": "PLAN", "session_id": "/c/new", "page_instance_id": "page-new",
                  "role_owner_id": "tab-new", "role_claim_id": "2000-new", "claim_role": True},
        ).json()
        older = self.client.post(
            "/api/status",
            json={"role": "PLAN", "session_id": "/c/old", "page_instance_id": "page-old",
                  "role_owner_id": "tab-old", "role_claim_id": "1000-old", "claim_role": True},
        ).json()
        self.assertFalse(newer["clear_role"])
        self.assertTrue(older["clear_role"])
        self.assertEqual(controller.state.role_owners["PLAN"]["role_claim_id"], "2000-new")

    def test_reserved_claim_generations_are_monotonic_and_ignore_same_tick_suffixes(self):
        old_claim = self.client.post("/api/reserve-role-claim", json={"role": "PLAN"}).json()["role_claim_id"]
        new_claim = self.client.post("/api/reserve-role-claim", json={"role": "PLAN"}).json()["role_claim_id"]
        self.assertNotEqual(old_claim, new_claim)
        new_first = self.client.post(
            "/api/status",
            json={"role": "PLAN", "session_id": "/c/new", "page_instance_id": "page-new",
                  "role_owner_id": "tab-new", "role_claim_id": new_claim, "claim_role": True},
        ).json()
        delayed_old = self.client.post(
            "/api/status",
            json={"role": "PLAN", "session_id": "/c/old", "page_instance_id": "page-old",
                  "role_owner_id": "tab-old", "role_claim_id": old_claim, "claim_role": True},
        ).json()
        self.assertFalse(new_first["clear_role"])
        self.assertTrue(delayed_old["clear_role"])
        self.assertEqual(controller.state.role_owners["PLAN"]["role_claim_id"], new_claim)

    def test_reserved_claim_generations_accept_newer_owner_after_older_owner(self):
        old_claim = self.client.post("/api/reserve-role-claim", json={"role": "PLAN"}).json()["role_claim_id"]
        new_claim = self.client.post("/api/reserve-role-claim", json={"role": "PLAN"}).json()["role_claim_id"]
        old_first = self.client.post(
            "/api/status",
            json={"role": "PLAN", "session_id": "/c/old", "page_instance_id": "page-old",
                  "role_owner_id": "tab-old", "role_claim_id": old_claim, "claim_role": True},
        ).json()
        newer = self.client.post(
            "/api/status",
            json={"role": "PLAN", "session_id": "/c/new", "page_instance_id": "page-new",
                  "role_owner_id": "tab-new", "role_claim_id": new_claim, "claim_role": True},
        ).json()
        self.assertFalse(old_first["clear_role"])
        self.assertFalse(newer["clear_role"])
        self.assertEqual(controller.state.role_owners["PLAN"]["role_claim_id"], new_claim)

    def test_reserved_claims_keep_literal_exact_role_names_independent(self):
        for role in ("PLAN", "PLAN1", "PLAN***"):
            claim = self.client.post("/api/reserve-role-claim", json={"role": role}).json()["role_claim_id"]
            result = self.client.post(
                "/api/status",
                json={"role": role, "session_id": f"/c/{role}", "page_instance_id": f"page-{role}",
                      "role_owner_id": f"tab-{role}", "role_claim_id": claim, "claim_role": True},
            ).json()
            self.assertFalse(result["clear_role"])
        self.assertEqual(set(controller.state.role_owners), {"PLAN", "PLAN1", "PLAN***"})

    def test_stale_status_is_mutation_free_including_sessions(self):
        self.client.post(
            "/api/status",
            json={"role": "PLAN", "session_id": "/c/current", "page_instance_id": "page-current",
                  "role_owner_id": "tab-current", "role_claim_id": "2000-current", "claim_role": True,
                  "dom_info": {"messages": {"last_assistant": {"text": "current"}}}},
        )
        before = {
            "owner": dict(controller.state.role_owners["PLAN"]),
            "sessions": set(controller.state.sessions["PLAN"]),
            "current_session": controller.state.current_sessions.get("PLAN"),
            "dom": dict(controller.state.dom_info["PLAN"]),
            "transcript": list(controller.state.transcripts["PLAN"]),
            "last_response": controller.state.last_response["PLAN"],
            "status": controller.state.status["PLAN"],
            "seen": controller.state.role_seen_at["PLAN"],
            "events": list(controller.state.events),
        }
        stale = self.client.post(
            "/api/status",
            json={"role": "PLAN", "session_id": "/c/stale", "page_instance_id": "page-stale",
                  "role_owner_id": "tab-stale", "role_claim_id": "1000-stale",
                  "dom_info": {"messages": {"last_assistant": {"text": "stale"}}}},
        ).json()
        self.assertTrue(stale["clear_role"])
        self.assertEqual(controller.state.role_owners["PLAN"], before["owner"])
        self.assertEqual(controller.state.sessions["PLAN"], before["sessions"])
        self.assertEqual(controller.state.current_sessions.get("PLAN"), before["current_session"])
        self.assertEqual(controller.state.dom_info["PLAN"], before["dom"])
        self.assertEqual(controller.state.transcripts["PLAN"], before["transcript"])
        self.assertEqual(controller.state.last_response["PLAN"], before["last_response"])
        self.assertEqual(controller.state.status["PLAN"], before["status"])
        self.assertEqual(controller.state.role_seen_at["PLAN"], before["seen"])
        self.assertEqual(list(controller.state.events), before["events"])

    def test_current_owner_sync_with_exact_identity_is_accepted(self):
        self.client.post(
            "/api/status",
            json={"role": "PLAN", "session_id": "/c/current", "page_instance_id": "page-current",
                  "role_owner_id": "tab-current", "role_claim_id": "2000-current", "claim_role": True},
        )
        synced = self.client.post(
            "/api/sync",
            json={"role": "PLAN", "session_id": "/c/current", "page_instance_id": "page-reload",
                  "role_owner_id": "tab-current", "role_claim_id": "2000-current", "reason": "periodic",
                  "transcript": {"messages": [{"role": "assistant", "text": "accepted"}], "counts": {}, "last_user": {}, "last_assistant": {"text": "accepted"}},
                  "snapshot": {"messages": {"messages": [{"role": "assistant", "text": "accepted"}], "counts": {}, "last_assistant": {"text": "accepted"}}}},
        ).json()
        self.assertEqual(synced["status"], "OK")
        self.assertEqual(controller.state.last_response["PLAN"], "accepted")

    def test_displaced_claim_cannot_mutate_report_or_sync_without_command(self):
        claims = [self.client.post("/api/reserve-role-claim", json={"role": "PLAN"}).json()["role_claim_id"] for _ in range(2)]
        for owner_id, claim_id, page_id in (("tab-a", claims[0], "page-a"), ("tab-b", claims[1], "page-b")):
            self.client.post(
                "/api/status",
                json={"role": "PLAN", "session_id": f"/c/{owner_id}", "page_instance_id": page_id,
                      "role_owner_id": owner_id, "role_claim_id": claim_id, "claim_role": True,
                      "dom_info": {"messages": {"last_assistant": {"text": owner_id}}}},
            )
        before = dict(controller.state.dom_info["PLAN"])
        report = self.client.post(
            "/api/report",
            json={"role": "PLAN", "session_id": "/c/tab-a", "page_instance_id": "page-a",
                  "role_owner_id": "tab-a", "role_claim_id": claims[0], "state": "ASSISTANT_DONE",
                  "text": "stale report", "dom_info": {"messages": {"last_assistant": {"text": "stale"}}}},
        ).json()
        sync = self.client.post(
            "/api/sync",
            json={"role": "PLAN", "session_id": "/c/tab-a", "page_instance_id": "page-a",
                  "role_owner_id": "tab-a", "role_claim_id": claims[0], "snapshot": {"messages": {"last_assistant": {"text": "stale sync"}}}},
        ).json()
        self.assertEqual(report["status"], "IGNORED")
        self.assertEqual(sync["status"], "IGNORED")
        self.assertEqual(controller.state.dom_info["PLAN"], before)

    def test_sentinel_never_leases_or_refreshes_command(self):
        command = controller.state.create_command("A", "PROBE", {})
        stale_seen_at = time.time() - 460
        controller.state.role_seen_at["A"] = stale_seen_at

        status = self.client.post(
            "/api/status",
            json={
                "role": "A",
                "session_id": "/backend-api/sentinel/frame.html",
                "page_instance_id": "sentinel-page",
            },
        ).json()
        report = self.client.post(
            "/api/report",
            json={
                "role": "A",
                "session_id": "/backend-api/sentinel/frame.html",
                "page_instance_id": "sentinel-page",
                "command_id": command["command_id"],
                "state": "PROBE_DONE",
            },
        ).json()
        sync = self.client.post(
            "/api/sync",
            json={
                "role": "A",
                "session_id": "/backend-api/sentinel/frame.html",
                "page_instance_id": "sentinel-page",
            },
        ).json()

        self.assertEqual(status["command"]["action"], "WAIT")
        self.assertEqual(report["status"], "IGNORED")
        self.assertEqual(sync["status"], "IGNORED")
        self.assertEqual(controller.state.commands["A"]["owner_page_instance_id"], "")
        self.assertEqual(controller.state.role_seen_at["A"], stale_seen_at)
        self.assertNotIn(command["command_id"], controller.state.command_results)

    def _activate_running_flow(self, run_id="run-test", role="TEST1", logical_role="REVIEW"):
        self.client.post(
            "/api/admin/flow-status",
            json={
                "run_id": run_id,
                "activate": True,
                "updates": {role: {"state": "RUNNING", "logical_role": logical_role, "from_role": "User"}},
            },
        )

    def test_flow_heartbeat_endpoint_records_runner_liveness(self):
        self._activate_running_flow()
        response = self.client.post(
            "/api/admin/flow-heartbeat",
            json={"run_id": "run-test", "pid": 4321},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["request_id"], "run-test")

        liveness = self.client.get("/api/admin/flow").json()["liveness"]
        self.assertEqual(liveness["runner"]["state"], "RUNNING")
        self.assertEqual(liveness["runner"]["pid"], 4321)
        self.assertIsNotNone(liveness["runner"]["last_heartbeat_age_s"])
        self.assertFalse(liveness["stalled"])

    def test_e09_stopped_runner_marks_active_flow_stalled(self):
        # A durable flow left RUNNING by a runner that crashed before terminalizing
        # must surface as STALLED once the heartbeat goes stale, never RUNNING.
        self._activate_running_flow(role="TEST1", logical_role="REVIEW")
        self.client.post("/api/admin/flow-heartbeat", json={"run_id": "run-test", "pid": 999})
        # Simulate the runner process dying: the last heartbeat is now well past the window.
        controller.state.runner_heartbeats["run-test"]["ts"] = time.time() - 10_000

        liveness = self.client.get("/api/admin/flow").json()["liveness"]
        self.assertTrue(liveness["stalled"])
        self.assertEqual(liveness["runner"]["state"], "STOPPED")
        self.assertEqual(liveness["role"], "TEST1")
        self.assertEqual(liveness["logical_role"], "REVIEW")
        self.assertEqual(liveness["reason"], "runner stopped before terminalizing flow")
        self.assertEqual(liveness["next_action"], "recover existing flow")

        # The single-tab overlay poll must also stop reporting a live RUNNING state.
        poll = self.client.post("/api/status", json={"role": "TEST1", "session_id": "/c/1"}).json()
        self.assertEqual(poll["flow_status"]["state"], "RUNNING")
        self.assertTrue(poll["flow_status"]["stalled"])

    def test_e08_stuck_delivered_command_marks_flow_stalled(self):
        # A recovery command stuck in DELIVERED (e.g. SYNC_TRANSCRIPT whose
        # TRANSCRIPT_SAVED never arrives) must surface STALLED even while the
        # runner process is still alive and heartbeating.
        self._activate_running_flow(role="TEST1", logical_role="REVIEW")
        self.client.post("/api/admin/flow-heartbeat", json={"run_id": "run-test", "pid": 111})

        command = controller.state.create_command("TEST1", "SYNC_TRANSCRIPT", {})
        command["status"] = "DELIVERED"
        command["delivered_at"] = time.time() - 10_000
        controller.state.command_status[command["command_id"]] = "DELIVERED"

        liveness = self.client.get("/api/admin/flow").json()["liveness"]
        self.assertTrue(liveness["stalled"])
        self.assertEqual(liveness["runner"]["state"], "RUNNING")
        self.assertEqual(liveness["reason"], "SYNC_TRANSCRIPT command did not terminate")
        self.assertEqual(liveness["last_command"]["action"], "SYNC_TRANSCRIPT")
        self.assertEqual(liveness["last_command"]["state"], "DELIVERED")

    def test_request_specific_liveness_does_not_leak_active_command_from_another_flow(self):
        self.client.post(
            "/api/admin/flow-status",
            json={
                "request_id": "old-flow",
                "run_id": "old-run",
                "activate": True,
                "updates": {
                    "DEV": {
                        "state": "RUNNING",
                        "logical_role": "REVIEW",
                        "from_role": "PLAN",
                    }
                },
            },
        )
        self.client.post(
            "/api/admin/flow-status",
            json={
                "request_id": "new-flow",
                "run_id": "new-run",
                "activate": True,
                "updates": {
                    "DEV": {
                        "state": "RUNNING",
                        "logical_role": "PLAN",
                        "from_role": "REVIEW",
                    }
                },
            },
        )
        self.assertEqual(controller.state.flow_store.document["active_request_id"], "new-flow")
        self.client.post(
            "/api/admin/flow-heartbeat",
            json={"request_id": "new-flow", "run_id": "new-run", "pid": 4321},
        )
        command = controller.state.create_command("DEV", "WAIT_ASSISTANT_DONE", {})
        command["status"] = "DELIVERED"
        command["delivered_at"] = time.time() - 10_000
        controller.state.command_status[command["command_id"]] = "DELIVERED"

        old_flow = self.client.get("/api/admin/flow", params={"request_id": "old-flow"}).json()
        new_flow = self.client.get("/api/admin/flow", params={"request_id": "new-flow"}).json()

        self.assertEqual(old_flow["flow"]["request_id"], "old-flow")
        self.assertEqual(old_flow["active_request_id"], "new-flow")
        self.assertEqual(old_flow["liveness"]["runner"]["state"], "UNKNOWN")
        self.assertTrue(old_flow["liveness"]["stalled"])
        self.assertEqual(
            old_flow["liveness"]["reason"],
            "runner heartbeat unavailable for active flow",
        )
        self.assertIsNone(old_flow["liveness"]["last_command"])
        self.assertEqual(old_flow["liveness"]["role"], "DEV")
        self.assertEqual(old_flow["liveness"]["logical_role"], "REVIEW")
        self.assertEqual(old_flow["liveness"]["next_action"], "recover existing flow")

        self.assertEqual(new_flow["flow"]["request_id"], "new-flow")
        self.assertEqual(new_flow["liveness"]["runner"]["state"], "RUNNING")
        self.assertTrue(new_flow["liveness"]["stalled"])
        self.assertEqual(
            new_flow["liveness"]["reason"],
            "WAIT_ASSISTANT_DONE command did not terminate",
        )
        self.assertEqual(new_flow["liveness"]["last_command"]["action"], "WAIT_ASSISTANT_DONE")
        self.assertEqual(new_flow["liveness"]["last_command"]["state"], "DELIVERED")
        self.assertEqual(new_flow["liveness"]["role"], "DEV")
        self.assertEqual(new_flow["liveness"]["logical_role"], "PLAN")

    def test_terminal_flow_is_never_stalled(self):
        self._activate_running_flow(role="TEST1", logical_role="REVIEW")
        self.client.post("/api/admin/flow-heartbeat", json={"run_id": "run-test"})
        controller.state.runner_heartbeats["run-test"]["ts"] = time.time() - 10_000
        self.client.post(
            "/api/admin/flow-status",
            json={"run_id": "run-test", "terminal_status": "complete", "updates": {}},
        )

        liveness = self.client.get("/api/admin/flow").json()["liveness"]
        self.assertFalse(liveness["stalled"])

    def test_fresh_running_flow_is_not_stalled(self):
        self._activate_running_flow(role="TEST1", logical_role="REVIEW")
        self.client.post("/api/admin/flow-heartbeat", json={"run_id": "run-test"})
        liveness = self.client.get("/api/admin/flow").json()["liveness"]
        self.assertFalse(liveness["stalled"])
        self.assertEqual(liveness["runner"]["state"], "RUNNING")

    def test_flow_status_is_returned_only_to_the_polling_role(self):
        response = self.client.post(
            "/api/admin/flow-status",
            json={
                "run_id": "run-test",
                "updates": {
                    "TEST1": {"state": "RUNNING", "from_role": "User"},
                    "TEST2": {"state": "WAITING"},
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        self.client.post("/api/admin/flow-heartbeat", json={"run_id": "run-test"})
        test1 = self.client.post("/api/status", json={"role": "TEST1", "session_id": "/c/1"}).json()
        test2 = self.client.post("/api/status", json={"role": "TEST2", "session_id": "/c/2"}).json()
        dev = self.client.post("/api/status", json={"role": "DEV", "session_id": "/c/dev"}).json()

        self.assertEqual(
            test1["flow_status"],
            {
                "run_id": "run-test",
                "state": "RUNNING",
                "stalled": False,
                "from_role": "User",
            },
        )
        self.assertEqual(test2["flow_status"], {"run_id": "run-test", "state": "WAITING", "stalled": False})
        self.assertIsNone(dev["flow_status"])

    def test_flow_status_accepts_done_with_from_detail(self):
        response = self.client.post(
            "/api/admin/flow-status",
            json={
                "run_id": "run-test",
                "updates": {
                    "TEST1": {"state": "DONE", "done_from": "A", "sent_to": "B"},
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        status = self.client.post("/api/status", json={"role": "TEST1", "session_id": "/c/1"}).json()
        self.assertEqual(
            status["flow_status"],
            {"run_id": "run-test", "state": "DONE", "stalled": False, "done_from": "A", "sent_to": "B"},
        )

    def test_waiting_retains_only_the_same_runs_validated_destination(self):
        self.client.post(
            "/api/admin/flow-status",
            json={"run_id": "run-test", "updates": {"TEST1": {"state": "DONE", "done_from": "A", "sent_to": "B"}}},
        )
        self.client.post(
            "/api/admin/flow-status",
            json={"run_id": "run-test", "updates": {"TEST1": {"state": "WAITING"}}},
        )
        self.client.post("/api/admin/flow-heartbeat", json={"run_id": "run-test"})

        status = self.client.post("/api/status", json={"role": "TEST1", "session_id": "/c/1"}).json()
        self.assertEqual(status["flow_status"], {"run_id": "run-test", "state": "WAITING", "stalled": False, "sent_to": "B"})

    def test_flow_status_cleanup_cannot_clear_a_newer_run(self):
        self.client.post(
            "/api/admin/flow-status",
            json={"run_id": "old-run", "updates": {"TEST1": {"state": "RUNNING"}}},
        )
        self.client.post(
            "/api/admin/flow-status",
            json={"run_id": "new-run", "activate": True, "updates": {"TEST1": {"state": "WAITING"}}},
        )
        self.client.post("/api/admin/flow-heartbeat", json={"run_id": "new-run"})

        stale_cleanup = self.client.post(
            "/api/admin/flow-status",
            json={"run_id": "old-run", "updates": {"TEST1": None}},
        )

        self.assertEqual(stale_cleanup.status_code, 200)
        payload = self.client.post("/api/status", json={"role": "TEST1", "session_id": "/c/1"}).json()
        self.assertEqual(payload["flow_status"], {"run_id": "new-run", "state": "WAITING", "stalled": False})

        self.client.post(
            "/api/admin/flow-status",
            json={"run_id": "new-run", "updates": {"TEST1": None}},
        )
        cleared = self.client.post("/api/status", json={"role": "TEST1", "session_id": "/c/1"}).json()
        self.assertIsNone(cleared["flow_status"])

    def test_flow_status_stale_update_cannot_replace_a_newer_run(self):
        self.client.post(
            "/api/admin/flow-status",
            json={"run_id": "old-run", "updates": {"TEST1": {"state": "RUNNING"}}},
        )
        self.client.post(
            "/api/admin/flow-status",
            json={"run_id": "new-run", "activate": True, "updates": {"TEST1": {"state": "WAITING"}}},
        )
        self.client.post("/api/admin/flow-heartbeat", json={"run_id": "new-run"})

        self.client.post(
            "/api/admin/flow-status",
            json={
                "run_id": "old-run",
                "updates": {
                    "TEST1": {"state": "RUNNING", "detail_label": "From", "detail_role": "OLD"}
                },
            },
        )

        payload = self.client.post("/api/status", json={"role": "TEST1", "session_id": "/c/1"}).json()
        self.assertEqual(payload["flow_status"], {"run_id": "new-run", "state": "WAITING", "stalled": False})

    def test_flow_status_rejects_invalid_state_without_touching_other_roles(self):
        response = self.client.post(
            "/api/admin/flow-status",
            json={
                "run_id": "run-test",
                "updates": {
                    "TEST1": {"state": "QUEUED"},
                    "TEST2": {"state": "WAITING"},
                },
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertNotIn("TEST1", controller.state.flow_statuses)
        self.assertNotIn("TEST2", controller.state.flow_statuses)
        self.assertFalse(self.flow_path.exists())

    def test_legacy_flow_calls_activate_only_genuinely_new_requests(self):
        first = self.client.post(
            "/api/admin/flow-status",
            json={"run_id": "legacy-1", "updates": {"TAB": {"state": "RUNNING"}}},
        )
        second = self.client.post(
            "/api/admin/flow-status",
            json={"run_id": "legacy-2", "updates": {"TAB": {"state": "RUNNING"}}},
        )
        delayed_first = self.client.post(
            "/api/admin/flow-status",
            json={
                "run_id": "legacy-1",
                "terminal_status": "completed",
                "updates": {"TAB": {"state": "DONE"}},
            },
        )
        retry_second = self.client.post(
            "/api/admin/flow-status",
            json={"run_id": "legacy-2", "updates": {"TAB": {"state": "RUNNING"}}},
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(delayed_first.status_code, 200)
        self.assertEqual(retry_second.status_code, 200)
        document = controller.state.flow_store.document
        self.assertEqual(document["active_request_id"], "legacy-2")
        self.assertEqual(document["requests"]["legacy-1"]["activation_order"], 1)
        self.assertEqual(document["requests"]["legacy-2"]["activation_order"], 2)
        self.assertEqual(len(document["requests"]), 2)
        self.assertEqual(controller.state.flow_statuses["TAB"]["run_id"], "legacy-2")

    def test_admin_flow_read_supports_active_specific_and_missing_request(self):
        response = self.client.post(
            "/api/admin/flow-status",
            json={
                "request_id": "req-active",
                "run_id": "run-active",
                "parent_request_id": "parent-1",
                "goal_hash": "a" * 64,
                "activate": True,
                "updates": {
                    "SHARED": {
                        "state": "RUNNING",
                        "logical_role": "PLAN",
                        "from_role": "REVIEW",
                    }
                },
            },
        )
        self.assertEqual(response.status_code, 200)
        self.client.post(
            "/api/admin/flow-status",
            json={
                "request_id": "req-other",
                "run_id": "run-other",
                "updates": {"SHARED": {"state": "DONE", "logical_role": "DEV"}},
            },
        )

        active = self.client.get("/api/admin/flow").json()
        specific = self.client.get("/api/admin/flow", params={"request_id": "req-other"}).json()
        missing = self.client.get("/api/admin/flow", params={"request_id": "missing"}).json()

        self.assertEqual(active["version"], 1)
        self.assertEqual(active["revision"], 2)
        self.assertEqual(active["active_request_id"], "req-active")
        self.assertIsNone(active["load_error"])
        self.assertEqual(active["flow"]["request_id"], "req-active")
        self.assertEqual(active["flow"]["roles"]["SHARED"]["logical_role"], "PLAN")
        self.assertEqual(specific["flow"]["request_id"], "req-other")
        self.assertIsNone(missing["flow"])

    def test_server_restart_hydrates_active_role_projection(self):
        self.client.post(
            "/api/admin/flow-status",
            json={
                "request_id": "req-restart",
                "run_id": "run-restart",
                "activate": True,
                "updates": {
                    "SHARED": {
                        "state": "RUNNING",
                        "logical_role": "REVIEW",
                        "from_role": "DEV",
                    }
                },
            },
        )
        self.client.post(
            "/api/admin/flow-heartbeat",
            json={"request_id": "req-restart", "run_id": "run-restart", "pid": 4321},
        )
        before_restart = self.client.get(
            "/api/admin/flow", params={"request_id": "req-restart"}
        ).json()
        self.assertEqual(before_restart["liveness"]["runner"]["state"], "RUNNING")
        self.assertFalse(before_restart["liveness"]["stalled"])

        controller.state = controller.DiagnosticState(flow_path=self.flow_path, task_path=self.task_path)

        restarted = self.client.get(
            "/api/admin/flow", params={"request_id": "req-restart"}
        ).json()
        self.assertEqual(restarted["flow"]["request_id"], "req-restart")
        self.assertFalse(restarted["flow"]["terminal_status"])
        self.assertEqual(restarted["liveness"]["runner"]["state"], "UNKNOWN")
        self.assertTrue(restarted["liveness"]["stalled"])
        self.assertEqual(
            restarted["liveness"]["reason"],
            "runner heartbeat unavailable for active flow",
        )
        self.assertEqual(restarted["liveness"]["role"], "SHARED")
        self.assertEqual(restarted["liveness"]["logical_role"], "REVIEW")
        self.assertEqual(restarted["liveness"]["next_action"], "recover existing flow")

        status = self.client.post(
            "/api/status",
            json={"role": "SHARED", "session_id": "/c/restart", "page_instance_id": "page-restart"},
        ).json()
        self.assertEqual(
            status["flow_status"],
            {
                "run_id": "run-restart",
                "state": "RUNNING",
                "stalled": True,
                "logical_role": "REVIEW",
                "from_role": "DEV",
            },
        )

        self.client.post(
            "/api/admin/flow-heartbeat",
            json={"request_id": "req-restart", "run_id": "run-restart", "pid": 9876},
        )
        recovered = self.client.get(
            "/api/admin/flow", params={"request_id": "req-restart"}
        ).json()
        self.assertEqual(recovered["liveness"]["runner"]["state"], "RUNNING")
        self.assertFalse(recovered["liveness"]["stalled"])
        recovered_status = self.client.post(
            "/api/status",
            json={"role": "SHARED", "session_id": "/c/restart", "page_instance_id": "page-restart"},
        ).json()
        self.assertEqual(recovered_status["flow_status"]["state"], "RUNNING")
        self.assertEqual(recovered_status["flow_status"]["logical_role"], "REVIEW")
        self.assertFalse(recovered_status["flow_status"]["stalled"])

    def test_corrupt_flow_file_blocks_only_flow_mutation_and_remains_untouched(self):
        corrupt = b'{"version":1,"requests":'
        self.flow_path.parent.mkdir(parents=True, exist_ok=True)
        self.flow_path.write_bytes(corrupt)
        controller.state = controller.DiagnosticState(flow_path=self.flow_path, task_path=self.task_path)

        read = self.client.get("/api/admin/flow")
        mutation = self.client.post(
            "/api/admin/flow-status",
            json={"run_id": "run-test", "updates": {"TEST1": {"state": "RUNNING"}}},
        )
        config = self.client.get("/api/admin/config")

        self.assertEqual(read.status_code, 200)
        self.assertEqual(read.json()["load_error"]["code"], "invalid_flow_file")
        self.assertIsNone(read.json()["flow"])
        self.assertEqual(mutation.status_code, 409)
        self.assertEqual(config.status_code, 200)
        self.assertEqual(self.flow_path.read_bytes(), corrupt)

    def test_admin_command_and_result_endpoints(self):
        self.client.post(
            "/api/status",
            json={"role": "A", "session_id": "sess-admin", "page_instance_id": "page-admin"},
        )
        create_response = self.client.post(
            "/api/admin/command",
            json={
                "role": "A",
                "action": "PROBE",
                "payload": {"depth": 1},
            },
        )
        self.assertEqual(create_response.status_code, 200)
        command = create_response.json()["command"]
        self.assertEqual(command["action"], "PROBE")

        command_id = command["command_id"]
        self.client.post(
            "/api/status",
            json={"role": "A", "session_id": "sess-admin", "page_instance_id": "page-admin"},
        )
        self.client.post(
            "/api/report",
            json={
                "role": "A",
                "session_id": "sess-admin",
                "page_instance_id": "page-admin",
                "command_id": command_id,
                "state": "PROBE_DONE",
                "text": "probe ok",
                "result": {"composer": True},
                "dom_info": {"composer": True, "composer_text_len": 0},
            },
        )

        result_response = self.client.get(f"/api/admin/command/{command_id}")
        self.assertEqual(result_response.status_code, 200)
        result_payload = result_response.json()
        self.assertEqual(result_payload["status"], "PROBE_DONE")
        self.assertEqual(result_payload["result"]["state"], "PROBE_DONE")

    def test_admin_live_browser_actions_reject_offline_without_delayed_command_after_recovery(self):
        for action in ("CLEAR_COMPOSER_TEXT", "RELOAD_PAGE"):
            rejected = self.client.post(
                "/api/admin/command",
                json={"role": "OFFLINE-A", "action": action, "payload": {}},
            )
            self.assertEqual(rejected.status_code, 409)
            self.assertIn("bridge is offline", rejected.json()["detail"])
            self.assertIsNone(controller.state.active_command("OFFLINE-A"))
            self.assertEqual(controller.state.command_status, {})

        self.client.post(
            "/api/status",
            json={"role": "OFFLINE-A", "session_id": "sess-recovered", "page_instance_id": "page-recovered"},
        )
        self.assertIsNone(controller.state.active_command("OFFLINE-A"))
        accepted = self.client.post(
            "/api/admin/command",
            json={"role": "OFFLINE-A", "action": "RELOAD_PAGE", "payload": {}},
        )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()["command"]["status"], "PENDING")

    def test_admin_non_bridge_command_flow_is_not_blocked_by_role_presence(self):
        accepted = self.client.post(
            "/api/admin/command",
            json={"role": "OFFLINE-SERVER", "action": "server_bookkeeping", "payload": {}},
        )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()["command"]["action"], "server_bookkeeping")

    def test_fail_closed_browser_states_are_terminal_command_results(self):
        for state_name in (
            "SEND_BLOCKED_OWNERSHIP_LOST",
            "PASTE_BLOCKED_MANUAL_INPUT",
            "MANUAL_INPUT_PENDING",
            "CHOICE_PROMPT_CLICKED",
            "CHOICE_PROMPT_CLICK_FAILED",
            "CHOICE_PROMPT_NOT_FOUND",
            "COMPOSER_TEXT_CLEARED",
            "COMPOSER_TEXT_CLEAR_FAILED",
        ):
            command = controller.state.create_command("A", "TEST", {})
            self.client.post(
                "/api/status",
                json={"role": "A", "session_id": "sess-terminal", "page_instance_id": f"page-{state_name}"},
            )
            self.client.post(
                "/api/report",
                json={
                    "role": "A",
                    "session_id": "sess-terminal",
                    "page_instance_id": f"page-{state_name}",
                    "command_id": command["command_id"],
                    "state": state_name,
                    "result": {},
                    "dom_info": {},
                },
            )

            result_payload = self.client.get(f"/api/admin/command/{command['command_id']}").json()
            self.assertTrue(result_payload["done"], state_name)
            self.assertEqual(result_payload["status"], state_name)

    def test_role_change_outcomes_are_terminal_under_original_owner_role(self):
        for state_name in ("ROLE_SET", "ROLE_TAKEOVER_RELOADING", "ROLE_TAKEOVER_FAILED"):
            with self.subTest(state=state_name):
                command = controller.state.create_command("A", "TAKEOVER_ROLE", {"role": "B"})
                owner = self.client.post(
                    "/api/status",
                    json={
                        "role": "A",
                        "session_id": "/c/owner",
                        "page_instance_id": "page-a",
                    },
                ).json()
                rival = self.client.post(
                    "/api/status",
                    json={
                        "role": "A",
                        "session_id": "/c/rival",
                        "page_instance_id": "page-b",
                    },
                ).json()
                rival_report = self.client.post(
                    "/api/report",
                    json={
                        "role": "A",
                        "session_id": "/c/rival",
                        "page_instance_id": "page-b",
                        "command_id": command["command_id"],
                        "state": state_name,
                    },
                ).json()
                owner_report = self.client.post(
                    "/api/report",
                    json={
                        "role": "A",
                        "session_id": "/c/owner",
                        "page_instance_id": "page-a",
                        "command_id": command["command_id"],
                        "state": state_name,
                    },
                ).json()
                result_payload = self.client.get(f"/api/admin/command/{command['command_id']}").json()

                self.assertEqual(owner["command"]["command_id"], command["command_id"])
                self.assertEqual(rival["command"]["action"], "WAIT")
                self.assertEqual(rival_report["status"], "IGNORED")
                self.assertEqual(rival_report["reason"], "command_owner_mismatch")
                self.assertEqual(owner_report["status"], "OK")
                self.assertTrue(result_payload["done"])
                self.assertEqual(result_payload["status"], state_name)
                self.assertNotEqual(controller.state.command_status[command["command_id"]], "DELIVERED")

    def test_admin_role_snapshot_and_events(self):
        controller.state.create_command("A", "PROBE", {"depth": 1})
        self.client.post(
            "/api/status",
            json={
                "role": "A",
                "session_id": "sess-2",
                "dom_info": {"composer": True, "composer_text_len": 5},
            },
        )

        role_response = self.client.get("/api/admin/role/A")
        self.assertEqual(role_response.status_code, 200)
        role_payload = role_response.json()
        self.assertEqual(role_payload["role"], "A")
        self.assertEqual(role_payload["status"], "ONLINE")
        self.assertTrue(role_payload["online"])
        self.assertIsNotNone(role_payload["last_seen_age_s"])
        self.assertEqual(role_payload["dom_info"]["composer_text_len"], 5)

        events_response = self.client.get("/api/admin/events?role=A&limit=5")
        self.assertEqual(events_response.status_code, 200)
        events_payload = events_response.json()
        self.assertGreaterEqual(len(events_payload["events"]), 1)

    def test_admin_role_timeline_filters_noise_before_limit_and_keeps_raw_events_separate(self):
        for index in range(120):
            controller.state.log("A", "SYNC", sequence=index)
        controller.state.update_flow_statuses(
            "run-timeline",
            {"A": {"state": "RUNNING", "logical_role": "DEV", "from_role": "User"}},
            request_id="request-timeline",
            activate=True,
        )
        controller.state.log("A", "COMMAND_CREATED", command_id="cmd-1", action="RELOAD_PAGE")
        controller.state.log("A", "ASSISTANT_DONE", command_id="cmd-2")
        controller.state.log("B", "ASSISTANT_DONE", command_id="cmd-other")

        timeline = self.client.get("/api/admin/role/A/timeline?limit=3")

        self.assertEqual(timeline.status_code, 200)
        payload = timeline.json()
        self.assertEqual(payload["role"], "A")
        self.assertEqual(
            [event["event"] for event in payload["events"]],
            ["FLOW_RUNNING", "COMMAND_CREATED", "ASSISTANT_DONE"],
        )
        self.assertTrue(all(event["role"] == "A" for event in payload["events"]))
        self.assertGreaterEqual(payload["omitted_event_count"], 120)

        raw = self.client.get("/api/admin/events?role=A&limit=200").json()["events"]
        self.assertTrue(any(event["event"] == "SYNC" for event in raw))

    def test_admin_role_marks_never_seen_numbered_role_offline(self):
        role_response = self.client.get("/api/admin/role/REVIEW1")

        role_payload = role_response.json()
        self.assertEqual(role_payload["status"], "OFFLINE")
        self.assertFalse(role_payload["online"])
        self.assertIsNone(role_payload["last_seen_age_s"])

    def test_dashboard_serves_repository_owned_html_file(self):
        self.assertEqual(
            controller.DASHBOARD_PATH,
            Path(controller.__file__).resolve().with_name("dashboard.html"),
        )

        response = self.client.get("/dashboard")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/html"))
        self.assertIn("<title>Stable Flow Runtime</title>", response.text)
        self.assertIn('id="dashboard-root"', response.text)
        self.assertIn('data-dashboard-version="12"', response.text)

    def test_admin_routes_lists_samples(self):
        response = self.client.get("/api/admin/routes")
        self.assertEqual(response.status_code, 200)
        routes = response.json()["routes"]
        samples = [route["sample"] for route in routes]

        self.assertIn("/api/admin/role/A", samples)
        self.assertIn("/api/admin/role/A/timeline?limit=20", samples)
        self.assertIn("/api/admin/events?role=A&limit=20", samples)
        self.assertIn("/api/admin/tasks/launch", samples)
        dashboard_route = next(route for route in routes if route["path"] == "/dashboard")
        self.assertEqual(dashboard_route["method"], "GET")
        self.assertEqual(dashboard_route["group"], "presentation")
        self.assertEqual(dashboard_route["sample"], "/dashboard")
        self.assertEqual({"client", "admin", "tasks", "presentation"}, {route["group"] for route in routes})

    def test_startup_log_lists_backend_api_samples(self):
        with patch("builtins.print") as print_mock:
            controller.log_startup_routes("http://127.0.0.1:8500")

        output = "\n".join(str(call.args[0]) for call in print_mock.call_args_list)
        self.assertIn("backend API", output)
        self.assertIn("http://127.0.0.1:8500/api/admin/role/A", output)
        self.assertIn("http://127.0.0.1:8500/api/admin/routes", output)
        self.assertIn("http://127.0.0.1:8500/dashboard", output)

    def test_admin_config_get_and_partial_update(self):
        get_response = self.client.get("/api/admin/config")
        self.assertEqual(get_response.status_code, 200)
        original = get_response.json()["config"]
        self.assertIn("poll_ms", original)
        self.assertIn("action_delay_min_ms", original)
        self.assertIn("send_accept_timeout_ms", original)
        self.assertIn("send_accept_poll_ms", original)
        self.assertIn("assistant_post_stop_timeout_ms", original)

        update_response = self.client.post(
            "/api/admin/config",
            json={
                "config": {
                    "poll_ms": 1234,
                    "send_delay_min_ms": 2222,
                }
            },
        )
        self.assertEqual(update_response.status_code, 200)
        updated = update_response.json()["config"]
        self.assertEqual(updated["poll_ms"], 1234)
        self.assertEqual(updated["send_delay_min_ms"], 2222)
        self.assertEqual(updated["action_delay_min_ms"], original["action_delay_min_ms"])

        verify_response = self.client.get("/api/admin/config")
        self.assertEqual(verify_response.status_code, 200)
        verified = verify_response.json()["config"]
        self.assertEqual(verified["poll_ms"], 1234)
        self.assertEqual(verified["send_delay_min_ms"], 2222)

    def test_status_ignores_sentinel_frame_for_command_delivery(self):
        controller.state.create_command("PROBE", {"depth": 1})

        response = self.client.post(
            "/api/status",
            json={
                "role": "A",
                "session_id": "/backend-api/sentinel/frame.html",
                "dom_info": {"composer": False, "composer_text_len": 0},
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["command"]["action"], "WAIT")
        self.assertEqual(controller.state.status["A"], "OFFLINE")
        self.assertEqual(controller.state.dom_info["A"], {})

    def test_report_and_sync_ignore_sentinel_dom_overwrite(self):
        command = controller.state.create_command("A", "PROBE", {})
        self.client.post(
            "/api/status",
            json={"role": "A", "session_id": "/c/abc", "page_instance_id": "page-real"},
        )
        self.client.post(
            "/api/report",
            json={
                "role": "A",
                "session_id": "/c/abc",
                "page_instance_id": "page-real",
                "command_id": command["command_id"],
                "state": "PROBE_DONE",
                "text": "",
                "result": {"composer": True},
                "dom_info": {"composer": True, "composer_text_len": 1},
            },
        )

        self.client.post(
            "/api/report",
            json={
                "role": "A",
                "session_id": "/backend-api/sentinel/frame.html",
                "page_instance_id": "sentinel-page",
                "command_id": command["command_id"],
                "state": "PROBE_DONE",
                "text": "",
                "result": {"composer": False},
                "dom_info": {"composer": False, "composer_text_len": 0},
            },
        )

        self.client.post(
            "/api/sync",
            json={
                "role": "A",
                "session_id": "/backend-api/sentinel/frame.html",
                "page_instance_id": "sentinel-page",
                "reason": "sentinel",
                "transcript": {
                    "messages": [{"role": "assistant", "text": "ignored"}],
                    "last_user": {"text": "ignored user"},
                    "last_assistant": {"text": "ignored assistant"},
                    "counts": {"assistant": 1},
                },
                "snapshot": {"composer": False, "composer_text_len": 0},
            },
        )

        self.assertEqual(controller.state.dom_info["A"]["composer"], True)
        self.assertEqual(controller.state.dom_info["A"]["composer_text_len"], 1)
        self.assertEqual(controller.state.last_user_message["A"], "")
        self.assertEqual(controller.state.last_response["A"], "")

    def test_empty_sync_clears_stale_transcript_state(self):
        self.client.post(
            "/api/sync",
            json={
                "role": "DEV",
                "session_id": "/c/old",
                "reason": "initial",
                "transcript": {
                    "messages": [{"role": "user", "text": "old prompt"}],
                    "last_user": {"text": "old prompt"},
                    "last_assistant": {"text": "old answer"},
                    "counts": {"user": 1, "assistant": 1},
                },
                "snapshot": {"messages": {"messages": [], "counts": {}}},
            },
        )

        self.client.post(
            "/api/sync",
            json={
                "role": "DEV",
                "session_id": "/",
                "reason": "empty_chat",
                "transcript": {
                    "messages": [],
                    "last_user": None,
                    "last_assistant": None,
                    "counts": {},
                },
                "snapshot": {"messages": {"messages": [], "counts": {}}},
            },
        )

        self.assertEqual(controller.state.transcripts["DEV"], [])
        self.assertEqual(controller.state.last_user_message["DEV"], "")
        self.assertEqual(controller.state.last_response["DEV"], "")

    def test_empty_status_clears_stale_transcript_state(self):
        self.client.post(
            "/api/sync",
            json={
                "role": "DEV2",
                "session_id": "/c/old",
                "reason": "initial",
                "transcript": {
                    "messages": [{"role": "user", "text": "old prompt"}],
                    "last_user": {"text": "old prompt"},
                    "last_assistant": {"text": "old answer"},
                    "counts": {"user": 1, "assistant": 1},
                },
                "snapshot": {"messages": {"messages": [{"role": "user", "text": "old prompt"}], "counts": {"user": 1, "assistant": 1}}},
            },
        )

        self.client.post(
            "/api/status",
            json={
                "role": "DEV2",
                "session_id": "/",
                "dom_info": {
                    "composer_text_len": 0,
                    "stop_visible": False,
                    "messages": {"messages": [], "counts": {"user": 0, "assistant": 0}},
                },
            },
        )

        role_response = self.client.get("/api/admin/role/DEV2")
        role_payload = role_response.json()
        self.assertEqual(role_payload["last_user"], "")
        self.assertEqual(role_payload["last_response"], "")
        self.assertEqual(controller.state.transcripts["DEV2"], [])

    def test_status_replaces_stale_transcript_state_from_current_dom(self):
        self.client.post(
            "/api/sync",
            json={
                "role": "DEV",
                "session_id": "/c/old",
                "reason": "initial",
                "transcript": {
                    "messages": [
                        {"role": "user", "text": "old prompt"},
                        {"role": "assistant", "text": "old answer"},
                    ],
                    "last_user": {"text": "old prompt"},
                    "last_assistant": {"text": "old answer"},
                    "counts": {"user": 1, "assistant": 1},
                },
                "snapshot": {
                    "messages": {
                        "messages": [
                            {"role": "user", "text": "old prompt"},
                            {"role": "assistant", "text": "old answer"},
                        ],
                        "counts": {"user": 1, "assistant": 1},
                        "last_user": {"text": "old prompt"},
                        "last_assistant": {"text": "old answer"},
                    }
                },
            },
        )

        self.client.post(
            "/api/status",
            json={
                "role": "DEV",
                "session_id": "/c/new",
                "dom_info": {
                    "composer_text_len": 0,
                    "stop_visible": False,
                    "messages": {
                        "messages": [
                            {"role": "user", "text": "new prompt"},
                            {"role": "assistant", "text": "new answer"},
                        ],
                        "counts": {"user": 1, "assistant": 1},
                        "last_user": {"text": "new prompt"},
                        "last_assistant": {"text": "new answer"},
                    },
                },
            },
        )

        role_response = self.client.get("/api/admin/role/DEV")
        role_payload = role_response.json()
        self.assertEqual(role_payload["last_user"], "new prompt")
        self.assertEqual(role_payload["last_response"], "new answer")
        self.assertEqual(
            controller.state.transcripts["DEV"],
            [
                {"role": "user", "text": "new prompt"},
                {"role": "assistant", "text": "new answer"},
            ],
        )

    def test_observation_sequence_rejects_out_of_order_same_page(self):
        newer = self.client.post(
            "/api/status",
            json={
                "role": "DEV",
                "session_id": "/c/current",
                "page_instance_id": "page-current",
                "observation_seq": 2,
                "dom_info": {
                    "messages": {
                        "messages": [{"role": "assistant", "text": "new"}],
                        "counts": {"assistant": 1},
                        "last_assistant": {"text": "new"},
                    }
                },
            },
        ).json()
        stale = self.client.post(
            "/api/sync",
            json={
                "role": "DEV",
                "session_id": "/c/current",
                "page_instance_id": "page-current",
                "observation_seq": 1,
                "snapshot": {
                    "messages": {
                        "messages": [{"role": "assistant", "text": "old"}],
                        "counts": {"assistant": 1},
                        "last_assistant": {"text": "old"},
                    }
                },
            },
        ).json()

        self.assertTrue(newer["observation_accepted"])
        self.assertFalse(stale["observation_accepted"])
        self.assertEqual(stale["observation_reason"], "stale_observation_seq")
        self.assertEqual(controller.state.last_response["DEV"], "new")

    def test_replaced_page_rejects_delayed_old_observation(self):
        self.client.post(
            "/api/status",
            json={
                "role": "PLAN",
                "session_id": "/c/old",
                "page_instance_id": "page-old",
                "role_owner_id": "tab-1",
                "role_claim_id": "g-1-claim",
                "claim_role": True,
                "observation_seq": 8,
                "dom_info": {"messages": {"messages": [], "counts": {}}},
            },
        )
        current = self.client.post(
            "/api/status",
            json={
                "role": "PLAN",
                "session_id": "/c/new",
                "page_instance_id": "page-new",
                "role_owner_id": "tab-1",
                "role_claim_id": "g-1-claim",
                "observation_seq": 1,
                "dom_info": {
                    "messages": {
                        "messages": [{"role": "assistant", "text": "current"}],
                        "counts": {"assistant": 1},
                        "last_assistant": {"text": "current"},
                    }
                },
            },
        ).json()
        delayed = self.client.post(
            "/api/sync",
            json={
                "role": "PLAN",
                "session_id": "/c/old",
                "page_instance_id": "page-old",
                "role_owner_id": "tab-1",
                "role_claim_id": "g-1-claim",
                "observation_seq": 9,
                "snapshot": {
                    "messages": {
                        "messages": [{"role": "assistant", "text": "stale"}],
                        "counts": {"assistant": 1},
                        "last_assistant": {"text": "stale"},
                    }
                },
            },
        ).json()

        self.assertTrue(current["observation_accepted"])
        self.assertFalse(delayed["observation_accepted"])
        self.assertEqual(delayed["observation_reason"], "stale_page_instance_id")
        snapshot = self.client.get("/api/admin/role/PLAN").json()
        self.assertEqual(snapshot["observation"]["page_instance_id"], "page-new")
        self.assertEqual(snapshot["last_response"], "current")

    def test_retired_claiming_page_is_cleared_without_state_mutation(self):
        self.client.post(
            "/api/status",
            json={
                "role": "PLAN",
                "session_id": "/c/old",
                "page_instance_id": "page-old",
                "role_owner_id": "tab-1",
                "role_claim_id": "g-1-claim",
                "claim_role": True,
                "observation_seq": 8,
                "dom_info": {
                    "messages": {
                        "messages": [{"role": "assistant", "text": "old"}],
                        "counts": {"assistant": 1},
                        "last_assistant": {"text": "old"},
                    }
                },
            },
        )
        current = self.client.post(
            "/api/status",
            json={
                "role": "PLAN",
                "session_id": "/c/new",
                "page_instance_id": "page-new",
                "role_owner_id": "tab-1",
                "role_claim_id": "g-1-claim",
                "claim_role": True,
                "observation_seq": 1,
                "dom_info": {
                    "messages": {
                        "messages": [
                            {"role": "user", "text": "current prompt"},
                            {"role": "assistant", "text": "current answer"},
                        ],
                        "counts": {"user": 1, "assistant": 1},
                        "last_user": {"text": "current prompt"},
                        "last_assistant": {"text": "current answer"},
                    }
                },
            },
        ).json()
        controller.state.flow_statuses["PLAN"] = {"state": "RUNNING", "detail": "phase-01"}

        before = {
            "owner": dict(controller.state.role_owners["PLAN"]),
            "sessions": set(controller.state.sessions["PLAN"]),
            "current_session": controller.state.current_sessions["PLAN"],
            "status": controller.state.status["PLAN"],
            "seen_at": controller.state.role_seen_at["PLAN"],
            "observation": dict(controller.state.observation_pages["PLAN"]),
            "retired_pages": set(controller.state.retired_observation_pages["PLAN"]),
            "dom": dict(controller.state.dom_info["PLAN"]),
            "transcript": list(controller.state.transcripts["PLAN"]),
            "last_user": controller.state.last_user_message["PLAN"],
            "last_response": controller.state.last_response["PLAN"],
            "commands": dict(controller.state.commands),
            "command_status": dict(controller.state.command_status),
            "command_results": dict(controller.state.command_results),
            "flow": dict(controller.state.flow_statuses["PLAN"]),
        }

        retired = self.client.post(
            "/api/status",
            json={
                "role": "PLAN",
                "session_id": "/c/old-returned",
                "page_instance_id": "page-old",
                "role_owner_id": "tab-1",
                "role_claim_id": "g-1-claim",
                "claim_role": True,
                "observation_seq": 99,
                "dom_info": {
                    "messages": {
                        "messages": [{"role": "assistant", "text": "conflicting stale"}],
                        "counts": {"assistant": 1},
                        "last_assistant": {"text": "conflicting stale"},
                    }
                },
            },
        ).json()

        self.assertTrue(current["observation_accepted"])
        self.assertTrue(retired["clear_role"])
        self.assertEqual(retired["command"]["action"], "WAIT")
        self.assertFalse(retired["observation_accepted"])
        self.assertEqual(retired["observation_reason"], "stale_page_instance_id")
        self.assertIsNone(retired["flow_status"])
        self.assertEqual(controller.state.role_owners["PLAN"]["page_instance_id"], "page-new")
        self.assertEqual(controller.state.role_owners["PLAN"]["session_id"], "/c/new")
        self.assertEqual(controller.state.role_owners["PLAN"], before["owner"])
        self.assertEqual(controller.state.sessions["PLAN"], before["sessions"])
        self.assertEqual(controller.state.current_sessions["PLAN"], before["current_session"])
        self.assertEqual(controller.state.status["PLAN"], before["status"])
        self.assertEqual(controller.state.role_seen_at["PLAN"], before["seen_at"])
        self.assertEqual(controller.state.observation_pages["PLAN"], before["observation"])
        self.assertEqual(controller.state.retired_observation_pages["PLAN"], before["retired_pages"])
        self.assertEqual(controller.state.dom_info["PLAN"], before["dom"])
        self.assertEqual(controller.state.transcripts["PLAN"], before["transcript"])
        self.assertEqual(controller.state.last_user_message["PLAN"], before["last_user"])
        self.assertEqual(controller.state.last_response["PLAN"], before["last_response"])
        self.assertEqual(controller.state.commands, before["commands"])
        self.assertEqual(controller.state.command_status, before["command_status"])
        self.assertEqual(controller.state.command_results, before["command_results"])
        self.assertEqual(controller.state.flow_statuses["PLAN"], before["flow"])
        admin = self.client.get("/api/admin/role/PLAN").json()
        self.assertEqual(admin["observation"]["page_instance_id"], "page-new")
        self.assertEqual(admin["observation"]["observation_seq"], 1)
        self.assertEqual(admin["last_user"], "current prompt")
        self.assertEqual(admin["last_response"], "current answer")

    def test_stale_current_session_sync_refreshes_only_presence(self):
        accepted_snapshot = {
            "messages": {
                "messages": [
                    {"role": "user", "text": "accepted prompt"},
                    {"role": "assistant", "text": "accepted answer"},
                ],
                "counts": {"user": 1, "assistant": 1},
                "last_user": {"text": "accepted prompt"},
                "last_assistant": {"text": "accepted answer"},
            }
        }
        self.client.post(
            "/api/status",
            json={
                "role": "DEV",
                "session_id": "/c/dev",
                "page_instance_id": "page-dev",
                "role_owner_id": "tab-dev",
                "role_claim_id": "g-2-claim",
                "claim_role": True,
                "observation_seq": 5,
                "dom_info": accepted_snapshot,
            },
        )
        stale_seen_at = time.time() - 460
        controller.state.role_seen_at["DEV"] = stale_seen_at
        before = {
            "observation": dict(controller.state.observation_pages["DEV"]),
            "dom": dict(controller.state.dom_info["DEV"]),
            "transcript": list(controller.state.transcripts["DEV"]),
            "last_user": controller.state.last_user_message["DEV"],
            "last_response": controller.state.last_response["DEV"],
        }

        stale = self.client.post(
            "/api/sync",
            json={
                "role": "DEV",
                "session_id": "/c/dev",
                "page_instance_id": "page-dev",
                "role_owner_id": "tab-dev",
                "role_claim_id": "g-2-claim",
                "observation_seq": 5,
                "reason": "periodic",
                "snapshot": {
                    "messages": {
                        "messages": [{"role": "assistant", "text": "conflicting snapshot"}],
                        "counts": {"assistant": 1},
                        "last_assistant": {"text": "conflicting snapshot"},
                    }
                },
                "transcript": {
                    "messages": [{"role": "assistant", "text": "conflicting transcript"}],
                    "last_user": {"text": "conflicting user"},
                    "last_assistant": {"text": "conflicting transcript"},
                },
            },
        ).json()

        self.assertEqual(stale["status"], "OK")
        self.assertFalse(stale["observation_accepted"])
        self.assertEqual(stale["observation_reason"], "stale_observation_seq")
        admin = self.client.get("/api/admin/role/DEV").json()
        self.assertTrue(admin["presence"]["online"])
        self.assertEqual(admin["presence"]["status"], "ONLINE")
        self.assertLess(admin["presence"]["last_seen_age_s"], 2)
        self.assertGreater(controller.state.role_seen_at["DEV"], stale_seen_at)
        self.assertEqual(controller.state.observation_pages["DEV"], before["observation"])
        self.assertEqual(controller.state.dom_info["DEV"], before["dom"])
        self.assertEqual(controller.state.transcripts["DEV"], before["transcript"])
        self.assertEqual(controller.state.last_user_message["DEV"], before["last_user"])
        self.assertEqual(controller.state.last_response["DEV"], before["last_response"])

    def test_terminal_command_accepts_result_when_observation_is_stale(self):
        command = controller.state.create_command("A", "WAIT_ASSISTANT_DONE", {})
        self.client.post(
            "/api/status",
            json={
                "role": "A",
                "session_id": "/c/a",
                "page_instance_id": "page-a",
                "observation_seq": 5,
                "dom_info": {
                    "messages": {
                        "messages": [{"role": "assistant", "text": "fresh"}],
                        "counts": {"assistant": 1},
                        "last_assistant": {"text": "fresh"},
                    }
                },
            },
        )
        terminal = self.client.post(
            "/api/report",
            json={
                "role": "A",
                "session_id": "/c/a",
                "page_instance_id": "page-a",
                "command_id": command["command_id"],
                "state": "ASSISTANT_DONE",
                "text": "done",
                "observation_seq": 4,
                "dom_info": {
                    "messages": {
                        "messages": [{"role": "assistant", "text": "stale"}],
                        "counts": {"assistant": 1},
                        "last_assistant": {"text": "stale"},
                    }
                },
            },
        ).json()

        self.assertEqual(terminal["status"], "OK")
        self.assertFalse(terminal["observation_accepted"])
        result = controller.state.command_results[command["command_id"]]
        self.assertEqual(result["state"], "ASSISTANT_DONE")
        self.assertFalse(result["observation_accepted"])
        self.assertEqual(result["dom_info"], {})
        self.assertEqual(controller.state.last_response["A"], "fresh")

    def test_newer_empty_observation_clears_and_stale_empty_does_not(self):
        populated = {
            "messages": {
                "messages": [{"role": "assistant", "text": "old"}],
                "counts": {"assistant": 1},
                "last_assistant": {"text": "old"},
            }
        }
        empty = {"messages": {"messages": [], "counts": {"user": 0, "assistant": 0}}}
        self.client.post(
            "/api/status",
            json={
                "role": "DEV",
                "session_id": "/c/dev",
                "page_instance_id": "page-dev",
                "observation_seq": 1,
                "dom_info": populated,
            },
        )
        cleared = self.client.post(
            "/api/sync",
            json={
                "role": "DEV",
                "session_id": "/c/dev",
                "page_instance_id": "page-dev",
                "observation_seq": 2,
                "snapshot": empty,
                "transcript": {"messages": [], "last_user": None, "last_assistant": None},
            },
        ).json()
        self.assertTrue(cleared["observation_accepted"])
        self.assertEqual(controller.state.last_response["DEV"], "")

        self.client.post(
            "/api/sync",
            json={
                "role": "DEV",
                "session_id": "/c/dev",
                "page_instance_id": "page-dev",
                "observation_seq": 3,
                "snapshot": {
                    "messages": {
                        "messages": [{"role": "assistant", "text": "new"}],
                        "counts": {"assistant": 1},
                        "last_assistant": {"text": "new"},
                    }
                },
            },
        )
        stale_empty = self.client.post(
            "/api/sync",
            json={
                "role": "DEV",
                "session_id": "/c/dev",
                "page_instance_id": "page-dev",
                "observation_seq": 2,
                "snapshot": empty,
                "transcript": {"messages": [], "last_user": None, "last_assistant": None},
            },
        ).json()
        self.assertFalse(stale_empty["observation_accepted"])
        self.assertEqual(controller.state.last_response["DEV"], "new")

    def test_all_observation_ingress_uses_shared_mutation_helper(self):
        command = controller.state.create_command("A", "WAIT_ASSISTANT_DONE", {})
        with patch.object(
            controller.state,
            "apply_role_observation",
            wraps=controller.state.apply_role_observation,
        ) as apply_observation:
            self.client.post(
                "/api/status",
                json={
                    "role": "A",
                    "session_id": "/c/a",
                    "page_instance_id": "page-a",
                    "observation_seq": 1,
                    "dom_info": {"composer": True},
                },
            )
            self.client.post(
                "/api/report",
                json={
                    "role": "A",
                    "session_id": "/c/a",
                    "page_instance_id": "page-a",
                    "command_id": command["command_id"],
                    "state": "ASSISTANT_PROGRESS",
                    "observation_seq": 2,
                    "dom_info": {"composer": True},
                },
            )
            self.client.post(
                "/api/sync",
                json={
                    "role": "A",
                    "session_id": "/c/a",
                    "page_instance_id": "page-a",
                    "observation_seq": 3,
                    "snapshot": {"composer": True},
                    "transcript": {"messages": []},
                },
            )

        self.assertEqual(apply_observation.call_count, 3)

    def test_admin_role_separates_presence_observation_and_command_state(self):
        command = controller.state.create_command("A", "WAIT_ASSISTANT_DONE", {})
        self.client.post(
            "/api/status",
            json={
                "role": "A",
                "session_id": "/c/a",
                "page_instance_id": "page-a",
                "observation_seq": 1,
                "dom_info": {"composer": True},
            },
        )
        self.client.post(
            "/api/report",
            json={
                "role": "A",
                "session_id": "/c/a",
                "page_instance_id": "page-a",
                "command_id": command["command_id"],
                "state": "ASSISTANT_PROGRESS",
                "observation_seq": 2,
                "dom_info": {"composer": True},
            },
        )

        online = self.client.get("/api/admin/role/A").json()
        self.assertEqual(online["status"], "ONLINE")
        self.assertTrue(online["presence"]["online"])
        self.assertEqual(online["observation"]["observation_seq"], 2)
        self.assertEqual(online["active_command"]["state"], "ASSISTANT_PROGRESS")
        self.assertEqual(online["dom_info"], online["observation"]["dom_info"])

        controller.state.role_seen_at["A"] = time.time() - 460
        offline = self.client.get("/api/admin/role/A").json()
        self.assertEqual(offline["status"], "OFFLINE")
        self.assertFalse(offline["presence"]["online"])
        self.assertEqual(offline["active_command"]["state"], "ASSISTANT_PROGRESS")

    @patch("server.time.sleep")
    @patch("server.os.kill")
    @patch("server.find_pid_on_port")
    def test_ensure_port_available_kills_existing_owner(self, find_pid, os_kill, sleep_mock):
        find_pid.side_effect = [4321, None]

        controller.ensure_port_available("127.0.0.1", 8500)

        os_kill.assert_called_once_with(4321, controller.signal.SIGTERM)
        sleep_mock.assert_called_once()

    @patch("server.os.kill")
    @patch("server.find_pid_on_port", return_value=None)
    def test_ensure_port_available_noop_when_port_is_free(self, find_pid, os_kill):
        controller.ensure_port_available("127.0.0.1", 8500)

        find_pid.assert_called_once_with("127.0.0.1", 8500)
        os_kill.assert_not_called()

    def test_claim_role_assigns_queued_role_once(self):
        controller.state.queue_auto_open_role("HERMES", "https://chatgpt.com/")

        first = self.client.post("/api/claim-role", json={"session_id": "/"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["role"], "HERMES")
        self.assertGreater(controller.state.auto_open_roles["HERMES"]["claimed_at"], 0)
        self.assertEqual(controller.state.auto_open_roles["HERMES"]["claimed_session_id"], "/")

        second = self.client.post("/api/claim-role", json={"session_id": "/c/next"})
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["role"], "")


class TaskControlPlaneTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name) / ".role_state"
        self.flow_path = root / "flow.json"
        self.task_path = root / "tasks.json"
        controller.state = controller.DiagnosticState(
            flow_path=self.flow_path,
            task_path=self.task_path,
            scheduler_poll_s=0.01,
        )
        self.client = TestClient(controller.app)

    def tearDown(self):
        controller.state.task_scheduler.stop()
        self.temp_dir.cleanup()

    @staticmethod
    def payload(**overrides):
        data = {
            "title": "Dashboard task",
            "target_root": r"E:\\target",
            "branch": "feat/task",
            "prompt": "Inspect current evidence and implement the authorized task.",
            "skill_path": "skills/ORCHESTRATOR.md",
            "controller_role": "control_x",
            "logical_roles": ["dev1", "review2", "plan_z"],
            "physical_role_map": {"dev1": "worker_a", "review2": "worker_b", "plan_z": "worker_a"},
            "finish_roles": ["plan_z"],
            "status": "READY",
            "enabled": True,
            "schedule": {"kind": "manual"},
        }
        data.update(overrides)
        return data

    def create(self, **overrides):
        response = self.client.post("/api/admin/tasks", json=self.payload(**overrides))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["task"]

    @staticmethod
    def mark_role_ready(role: str):
        normalized = role.upper()
        controller.state.role_seen_at[normalized] = time.time()
        controller.state.dom_info[normalized] = {
            "composer": True,
            "composer_text": "",
            "composer_text_len": 0,
            "composer_attachments": [],
            "stop_visible": False,
            "manual_input_pending": False,
        }

    @staticmethod
    def launch_payload(**overrides):
        data = {
            "controller_role": "C2",
            "prompt": "Implement compact live dashboard updates.",
            "logical_roles": ["C2", "REVIEW"],
            "physical_role_map": {"C2": "C2", "REVIEW": "C3"},
            "finish_roles": ["REVIEW"],
            "execution_options": {
                "timeout": 1800,
                "request_timeout": 1200,
                "parallelism": 4,
                "max_turns": 0,
                "reload_after": 10,
            },
        }
        data.update(overrides)
        return data

    @staticmethod
    def single_launch_payload(**overrides):
        data = {
            "controller_role": "C2",
            "prompt": "chỉ cần nói ok.",
            "logical_roles": ["C2"],
            "physical_role_map": {"C2": "C2"},
            "finish_roles": ["C2"],
            "execution_options": {
                "timeout": 1800,
                "request_timeout": 1200,
                "parallelism": 4,
                "max_turns": 0,
                "reload_after": 10,
            },
        }
        data.update(overrides)
        return data

    def test_compact_task_launch_starts_exact_main_process_without_task_persistence(self):
        self.mark_role_ready("C2")
        self.mark_role_ready("C3")
        process = MagicMock()
        process.pid = 43210
        process.poll.return_value = None
        launch_dir = Path(self.temp_dir.name) / "dashboard-launches"

        with (
            patch.object(controller, "DASHBOARD_LAUNCH_DIR", launch_dir),
            patch("server.shutil.which", return_value=r"C:\tools\uv.exe"),
            patch("server.subprocess.Popen", return_value=process) as popen,
            patch("server.time.sleep"),
        ):
            response = self.client.post("/api/admin/tasks/launch", json=self.launch_payload())

            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            run = body["run"]
            self.assertEqual(body["status"], "STARTED")
            self.assertEqual(run["pid"], 43210)
            self.assertTrue(run["run_id"].startswith("dashboard-"))
            self.assertEqual(run["physical_roles"], ["C2", "C3"])
            self.assertEqual(
                run["command"],
                'uv run main.py --role "C2,REVIEW" --browser-roles "C2,C3" '
                '--role-map "C2=C2 REVIEW=C3" --finish-roles "REVIEW" '
                '--timeout 1800 --request-timeout 1200 --parallelism 4 '
                '--max-turns 0 --reload-after 10 --goal "Implement compact live dashboard updates."',
            )
            argv = popen.call_args.args[0]
            self.assertEqual(
                argv,
                [
                    r"C:\tools\uv.exe", "run", "main.py",
                    "--role", "C2,REVIEW",
                    "--browser-roles", "C2,C3",
                    "--role-map", "C2=C2 REVIEW=C3",
                    "--finish-roles", "REVIEW",
                    "--timeout", "1800",
                    "--request-timeout", "1200",
                    "--parallelism", "4",
                    "--max-turns", "0",
                    "--reload-after", "10",
                    "--goal", "Implement compact live dashboard updates.",
                ],
            )
            kwargs = popen.call_args.kwargs
            self.assertEqual(kwargs["cwd"], controller.CONTROL_REPOSITORY)
            self.assertFalse(kwargs["shell"])
            for variable in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "UV_INTERNAL__PYTHONHOME", "UV_RUN_RECURSION_DEPTH"):
                self.assertNotIn(variable, kwargs["env"])
            self.assertEqual(controller.state.task_store.list_tasks(), [])

            conflict = self.client.post("/api/admin/tasks/launch", json=self.launch_payload(prompt="second"))
            self.assertEqual(conflict.status_code, 409)
            self.assertEqual(conflict.json()["detail"]["code"], "controller_busy")
            self.assertEqual(popen.call_count, 1)

    def test_compact_single_role_launch_uses_role_runner(self):
        self.mark_role_ready("C2")
        process = MagicMock()
        process.pid = 43211
        process.poll.return_value = None
        launch_dir = Path(self.temp_dir.name) / "dashboard-launches"

        with (
            patch.object(controller, "DASHBOARD_LAUNCH_DIR", launch_dir),
            patch("server.shutil.which", return_value=r"C:\tools\uv.exe"),
            patch("server.subprocess.Popen", return_value=process) as popen,
            patch("server.time.sleep"),
        ):
            response = self.client.post("/api/admin/tasks/launch", json=self.single_launch_payload())

        self.assertEqual(response.status_code, 200, response.text)
        run = response.json()["run"]
        self.assertEqual(
            run["command"],
            'uv run role.py --role "C2" --timeout 1800 --request-timeout 1200 '
            '--prompt "chỉ cần nói ok."',
        )
        self.assertEqual(
            popen.call_args.args[0],
            [
                r"C:\tools\uv.exe", "run", "role.py",
                "--role", "C2",
                "--timeout", "1800",
                "--request-timeout", "1200",
                "--prompt", "chỉ cần nói ok.",
            ],
        )
        self.assertNotIn("main.py", run["command"])
        self.assertNotIn("--finish-roles", run["command"])
        self.assertEqual(controller.state.task_store.list_tasks(), [])

    def test_compact_task_launch_rejects_offline_role_without_spawning(self):
        self.mark_role_ready("C2")
        with patch("server.subprocess.Popen") as popen:
            response = self.client.post("/api/admin/tasks/launch", json=self.launch_payload())

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "role_offline")
        popen.assert_not_called()
        self.assertEqual(controller.state.task_store.list_tasks(), [])

    def test_compact_task_launch_reports_process_start_failure(self):
        self.mark_role_ready("C2")
        self.mark_role_ready("C3")
        launch_dir = Path(self.temp_dir.name) / "dashboard-launches"
        with (
            patch.object(controller, "DASHBOARD_LAUNCH_DIR", launch_dir),
            patch("server.shutil.which", return_value=r"C:\tools\uv.exe"),
            patch("server.subprocess.Popen", side_effect=OSError("spawn blocked")),
        ):
            response = self.client.post("/api/admin/tasks/launch", json=self.launch_payload())

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "launch_failed")
        self.assertIn("spawn blocked", response.json()["detail"]["message"])
        self.assertEqual(controller.state.dashboard_processes, {})
        self.assertEqual(controller.state.task_store.list_tasks(), [])

    def test_task_crud_move_archive_and_revision_conflicts(self):
        task = self.create()
        listed = self.client.get("/api/admin/tasks").json()
        self.assertEqual(listed["tasks"][0]["task_id"], task["task_id"])
        self.assertIn("scheduler", listed)
        detail = self.client.get(f"/api/admin/tasks/{task['task_id']}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["task"]["execution_options"]["request_timeout"], 1200.0)

        options = {
            "timeout": 900,
            "request_timeout": 700,
            "parallelism": 2,
            "max_turns": 8,
            "reload_after": 4,
            "new_chat_on_handoff": True,
            "handoff_command_policy": "always",
        }
        option_changed = self.client.patch(
            f"/api/admin/tasks/{task['task_id']}",
            json={"expected_revision": task["revision"], "changes": {"execution_options": options}},
        ).json()["task"]
        self.assertEqual(option_changed["execution_options"]["parallelism"], 2)
        self.assertTrue(option_changed["execution_options"]["new_chat_on_handoff"])

        partial_options = self.client.patch(
            f"/api/admin/tasks/{task['task_id']}",
            json={"expected_revision": option_changed["revision"], "changes": {"execution_options": {"parallelism": 3}}},
        ).json()["task"]
        self.assertEqual(partial_options["execution_options"]["parallelism"], 3)
        self.assertEqual(partial_options["execution_options"]["timeout"], 900.0)
        self.assertEqual(partial_options["execution_options"]["request_timeout"], 700.0)
        self.assertEqual(partial_options["execution_options"]["handoff_command_policy"], "always")

        rejected_off = self.client.patch(
            f"/api/admin/tasks/{task['task_id']}",
            json={
                "expected_revision": partial_options["revision"],
                "changes": {"execution_options": {"handoff_command_policy": "off"}},
            },
        )
        self.assertEqual(rejected_off.status_code, 400)
        self.assertIn("auto or always", rejected_off.json()["detail"]["message"])

        changed = self.client.patch(
            f"/api/admin/tasks/{task['task_id']}",
            json={"expected_revision": partial_options["revision"], "changes": {"title": "Changed"}},
        ).json()["task"]
        stale = self.client.patch(
            f"/api/admin/tasks/{task['task_id']}",
            json={"expected_revision": option_changed["revision"], "changes": {"title": "Stale"}},
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["detail"]["code"], "stale_revision")

        wrong = self.client.post(
            f"/api/admin/tasks/{task['task_id']}/move",
            json={"expected_revision": changed["revision"], "status": "RUNNING", "actor_role": "OTHER"},
        )
        self.assertEqual(wrong.status_code, 409)
        self.assertEqual(wrong.json()["detail"]["code"], "controller_mismatch")
        running = self.client.post(
            f"/api/admin/tasks/{task['task_id']}/move",
            json={"expected_revision": changed["revision"], "status": "RUNNING", "actor_role": "CONTROL_X"},
        ).json()["task"]
        linked = self.client.patch(
            f"/api/admin/tasks/{task['task_id']}",
            json={"expected_revision": running["revision"], "actor_role": "CONTROL_X", "changes": {"active_request_id": "req-1"}},
        ).json()["task"]
        blocked_reassign = self.client.patch(
            f"/api/admin/tasks/{task['task_id']}",
            json={"expected_revision": linked["revision"], "changes": {"controller_role": "NEW_CONTROL"}},
        )
        self.assertEqual(blocked_reassign.status_code, 409)
        self.assertEqual(blocked_reassign.json()["detail"]["code"], "active_task_reassignment")
        done = self.client.post(
            f"/api/admin/tasks/{task['task_id']}/move",
            json={"expected_revision": linked["revision"], "status": "DONE", "actor_role": "CONTROL_X"},
        ).json()["task"]
        archived = self.client.patch(
            f"/api/admin/tasks/{task['task_id']}",
            json={"expected_revision": done["revision"], "changes": {"archived": True}},
        ).json()["task"]
        self.assertTrue(archived["archived_at"])
        self.assertEqual(self.client.get("/api/admin/tasks").json()["tasks"], [])
        self.assertEqual(len(self.client.get("/api/admin/tasks?include_archived=true").json()["tasks"]), 1)

    def test_controller_conflict_pause_resume_and_required_error_classes(self):
        first = self.create(title="first")
        second = self.create(title="second")
        running = self.client.post(
            f"/api/admin/tasks/{first['task_id']}/move",
            json={"expected_revision": first["revision"], "status": "RUNNING", "actor_role": "CONTROL_X"},
        )
        self.assertEqual(running.status_code, 200)
        conflict = self.client.post(
            f"/api/admin/tasks/{second['task_id']}/move",
            json={"expected_revision": second["revision"], "status": "RUNNING", "actor_role": "CONTROL_X"},
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["detail"]["code"], "controller_busy")

        paused = self.client.post(
            f"/api/admin/tasks/{second['task_id']}/pause",
            json={"expected_revision": second["revision"]},
        ).json()["task"]
        self.assertFalse(paused["enabled"])
        resumed = self.client.post(
            f"/api/admin/tasks/{second['task_id']}/resume",
            json={"expected_revision": paused["revision"]},
        ).json()["task"]
        self.assertTrue(resumed["enabled"])
        self.assertEqual(self.client.get("/api/admin/tasks/not-found").status_code, 404)
        bad_schedule = self.client.post(
            "/api/admin/tasks",
            json=self.payload(schedule={"kind": "interval", "minutes": 0}),
        )
        self.assertEqual(bad_schedule.status_code, 400)
        bad_field = self.client.post(
            "/api/admin/tasks",
            json={**self.payload(), "command": "do something"},
        )
        self.assertEqual(bad_field.status_code, 400)

    def test_create_command_accepts_exact_durable_id_and_rejects_collision(self):
        command = controller.state.create_command("CONTROL_X", "PROBE", {"depth": 1}, "durable-command-id")
        self.assertEqual(command["command_id"], "durable-command-id")
        with self.assertRaisesRegex(RuntimeError, "command_id already exists"):
            controller.state.create_command("OTHER", "PROBE", {}, "durable-command-id")
        with self.assertRaisesRegex(ValueError, "command_id"):
            controller.state.create_command("OTHER", "PROBE", {}, " " * 4)

    def test_required_conflicts_invalid_transition_archive_execution_and_uncertain_wake(self):
        backlog = self.create(title="backlog", status="BACKLOG", controller_role="controller_a")
        invalid = self.client.post(
            f"/api/admin/tasks/{backlog['task_id']}/move",
            json={"expected_revision": backlog["revision"], "status": "RUNNING", "actor_role": "CONTROLLER_A"},
        )
        self.assertEqual(invalid.status_code, 409)
        self.assertEqual(invalid.json()["detail"]["code"], "invalid_state_transition")

        archive = self.client.patch(
            f"/api/admin/tasks/{backlog['task_id']}",
            json={"expected_revision": backlog["revision"], "changes": {"archived": True}},
        )
        self.assertEqual(archive.status_code, 409)
        self.assertEqual(archive.json()["detail"]["code"], "archive_requires_done")

        ready = self.create(title="execution", controller_role="controller_b")
        wrong_execution = self.client.patch(
            f"/api/admin/tasks/{ready['task_id']}",
            json={"expected_revision": ready["revision"], "actor_role": "OTHER", "changes": {"blocker": "blocked"}},
        )
        self.assertEqual(wrong_execution.status_code, 409)
        self.assertEqual(wrong_execution.json()["detail"]["code"], "controller_mismatch")

        claimed = self.client.post(
            f"/api/admin/tasks/{ready['task_id']}/wake",
            json={"expected_revision": ready["revision"]},
        ).json()["task"]
        duplicate = self.client.post(
            f"/api/admin/tasks/{ready['task_id']}/wake",
            json={"expected_revision": claimed["revision"]},
        )
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json()["detail"]["code"], "duplicate_or_uncertain_wake")

        uncertain = controller.state.task_store.update_wake(
            ready["task_id"], claimed["revision"], state="UNCERTAIN", error="ambiguous", blocker="ambiguous"
        )
        unresolved = self.client.patch(
            f"/api/admin/tasks/{ready['task_id']}",
            json={"expected_revision": uncertain["revision"], "actor_role": "CONTROLLER_B", "changes": {}, "wake_resolution": "invalid"},
        )
        self.assertEqual(unresolved.status_code, 400)
        resolved = self.client.patch(
            f"/api/admin/tasks/{ready['task_id']}",
            json={"expected_revision": uncertain["revision"], "actor_role": "CONTROLLER_B", "changes": {}, "wake_resolution": "not_sent"},
        )
        self.assertEqual(resolved.status_code, 200)
        self.assertEqual(resolved.json()["task"]["wake"]["state"], "IDLE")

    def test_wake_is_reserved_server_side_without_shell_or_direct_dashboard_command(self):
        task = self.create(controller_role="offline_controller")
        with patch("server.subprocess.run") as subprocess_run:
            response = self.client.post(
                f"/api/admin/tasks/{task['task_id']}/wake",
                json={"expected_revision": task["revision"]},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["task"]["wake"]["state"], "CLAIMED")
        subprocess_run.assert_not_called()
        controller.state.task_scheduler.tick()
        current = self.client.get(f"/api/admin/tasks/{task['task_id']}").json()["task"]
        self.assertEqual(current["wake"]["state"], "DEFERRED")
        self.assertEqual(current["wake"]["error"], "controller_offline")
        self.assertEqual(controller.state.commands, {})

    def test_role_inventory_only_adds_task_assignments_while_task_reserves_controller(self):
        task = self.create(controller_role="ops_a", physical_role_map={"dev1": "box_7", "review2": "worker-12", "plan_z": "box_7"})
        controller.state.role_seen_at["OPS_A"] = time.time()
        controller.state.dom_info["OPS_A"] = {"composer": True, "page_path": "/c/controller"}

        ready_roles = {item["role"]: item for item in self.client.get("/api/admin/roles").json()["roles"]}
        self.assertIn("OPS_A", ready_roles)
        self.assertNotIn("BOX_7", ready_roles)
        self.assertNotIn("WORKER-12", ready_roles)
        self.assertEqual(ready_roles["OPS_A"]["transport"], "userscript")
        self.assertIsNone(ready_roles["OPS_A"]["external_target"])
        self.assertEqual(ready_roles["OPS_A"]["page_path"], "/c/controller")
        self.assertIsNone(ready_roles["OPS_A"]["current_task_id"])

        moved = self.client.post(
            f"/api/admin/tasks/{task['task_id']}/move",
            json={"expected_revision": task["revision"], "status": "RUNNING", "actor_role": "OPS_A"},
        )
        self.assertEqual(moved.status_code, 200)
        running_roles = {item["role"]: item for item in self.client.get("/api/admin/roles").json()["roles"]}
        self.assertIn("BOX_7", running_roles)
        self.assertIn("WORKER-12", running_roles)
        self.assertEqual(running_roles["OPS_A"]["current_task_id"], task["task_id"])

    def test_lifespan_starts_and_stops_exact_scheduler_instance(self):
        active = controller.state
        self.assertFalse(active.task_scheduler.health()["running"])
        with TestClient(controller.app) as client:
            health = client.get("/api/admin/tasks").json()["scheduler"]
            self.assertTrue(health["running"])
            self.assertEqual(health["server_instance_id"], active.server_instance_id)
        self.assertFalse(active.task_scheduler.health()["running"])

    def test_task_store_load_error_isolated_from_flow_role_and_command_reads(self):
        corrupt = b'{"version":1,"tasks":'
        self.task_path.parent.mkdir(parents=True, exist_ok=True)
        self.task_path.write_bytes(corrupt)
        controller.state = controller.DiagnosticState(flow_path=self.flow_path, task_path=self.task_path)
        tasks = self.client.get("/api/admin/tasks")
        self.assertEqual(tasks.status_code, 200)
        self.assertIsNotNone(tasks.json()["load_error"])
        self.assertEqual(self.client.get("/api/admin/flow").status_code, 200)
        self.assertEqual(self.client.get("/api/admin/role/ANY").status_code, 200)
        create = self.client.post("/api/admin/tasks", json=self.payload())
        self.assertEqual(create.status_code, 409)
        self.assertEqual(create.json()["detail"]["code"], "task_store_unavailable")
        self.assertEqual(self.task_path.read_bytes(), corrupt)


if __name__ == "__main__":
    unittest.main()
