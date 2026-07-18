import threading
import unittest
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fastapi.testclient import TestClient

import server as controller


class DiagnosticControllerTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(controller.app)
        controller.state = controller.DiagnosticState()
        for role in ("DEV", "IMG", "REVIEW", "SOLO"):
            controller.state.status[role] = "ONLINE"
            controller.state.role_seen_at[role] = time.time()

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
        test1 = self.client.post("/api/status", json={"role": "TEST1", "session_id": "/c/1"}).json()
        test2 = self.client.post("/api/status", json={"role": "TEST2", "session_id": "/c/2"}).json()
        dev = self.client.post("/api/status", json={"role": "DEV", "session_id": "/c/dev"}).json()

        self.assertEqual(
            test1["flow_status"],
            {
                "run_id": "run-test",
                "state": "RUNNING",
                "from_role": "User",
            },
        )
        self.assertEqual(test2["flow_status"], {"run_id": "run-test", "state": "WAITING"})
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
            {"run_id": "run-test", "state": "DONE", "done_from": "A", "sent_to": "B"},
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

        status = self.client.post("/api/status", json={"role": "TEST1", "session_id": "/c/1"}).json()
        self.assertEqual(status["flow_status"], {"run_id": "run-test", "state": "WAITING", "sent_to": "B"})

    def test_flow_status_cleanup_cannot_clear_a_newer_run(self):
        self.client.post(
            "/api/admin/flow-status",
            json={"run_id": "old-run", "updates": {"TEST1": {"state": "RUNNING"}}},
        )
        self.client.post(
            "/api/admin/flow-status",
            json={"run_id": "new-run", "updates": {"TEST1": {"state": "WAITING"}}},
        )

        stale_cleanup = self.client.post(
            "/api/admin/flow-status",
            json={"run_id": "old-run", "updates": {"TEST1": None}},
        )

        self.assertEqual(stale_cleanup.status_code, 200)
        payload = self.client.post("/api/status", json={"role": "TEST1", "session_id": "/c/1"}).json()
        self.assertEqual(payload["flow_status"], {"run_id": "new-run", "state": "WAITING"})

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
            json={"run_id": "new-run", "updates": {"TEST1": {"state": "WAITING"}}},
        )

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
        self.assertEqual(payload["flow_status"], {"run_id": "new-run", "state": "WAITING"})

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

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("TEST1", controller.state.flow_statuses)
        self.assertEqual(controller.state.flow_statuses["TEST2"]["state"], "WAITING")

    def test_admin_command_and_result_endpoints(self):
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

    def test_fail_closed_browser_states_are_terminal_command_results(self):
        for state_name in (
            "SEND_BLOCKED_OWNERSHIP_LOST",
            "PASTE_BLOCKED_MANUAL_INPUT",
            "MANUAL_INPUT_PENDING",
            "CHOICE_PROMPT_CLICKED",
            "CHOICE_PROMPT_CLICK_FAILED",
            "CHOICE_PROMPT_NOT_FOUND",
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

    def test_admin_role_marks_never_seen_numbered_role_offline(self):
        role_response = self.client.get("/api/admin/role/REVIEW1")

        role_payload = role_response.json()
        self.assertEqual(role_payload["status"], "OFFLINE")
        self.assertFalse(role_payload["online"])
        self.assertIsNone(role_payload["last_seen_age_s"])

    def test_admin_routes_lists_samples(self):
        response = self.client.get("/api/admin/routes")
        self.assertEqual(response.status_code, 200)
        routes = response.json()["routes"]
        samples = [route["sample"] for route in routes]

        self.assertIn("/api/admin/role/A", samples)
        self.assertIn("/api/admin/events?role=A&limit=20", samples)
        self.assertEqual({"client", "admin"}, {route["group"] for route in routes})

    def test_startup_log_lists_backend_api_samples(self):
        with patch("builtins.print") as print_mock:
            controller.log_startup_routes("http://127.0.0.1:8500")

        output = "\n".join(str(call.args[0]) for call in print_mock.call_args_list)
        self.assertIn("backend API", output)
        self.assertIn("http://127.0.0.1:8500/api/admin/role/A", output)
        self.assertIn("http://127.0.0.1:8500/api/admin/routes", output)

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


if __name__ == "__main__":
    unittest.main()
