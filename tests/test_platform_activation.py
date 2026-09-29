"""Offline regression tests: synthetic identities, no QR data or real requests."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import nexsim_platform_activation as activation
import nexsim_status_web as web


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.batch = {
            "batch_id": "pb-20260929-120000-abcdef01", "org_id": 1, "product_id": 2,
            "records": [{"inventory_id": 3, "iccid": "0" * 20, "qr_status": "not_requested"}],
        }
        self.order = {"id": 4, "iccid": "0" * 20, "cardId": 3, "orgId": 1,
                      "productId": 2, "orderNo": "TEST", "orderStatus": "ACTIVE",
                      "phoneNumber": "TEST-PHONE", "signalAddonStatus": "SUCCESS"}
        self.subscriber = {**self.order, "id": 5, "orderId": 4, "subscriberStatus": "ACTIVE"}
        self.client = Mock(org_id=1)
        guard = patch("requests.sessions.Session.request", side_effect=AssertionError("Network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def verify(self, orders=None, subscribers=None):
        pages = {"/api/orders/page": [self.order] if orders is None else orders,
                 "/api/orders/subscribers/page": [self.subscriber] if subscribers is None else subscribers}
        self.client.get.side_effect = lambda endpoint, params: {"records": pages[endpoint], "pages": 1}
        with patch.object(activation, "_validate_saved_qr", side_effect=AssertionError("QR access forbidden")):
            result = activation.verify_activation(self.client, self.batch, self.root, 1, 2)
        self.client.submit_activation_csv.assert_not_called()
        self.client.get_qr_code_once.assert_not_called()
        return result

    def prepare_preview(self, price="12.00", balance="50.00"):
        data = {
            "/api/inventory/page": {"records": [{"id": 3, "iccid": "0" * 20,
                "ownerOrgId": 1, "productId": 2, "inventoryType": "P", "simType": "ESIM",
                "status": "ALLOCATED", "qrCodeSupported": True}], "pages": 1},
            "/api/orders/page": {"records": [], "pages": 1},
            "/api/products": [{"id": 2, "displayPrice": price, "cardCategory": "P_CARD",
                               "productPurpose": "BASE_PLAN", "activationEnabled": 1}],
            "/api/finance/organizations/1/balances": [{"balanceType": "ACTIVATION", "balance": balance}],
        }
        self.client.get.side_effect = lambda endpoint, params=None: data[endpoint]
        # Submission still validates saved QR files in production. Never create
        # or read any QR fixture while testing the independent money flow.
        guard = patch.object(activation, "_validate_saved_qr")
        guard.start()
        self.addCleanup(guard.stop)

    def test_manual_activation_without_qr_or_submission_intent(self):
        result = self.verify()
        self.assertTrue(result["all_confirmed"])
        summary = activation.activation_artifact_summary(self.root, self.batch["batch_id"])
        self.assertFalse(summary["has_submission_intent"])
        self.assertEqual(summary["state"], "verified")
        self.assertEqual(summary["verification"]["rows"], result["rows"])

    def test_no_order_is_unconfirmed_and_stays_unconfirmed_after_reload(self):
        result = self.verify([], [])
        self.assertEqual(result["rows"][0]["state"], "unconfirmed")
        self.assertIn("未查到", result["rows"][0]["message"])
        self.assertEqual(activation.activation_artifact_summary(self.root, self.batch["batch_id"])["state"], "unconfirmed")

    def test_unknown_signal_mismatched_phone_or_duplicate_orders_never_succeed(self):
        cases = [([self.order], [{**self.subscriber, "signalAddonStatus": "UNKNOWN"}]),
                 ([self.order], [{**self.subscriber, "phoneNumber": "OTHER"}]),
                 ([self.order, {**self.order, "id": 9}], [self.subscriber]),
                 ([{**self.order, "orgId": 9}], [self.subscriber]),
                 ([self.order], [self.subscriber, self.subscriber])]
        for orders, subscribers in cases:
            with self.subTest(orders=orders, subscribers=subscribers):
                self.assertFalse(self.verify(orders, subscribers)["all_confirmed"])

    def test_wrong_account_stops_before_queries(self):
        self.client.org_id = 9
        with self.assertRaises(activation.PlatformActivationStop):
            activation.verify_activation(self.client, self.batch, self.root, 1, 2)
        self.client.get.assert_not_called()

    def test_legacy_csv_is_checked_not_verified(self):
        directory = activation.activation_directory(self.root, self.batch["batch_id"])
        directory.mkdir(parents=True)
        (directory / "verification.csv").write_text("legacy", encoding="utf-8")
        summary = activation.activation_artifact_summary(self.root, self.batch["batch_id"])
        self.assertEqual(summary["state"], "checked")
        self.assertIsNone(summary["verification"])

    def test_preview_uses_explicit_amount_limit(self):
        self.prepare_preview()
        result = activation.preview_activation(self.client, self.batch, self.root, 1, 2, max_total="50.00")
        self.assertEqual(result["total"], "12.00")
        self.assertEqual(result["max_total"], "50.00")

    def test_insufficient_balance_still_blocks(self):
        self.prepare_preview(balance="1.00")
        with self.assertRaisesRegex(activation.PlatformActivationStop, "余额不足"):
            activation.preview_activation(self.client, self.batch, self.root, 1, 2, max_total="50.00")

    def test_changed_total_blocks_before_intent_or_write(self):
        self.prepare_preview(price="13.00")
        with self.assertRaisesRegex(activation.PlatformActivationStop, "总价已变化"):
            activation.submit_activation(self.client, self.batch, self.root, 1, 2, "12.00", max_total="50.00")
        self.client.submit_activation_csv.assert_not_called()
        self.assertFalse(activation.activation_artifact_summary(self.root, self.batch["batch_id"])["has_submission_intent"])

    def test_matching_total_submits_once_using_iccid(self):
        self.prepare_preview()
        self.client.submit_activation_csv.return_value = {"_http_status": 200, "code": 0, "data": {}}
        activation.submit_activation(self.client, self.batch, self.root, 1, 2, "12.00", max_total="50.00")
        with self.assertRaises(activation.PlatformActivationStop):
            activation.submit_activation(self.client, self.batch, self.root, 1, 2, "12.00", max_total="50.00")
        self.client.submit_activation_csv.assert_called_once()
        self.assertEqual(self.client.submit_activation_csv.call_args.args[1].decode("utf-8-sig"), "ICCID\r\n" + "0" * 20 + "\r\n")

    def test_unknown_submission_cannot_be_repeated(self):
        self.prepare_preview()
        self.client.submit_activation_csv.side_effect = activation.PlatformActivationStop("unknown", unknown=True)
        for _ in range(2):
            with self.assertRaises(activation.PlatformActivationStop):
                activation.submit_activation(self.client, self.batch, self.root, 1, 2, "12.00", max_total="50.00")
        self.client.submit_activation_csv.assert_called_once()

    def test_job_automatically_verifies_after_submit_and_preserves_query_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                manager = web.PlatformActivationJobManager(Mock(), self.root)
                manager.jobs["test"] = {"action": "submit", "batch": copy.deepcopy(self.batch),
                    "base_url": "https://example.invalid", "credentials": ("test", "test"),
                    "org_id": 1, "product_id": 2, "confirmed_total": "12.00", "max_total": "50.00"}
                with patch.object(activation, "PlatformClient", return_value=self.client), \
                     patch.object(activation, "submit_activation", return_value={}) as submit, \
                     patch.object(activation, "verify_activation", return_value={"confirmed": 1},
                                  side_effect=activation.PlatformActivationStop("offline") if fail else None) as verify:
                    manager._run("test")
                submit.assert_called_once()
                verify.assert_called_once()
                result = manager.snapshot("test")["result"]
                self.assertIn("verification_error" if fail else "verification", result)
                self.assertNotIn("credentials", manager.snapshot("test"))


if __name__ == "__main__":
    unittest.main()
