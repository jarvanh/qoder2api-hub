"""Offline account-group/scope regressions; only fake credentials in temp dirs.

Run: python tests/_test_admin_groups.py
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import qoder_accounts as A
import qoder_settings as S


class AdminGroupsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="admin-groups-")
        self.addCleanup(self.tmp.cleanup)
        self.network = patch("socket.socket", side_effect=AssertionError("network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.pool = A.AccountPool(self.tmp.name)
        self.a = self.make("a", "red")
        self.b = self.make("b", "blue")
        self.c = self.make("c", "", "intl")
        self.pool.accounts = [self.a, self.b, self.c]

    def make(self, uid, group="", realm="cn"):
        return A.Account({"uid": uid, "accessToken": "fake-token-" + uid,
                          "expiresAt": time.time() + 3600,
                          "realm": realm, "group": group})

    def test_group_roundtrip_and_legacy(self):
        self.assertEqual(self.make("old").group, "")
        self.a.save(self.tmp.name)
        with open(self.a.path, encoding="utf-8") as f:
            restored = A.Account(json.load(f))
        self.assertEqual(restored.group, "red")
        self.assertEqual(restored.public()["group"], "red")
        self.assertNotIn("accessToken", restored.public())
        self.assertEqual(A.account_to_export(restored)["group"], "red")
        self.a.group = "green"
        self.a.save(self.tmp.name)
        self.pool.load()
        self.assertEqual(self.pool.get("a").group, "green")

    def test_normalize_import_group_override_and_pool_import(self):
        row = {"uid": "new", "accessToken": "fake", "group": "red"}
        self.assertEqual(A.normalise_import_row(row)["group"], "red")
        self.assertEqual(A.normalize_import_account(row, group="blue")["group"], "blue")
        self.assertEqual(A.normalise_import_row(row, group="")["group"], "")
        self.assertEqual(A.normalise_import_row({"accessToken": "fake"})["group"], "")
        result = self.pool.import_rows([row], group="blue")
        self.assertEqual(result["added"], ["new"])
        self.assertEqual(self.pool.get("new").group, "blue")

    def test_group_listing_and_selector_union(self):
        self.assertEqual([x["uid"] for x in self.pool.list_public(group="red")], ["a"])
        self.assertEqual([x["uid"] for x in self.pool.list_public(group="")], ["c"])
        self.assertIsNone(self.pool.scope_uids([], []))
        self.assertEqual(self.pool.scope_uids(["b"], ["red"]), {"a", "b"})
        self.assertEqual(self.pool.scope_uids(account_groups=[""]), {"c"})
        self.assertEqual(self.pool.scope_uids(account_groups=["gone"]), set())
        self.assertEqual(self.pool.scope_uids(["c"], realm="cn"), set())
        self.a.group = "blue"
        self.assertEqual(self.pool.scope_uids(account_groups=["red"]), set())

    def test_empty_scope_never_falls_back(self):
        self.pool.affinity.bind("s", "a")
        self.assertIsNone(self.pool.pick_for_session("cn", "s", allowed_uids=set()))
        self.assertIsNone(self.pool.affinity.get("s"))
        self.assertIsNone(self.pool.pick(allowed_uids=set()))
        self.assertIsNone(self.pool.representative(allowed_uids=set()))
        self.assertEqual(self.pool.count_ready(allowed_uids=set()), 0)

    def test_affinity_rebound_and_no_realm_affinity(self):
        self.pool.affinity.bind("s", "a")
        self.assertIs(self.pool.pick_for_session("cn", "s", allowed_uids={"b"}), self.b)
        self.assertEqual(self.pool.affinity.get("s"), "b")
        self.assertIs(self.pool.pick_for_session(session_key="s", allowed_uids={"b"}), self.b)
        self.assertIsNone(self.pool.pick_for_session("intl", "s", allowed_uids={"b"}))

    def test_scope_applies_to_expiry_rotation_degraded_and_model(self):
        self.a.in_expiring_window = lambda: True
        self.a.expiring_window_days = 5
        for _ in range(8):
            self.assertIs(self.pool.pick(allowed_uids={"b"}), self.b)
        self.b.degrade_until = time.time() + 300
        self.assertIs(self.pool.pick(allowed_uids={"b"}), self.b)
        self.assertIsNone(self.pool.pick(exclude={"b"}, allowed_uids={"b"}))
        self.b.model_cooldowns["fake-model"] = time.time() + 300
        self.assertIsNone(self.pool.pick(model="fake-model", allowed_uids={"b"}))
        self.assertEqual(self.pool.count_ready(model="fake-model", allowed_uids={"b"}), 0)
        self.assertIsNone(self.pool.pick("intl", allowed_uids={"b"}))
        self.assertEqual(self.pool.count_ready("cn", allowed_uids={"b"}), 1)
        self.assertIs(self.pool.representative(allowed_uids={"b"}), self.b)

    def test_legacy_signatures_still_work(self):
        self.assertEqual(self.pool.count_ready("cn"), 2)
        self.assertIn(self.pool.pick("cn").uid, {"a", "b"})
        self.assertIn(self.pool.pick_for_session("cn", "legacy").uid, {"a", "b"})
        self.assertEqual(len(self.pool.list_public()), 3)

    def test_key_selectors_roundtrip(self):
        keys = S.set_api_keys(self.tmp.name, [{"key": "fake-key",
            "account_ids": [" b ", "b"], "account_groups": ["red", "", "red"]}])
        self.assertEqual(keys[0]["account_ids"], ["b"])
        self.assertEqual(S.api_keys(self.tmp.name)[0]["account_groups"], ["red", ""])
        self.assertEqual(S._clean_key_entry({"key": "fake", "group": "blue"})["account_groups"], ["blue"])
        legacy = S._clean_key_entry({"key": "fake"})
        self.assertIsNone(self.pool.scope_uids(legacy["account_ids"], legacy["account_groups"]))
        with self.assertRaises(ValueError):
            S._clean_key_entry({"key": "fake", "account_groups": {"bad": 1}})
        with self.assertRaises(ValueError):
            self.pool.scope_uids(account_groups=[12])


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(AdminGroupsTests))
    print("SUMMARY: TOTAL %d checks, %d passed, %d failed" % (result.testsRun, result.testsRun-len(result.failures)-len(result.errors), len(result.failures)+len(result.errors)))
    sys.exit(0 if result.wasSuccessful() else 1)
