"""Offline checks using synthetic ICCIDs; no activation strings or QR images."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import nexsim_installation as installation
import nexsim_material_guard as guard
import nexsim_platform_activation as platform


class InstallationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.manager = installation.InstallationManager(self.root)
        self.client = Mock(base="https://admin.nexsimus.com", org_id=1,
                           account={"username": "synthetic"})
        self.owner = installation.owner_of(self.client)
        self.card = {"id": 3, "iccid": "0" * 20, "ownerOrgId": 1, "status": "USED",
                     "inventoryType": "P", "simType": "ESIM", "qrCodeSupported": True,
                     "qrViewCount": 0, "qrViewLimit": 2}
        self.row = {"inventory_id": 3, "iccid": self.card["iccid"], "eligible": True, "state": "ready"}
        for target in ("requests.sessions.Session.request", "qrcode.make"):
            guard_patch = patch(target, side_effect=AssertionError("Real network/images forbidden"))
            guard_patch.start()
            self.addCleanup(guard_patch.stop)

    def resolve(self, cards, status="RELEASED", targets=None):
        self.client.get.side_effect = lambda endpoint, params=None: (
            {"records": cards, "pages": 1} if endpoint == "/api/inventory/page" else {"status": status})
        return installation.resolve_cards(self.client, targets or [self.card["iccid"]])

    def job(self, job_id="im-" + "a" * 32):
        self.manager.jobs[job_id] = {"id": job_id, "owner": self.owner,
            "rows": [dict(self.row)], "state": "prepared", "fetch_started": False, "cancel": False}
        return job_id

    def test_used_released_allowed_without_product_or_new_order(self):
        rows = self.resolve([self.card])
        self.assertTrue(rows[0]["eligible"])
        self.client.get_qr_code_once.assert_not_called()
        self.client.submit_activation_csv.assert_not_called()

    def test_missing_and_duplicate_iccids_are_explicitly_blocked(self):
        targets = [self.card["iccid"], "1" * 20]
        rows = self.resolve([self.card, self.card], targets=targets)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(not row["eligible"] for row in rows))

    def test_wrong_owner_status_capacity_or_installed_cannot_fetch(self):
        for changes, status in [({"ownerOrgId": 2}, "RELEASED"), ({"status": "ALLOCATED"}, "RELEASED"),
                                ({"qrViewCount": 2}, "RELEASED"), ({"qrViewLimit": None}, "RELEASED"),
                                ({}, "INSTALLED"), ({}, None)]:
            with self.subTest(changes=changes, status=status):
                self.assertFalse(self.resolve([{**self.card, **changes}], status)[0]["eligible"])
        self.client.get_qr_code_once.assert_not_called()

    def test_shared_claim_survives_new_manager_and_different_batch(self):
        guard.claim(self.root, self.client, 3, "first")
        with self.assertRaises(platform.PlatformActivationStop):
            guard.claim(self.root, self.client, 3, "second")
        self.assertTrue(installation.InstallationManager(self.root)._previous_attempt(self.owner, self.row))

    def test_legacy_metadata_blocks_fetch_without_reading_images(self):
        folder = self.root / "platform-activation-batches"
        folder.mkdir()
        (folder / "pb-test.json").write_text(json.dumps({"owner": self.owner, "qr_operations": "started",
            "records": [{"iccid": self.row["iccid"], "qr_status": "not_requested"}]}), encoding="utf-8")
        self.assertTrue(self.manager._previous_attempt(self.owner, self.row))

    def test_confirmation_and_selection_are_required_server_side(self):
        job_id = self.job()
        for body in ({"job_id": job_id, "inventory_ids": [3]},
                     {"job_id": job_id, "inventory_ids": [9], "confirmed": True}):
            with self.assertRaises(installation.InstallationStop):
                self.manager.start_fetch(body)
        self.assertFalse(self.manager.jobs[job_id]["fetch_started"])

    def test_fresh_login_identity_must_match(self):
        job_id = self.job()
        other = Mock(base=self.client.base, org_id=1, account={"username": "different"})
        with patch.object(self.manager, "_client", return_value=other):
            self.manager._fetch(job_id, {}, [self.row])
        self.assertEqual(self.manager.snapshot(job_id)["state"], "failed")
        other.get_qr_code_once.assert_not_called()

    def test_changed_state_blocks_before_consumable_request(self):
        job_id = self.job()
        with patch.object(self.manager, "_client", return_value=self.client), \
                patch.object(installation, "resolve_cards", return_value=[{**self.row, "eligible": False}]):
            self.manager._fetch(job_id, {}, [self.row])
        self.client.get_qr_code_once.assert_not_called()

    def test_unknown_request_is_not_retried_and_marker_is_persistent(self):
        job_id = self.job()
        self.client.get.return_value = {"status": "RELEASED"}
        self.client.get_qr_code_once.side_effect = platform.PlatformActivationStop("synthetic timeout", unknown=True)
        with patch.object(self.manager, "_client", return_value=self.client), \
                patch.object(installation, "resolve_cards", return_value=[self.row]):
            self.manager._fetch(job_id, {}, [self.row])
        self.client.get_qr_code_once.assert_called_once_with(3, self.row["iccid"])
        self.client.submit_activation_csv.assert_not_called()
        self.assertTrue(self.manager._marker(self.owner, self.row).exists())
        self.assertEqual(self.manager.snapshot(job_id)["rows"][0]["state"], "unknown")
        self.assertIsNone(self.manager.download(job_id, 3, "txt"))

    def test_restart_restores_metadata_but_does_not_resume_request(self):
        job_id = self.job()
        self.manager.jobs[job_id].update(state="fetching", fetch_started=True)
        self.manager._persist(job_id)
        recovered = installation.InstallationManager(self.root).snapshot(job_id)
        self.assertEqual(recovered["state"], "interrupted")
        self.assertTrue(recovered["fetch_started"])
        self.assertIsNone(self.manager.snapshot("../record"))
        self.client.get_qr_code_once.assert_not_called()


if __name__ == "__main__":
    unittest.main()
