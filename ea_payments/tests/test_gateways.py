"""Real Frappe documents; only provider HTTP and SMS transport are stubbed."""
import importlib
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.utils import now_datetime


class TestGateways(unittest.TestCase):
    def setUp(self):
        self.assertTrue(
            frappe.db.exists("DocType", "Pesapal Settings"),
            "Missing behavior: saving Pesapal Settings must register a native Phone gateway",
        )
        self.assertTrue(frappe.db.exists("DocType", "Flutterwave Settings"))
        self.common = importlib.import_module("ea_payments.common")
        self.pesapal = importlib.import_module("ea_payments.ea_payments.doctype.pesapal_settings.pesapal_settings")
        self.flutterwave = importlib.import_module("ea_payments.ea_payments.doctype.flutterwave_settings.flutterwave_settings")
        self.http_calls = []
        self.http_error = False
        self.provider_amount = 12000
        self.provider_currency = "UGX"
        self.provider_reference = None
        self.provider_status = "success"
        self.sparse_unpaid = False
        self.pr = self.make_pr()
        self.http_patch = patch("requests.request", side_effect=self.http)
        self.http_patch.start()
        self.addCleanup(self.http_patch.stop)
        for provider in ("Pesapal", "Flutterwave"):
            settings = frappe.get_doc(provider + " Settings")
            settings.update({
                "environment": "Sandbox", "consumer_key": "demo-key",
                "consumer_secret": "demo-secret", "secret_key": "demo-secret",
                "webhook_secret_hash": "demo-hash", "ipn_id": "",
            })
            settings.save(ignore_permissions=True)
        self.sms = patch("ea_payments.common.send_sms")
        self.sent = self.sms.start()
        self.addCleanup(self.sms.stop)
        frappe.db.set_single_value("SMS Settings", "sms_gateway_url", "https://sms.example.test")

    def tearDown(self):
        frappe.db.rollback()

    def make_pr(self, provider="Pesapal", channel="Phone", status="Requested"):
        doc = frappe.get_doc({
            "doctype": "Payment Request", "name": "EA-TEST-" + frappe.generate_hash(length=10),
            "payment_request_type": "Inward", "payment_channel": channel,
            "payment_gateway": provider, "grand_total": 12000, "currency": "UGX",
            "status": status, "docstatus": 1, "outstanding_amount": 12000,
            "reference_doctype": "POS Invoice", "reference_name": "EA-DEMO-BILL",
            "phone_number": "256700000001", "email_to": "guest@example.test",
        })
        doc.db_insert()
        return doc

    def order(self, provider="Pesapal", pr=None, **extra):
        pr = pr or self.pr
        settings = frappe.get_doc(provider + " Settings")
        args = dict(reference_doctype="Payment Request", reference_docname=pr.name,
                    amount=12000, currency="UGX", payer_email="guest@example.test", payer_name="Demo Guest")
        args.update(extra)
        url = settings.get_payment_url(**args)
        name = frappe.db.get_value("Integration Request", {
            "reference_doctype": "Payment Request", "reference_docname": pr.name,
            "integration_request_service": provider,
        }, "name")
        return settings, frappe.get_doc("Integration Request", name), url

    def http(self, method, url, **kwargs):
        self.http_calls.append((method, url, kwargs))
        if self.http_error:
            raise RuntimeError("demo-secret token=must-not-log")
        payload = kwargs.get("json") or {}
        if url.endswith("Auth/RequestToken"):
            response = {"token": "demo-access-token", "status": "200", "error": None}
        elif url.endswith("URLSetup/RegisterIPN"):
            response = {"ipn_id": "demo-ipn", "status": "200", "error": None}
        elif url.endswith("Transactions/SubmitOrderRequest"):
            response = {"order_tracking_id": "TRACK-" + payload["id"],
                        "merchant_reference": payload["id"], "redirect_url": "https://cybqa.pesapal.com/pay/" + payload["id"],
                        "status": "200", "error": None, "token": "must-not-store"}
        elif url.endswith("Transactions/GetTransactionStatus"):
            tracking = kwargs["params"]["orderTrackingId"]
            # Real API 3.0 shape: HTTP 200 with an error object even when processed
            # (sandbox and published sample); pending carries payment_details_not_found.
            code, description = {"success": (1, "COMPLETED"), "pending": (0, "INVALID"),
                                 "FAILED": (2, "FAILED"), "REVERSED": (3, "REVERSED")}.get(self.provider_status, (None, None))
            error = {"error_type": None, "code": None, "message": None, "call_back_url": "https://example.test/cb"}
            if code == 0:
                error = {"error_type": "api_error", "code": "payment_details_not_found",
                         "message": "Pending Payment", "call_back_url": "https://example.test/cb"}
            response = {"payment_method": "", "amount": self.provider_amount, "created_date": "2026-10-15T10:00:00",
                        "confirmation_code": "", "payment_status_description": description,
                        "description": "", "message": "Request processed successfully",
                        "payment_account": "", "call_back_url": "https://example.test/cb", "status_code": code,
                        "merchant_reference": self.provider_reference or tracking.removeprefix("TRACK-"),
                        "payment_status_code": "", "currency": self.provider_currency,
                        "error": error, "status": "200", "token": "must-not-store"}
            if code == 0:
                response["status"] = "500"  # as returned by cybqa for an unpaid order
            if self.sparse_unpaid and code in (0, 2, 3):
                response.update(amount=0.0, currency=None, merchant_reference=None,
                                created_date=None, payment_method="", confirmation_code="")
            if code is None:
                response = {"error": {"error_type": "api_error", "code": "invalid_api_auth",
                                      "message": "Unauthorized"}, "status": "500"}
        elif url.endswith("/payments"):
            response = {"status": "success", "data": {"link": "https://checkout.flutterwave.com/" + payload["tx_ref"]}}
        elif "/transactions/" in url:
            response = {"status": "success", "data": {
                "id": 42001, "status": "successful" if self.provider_status == "success" else self.provider_status,
                "amount": self.provider_amount, "currency": self.provider_currency,
                "tx_ref": self.provider_reference or kwargs.get("params", {}).get("tx_ref", self.pr.name),
                "flw_ref": "FLW-MOCK-42001", "charged_amount": self.provider_amount,
                "app_fee": 360, "merchant_fee": 0, "processor_response": "Approved",
                "payment_type": "mobilemoneyuganda", "created_at": "2026-10-15T10:00:00Z",
                "secret": "must-not-store",
            }}
        else:
            raise AssertionError("unexpected provider URL: " + url)
        class Response:
            def raise_for_status(self):
                pass
            def json(self):
                return response
        return Response()

    def ipn(self, integration=None, **kwargs):
        if integration is None:
            _, integration, _ = self.order()
        return self.pesapal.ipn(OrderTrackingId=integration.name,
                                OrderMerchantReference=self.pr.name, OrderNotificationType="IPNCHANGE", **kwargs)

    def webhook(self, integration=None, signature="demo-hash", **body):
        from werkzeug.test import EnvironBuilder
        from werkzeug.wrappers import Request
        if integration is None:
            self.pr.db_set("payment_gateway", "Flutterwave")
            _, integration, _ = self.order("Flutterwave")
        body = {"event": "charge.completed", "data": {"id": 42001, "tx_ref": self.pr.name, **body}}
        with patch.object(frappe.local, "request", Request(EnvironBuilder(
            method="POST", json=body, headers={"verif-hash": signature}).get_environ()), create=True):
            return self.flutterwave.webhook()

    def assert_unpaid(self, code, integration, status="Requested"):
        self.assertEqual(frappe.db.get_value("Payment Request", self.pr.name, "status"), status)
        integration.reload()
        self.assertEqual(integration.status, "Failed")
        self.assertIn(code, integration.error)

    def test_settings_register_gateways_phone_accounts_and_modes(self):
        for provider in ("Pesapal", "Flutterwave"):
            with self.subTest(provider=provider):
                gateway = frappe.get_doc("Payment Gateway", provider)
                self.assertEqual(gateway.gateway_settings, provider + " Settings")
                self.assertEqual(frappe.db.get_value("Mode of Payment", provider, "type"), "Phone")
                self.assertTrue(frappe.db.exists("Payment Gateway Account", {
                    "payment_gateway": provider, "payment_channel": "Phone"}))

    def test_only_ugx_is_accepted(self):
        for provider in ("Pesapal", "Flutterwave"):
            settings = frappe.get_doc(provider + " Settings")
            settings.validate_transaction_currency("UGX")
            with self.assertRaisesRegex(frappe.ValidationError, "PAY-002"):
                settings.validate_transaction_currency("USD")

    def test_each_settings_is_single_and_secrets_are_password_fields(self):
        for provider, fields in (("Pesapal", ("consumer_key", "consumer_secret")),
                                  ("Flutterwave", ("secret_key", "webhook_secret_hash"))):
            meta = frappe.get_meta(provider + " Settings")
            self.assertTrue(meta.issingle)
            for field in fields:
                self.assertEqual(meta.get_field(field).fieldtype, "Password")

    def test_pesapal_reuses_one_order_and_stores_registered_ipn(self):
        _, integration, url = self.order()
        _, again, again_url = self.order()
        self.assertEqual((integration.name, url), (again.name, again_url))
        self.assertEqual(sum(c[1].endswith("SubmitOrderRequest") for c in self.http_calls), 1)
        self.assertEqual(frappe.db.get_single_value("Pesapal Settings", "ipn_id"), "demo-ipn")
        self.assertEqual(integration.name, "TRACK-" + self.pr.name)

    def test_flutterwave_reuses_pr_as_tx_ref(self):
        self.pr.db_set("payment_gateway", "Flutterwave")
        _, integration, url = self.order("Flutterwave")
        _, again, again_url = self.order("Flutterwave")
        self.assertEqual((integration.name, url), (self.pr.name, again_url))
        self.assertEqual(again.name, integration.name)
        self.assertEqual(sum(c[1].endswith("/payments") for c in self.http_calls), 1)

    def test_provider_order_uses_server_amount_not_client_amount(self):
        self.order(amount=1, currency="USD")
        payload = next(c[2]["json"] for c in self.http_calls if c[1].endswith("SubmitOrderRequest"))
        self.assertEqual((payload["amount"], payload["currency"], payload["id"]), (12000, "UGX", self.pr.name))

    def test_foreign_or_non_payment_request_is_refused(self):
        with self.assertRaisesRegex(frappe.ValidationError, "PAY-003"):
            self.order(reference_doctype="Sales Invoice")
        with self.assertRaisesRegex(frappe.ValidationError, "PAY-003"):
            self.order("Flutterwave")

    def test_missing_payment_request_is_refused(self):
        with self.assertRaisesRegex(frappe.ValidationError, "PAY-003"):
            self.order(reference_docname="DOES-NOT-EXIST")

    def test_non_whole_ugx_order_amount_is_refused(self):
        self.pr.db_set("grand_total", 12000.5)
        with self.assertRaisesRegex(frappe.ValidationError, "PAY-001"):
            self.order()

    def test_sms_sender_fallback_and_retry_reuse_same_link(self):
        settings = frappe.get_doc("Pesapal Settings")
        args = dict(reference_doctype="Payment Request", reference_docname=self.pr.name,
                    sender="256700000002", phone_number="", request_amount=12000)
        first = settings.request_for_payment(**args)
        second = settings.request_for_payment(**args)
        self.assertEqual(first, second)
        self.assertEqual(self.sent.call_count, 2)
        self.assertEqual(self.sent.call_args.kwargs["receiver_list"], ["256700000002"])
        self.assertIn(first, self.sent.call_args.kwargs["msg"])
        self.assertEqual(sum(c[1].endswith("SubmitOrderRequest") for c in self.http_calls), 1)

    def test_sms_phone_number_takes_precedence(self):
        frappe.get_doc("Pesapal Settings").request_for_payment(
            reference_doctype="Payment Request", reference_docname=self.pr.name,
            phone_number="256700000003", sender="256700000002")
        self.assertEqual(self.sent.call_args.kwargs["receiver_list"], ["256700000003"])

    def test_pay_005_before_creating_order_when_sms_absent(self):
        frappe.db.set_single_value("SMS Settings", "sms_gateway_url", "")
        with self.assertRaisesRegex(frappe.ValidationError, "PAY-005"):
            frappe.get_doc("Pesapal Settings").request_for_payment(
                reference_doctype="Payment Request", reference_docname=self.pr.name, sender="256700000002")
        self.assertEqual(self.http_calls, [])

    def test_successful_pesapal_ipn_reverifies_and_completes_native_documents(self):
        result = self.ipn()
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["orderNotificationType"], "IPNCHANGE")
        self.assertEqual(frappe.db.get_value("Payment Request", self.pr.name, "status"), "Paid")
        self.assertEqual(frappe.db.get_value("Integration Request", "TRACK-" + self.pr.name, "status"), "Completed")
        self.assertTrue(any(c[1].endswith("GetTransactionStatus") for c in self.http_calls))

    def test_pesapal_ipn_never_trusts_callback_status_amount_or_currency(self):
        self.provider_status = "FAILED"
        self.ipn(status_code=1, amount=12000, currency="UGX")
        self.assertEqual(frappe.db.get_value("Payment Request", self.pr.name, "status"), "Failed")

    def test_pesapal_completed_status_with_real_error_object_settles(self):
        result = self.ipn()
        self.assertEqual(result["status"], 200)
        self.assertEqual(frappe.db.get_value("Payment Request", self.pr.name, "status"), "Paid")

    def test_pesapal_pending_status_code_0_stays_queued_for_poller(self):
        _, integration, _ = self.order()
        self.provider_status = "pending"
        result = self.ipn(integration)
        self.assertEqual((result["status"], result.get("code")), (200, None))
        self.assertEqual(frappe.db.get_value("Integration Request", integration.name, "status"), "Queued")
        self.assertEqual(frappe.db.get_value("Payment Request", self.pr.name, "status"), "Requested")
        self.provider_status = "success"
        self.common.poll_queued()
        self.assertEqual(frappe.db.get_value("Payment Request", self.pr.name, "status"), "Paid")

    def test_pesapal_failed_or_reversed_status_fails_with_code(self):
        for status in ("FAILED", "REVERSED"):
            with self.subTest(status=status):
                self.pr = self.make_pr()
                _, integration, _ = self.order()
                self.provider_status = status
                self.ipn(integration)
                self.assert_unpaid("PAY-006", integration, "Failed")

    def test_pesapal_status_auth_error_without_status_code_is_pay_006(self):
        _, integration, _ = self.order()
        self.provider_status = "auth-error"
        self.ipn(integration)
        self.assert_unpaid("PAY-006", integration)

    def test_environment_or_credential_change_reregisters_pesapal_ipn(self):
        self.order()
        frappe.get_doc("Pesapal Settings").save(ignore_permissions=True)
        self.assertEqual(frappe.db.get_single_value("Pesapal Settings", "ipn_id"), "demo-ipn")
        for change in ({"environment": "Live"}, {"consumer_key": "rotated-demo-key"}):
            with self.subTest(change=change):
                frappe.db.set_single_value("Pesapal Settings", "ipn_id", "old-ipn")
                settings = frappe.get_doc("Pesapal Settings")
                settings.update(change)
                settings.save(ignore_permissions=True)
                self.assertFalse(frappe.db.get_single_value("Pesapal Settings", "ipn_id"))
        registrations = sum(c[1].endswith("RegisterIPN") for c in self.http_calls)
        self.order(pr=self.make_pr())
        self.assertEqual(sum(c[1].endswith("RegisterIPN") for c in self.http_calls), registrations + 1)
        self.assertIn("https://pay.pesapal.com/v3/api/URLSetup/RegisterIPN", [c[1] for c in self.http_calls])
        self.assertEqual(frappe.db.get_single_value("Pesapal Settings", "ipn_id"), "demo-ipn")

    def test_flutterwave_signed_webhook_reverifies_not_body(self):
        self.webhook(amount=1, currency="USD", status="failed")
        self.assertEqual(frappe.db.get_value("Payment Request", self.pr.name, "status"), "Paid")
        self.assertTrue(any("/transactions/" in c[1] for c in self.http_calls))

    def test_flutterwave_failed_status_cannot_be_overridden_by_body(self):
        self.provider_status = "failed"
        self.webhook(amount=12000, currency="UGX", status="successful")
        self.assertEqual(frappe.db.get_value("Payment Request", self.pr.name, "status"), "Failed")

    def test_pay_001_amount_mismatch_is_audited_and_not_paid(self):
        _, integration, _ = self.order()
        self.provider_amount = 11999
        self.ipn(integration)
        self.assert_unpaid("PAY-001", integration)

    def test_pay_001_fractional_provider_amount_is_not_truncated(self):
        _, integration, _ = self.order()
        self.provider_amount = "12000.9"
        self.ipn(integration)
        self.assert_unpaid("PAY-001", integration)

    def test_integer_ugx_accepts_decimal_zero_representation(self):
        self.provider_amount = "12000.00"
        self.ipn()
        self.assertEqual(frappe.db.get_value("Payment Request", self.pr.name, "status"), "Paid")

    def test_pay_002_currency_mismatch_is_not_paid(self):
        _, integration, _ = self.order()
        self.provider_currency = "USD"
        self.ipn(integration)
        self.assert_unpaid("PAY-002", integration)

    def test_pay_003_verified_foreign_reference_is_not_paid(self):
        _, integration, _ = self.order()
        self.provider_reference = "ANOTHER-PR"
        self.ipn(integration)
        self.assert_unpaid("PAY-003", integration)

    def test_pay_003_unknown_tracking_id_does_not_create_a_payment(self):
        result = self.pesapal.ipn(OrderTrackingId="unknown", OrderMerchantReference=self.pr.name)
        self.assertEqual(result["code"], "PAY-003")
        self.assertEqual(frappe.db.get_value("Payment Request", self.pr.name, "status"), "Requested")

    def test_pay_003_foreign_callback_reference_is_refused(self):
        _, integration, _ = self.order()
        result = self.pesapal.ipn(OrderTrackingId=integration.name, OrderMerchantReference="OTHER")
        self.assertEqual(result["code"], "PAY-003")
        self.assertEqual(frappe.db.get_value("Payment Request", self.pr.name, "status"), "Requested")

    def test_pay_004_bad_or_missing_webhook_signature_has_no_http_or_paid(self):
        self.pr.db_set("payment_gateway", "Flutterwave")
        _, integration, _ = self.order("Flutterwave")
        count = len(self.http_calls)
        for signature in ("wrong", ""):
            result = self.webhook(integration, signature=signature)
            self.assertEqual(result["code"], "PAY-004")
        self.assertEqual(len(self.http_calls), count)
        self.assertEqual(frappe.db.get_value("Payment Request", self.pr.name, "status"), "Requested")

    def test_pay_006_http_error_is_sanitized(self):
        _, integration, _ = self.order()
        self.http_error = True
        with patch.object(frappe, "log_error") as log:
            result = self.ipn(integration)
        self.assertEqual(result["code"], "PAY-006")
        self.assert_unpaid("PAY-006", integration)
        self.assertNotIn("demo-secret", str(log.call_args_list) + integration.error)

    def test_no_tokens_or_secrets_in_integration_audit(self):
        _, integration, _ = self.order()
        self.ipn(integration)
        integration.reload()
        for secret in ("demo-key", "demo-secret", "demo-access-token", "must-not-store"):
            self.assertNotIn(secret, (integration.data or "") + (integration.output or "") + (integration.error or ""))

    def test_duplicate_ipn_calls_native_set_as_paid_exactly_once(self):
        _, integration, _ = self.order()
        cls = type(self.pr)
        original = cls.set_as_paid
        calls = []
        def paid(doc):
            calls.append(doc.name)
            return original(doc)
        with patch.object(cls, "set_as_paid", paid):
            self.ipn(integration)
            self.ipn(integration)
        self.assertEqual(calls, [self.pr.name])

    def test_on_payment_authorized_compatibility_before_set_as_paid(self):
        _, integration, _ = self.order()
        cls = type(self.pr)
        original = cls.set_as_paid
        calls = []
        def authorized(doc, status):
            calls.append(("authorized", status))
        def paid(doc):
            calls.append(("paid", doc.name))
            return original(doc)
        with patch.object(cls, "on_payment_authorized", authorized, create=True), patch.object(cls, "set_as_paid", paid):
            self.ipn(integration)
        self.assertEqual(calls, [("authorized", "Completed"), ("paid", self.pr.name)])

    def test_already_paid_request_never_calls_set_as_paid(self):
        _, integration, _ = self.order()
        self.pr.db_set("status", "Paid")
        with patch.object(type(self.pr), "set_as_paid", side_effect=AssertionError("must not pay twice")):
            self.ipn(integration)
        self.assertEqual(frappe.db.get_value("Integration Request", integration.name, "status"), "Completed")

    def test_cancelled_request_is_refused(self):
        _, integration, _ = self.order()
        self.pr.db_set("docstatus", 2)
        result = self.ipn(integration)
        self.assertEqual(result["code"], "PAY-003")

    def test_completed_provider_payment_for_cancelled_request_is_unallocated_and_alerted(self):
        _, integration, _ = self.order()
        self.pr.db_set({"docstatus": 2, "status": "Cancelled"})
        result = self.ipn(integration)
        self.assertEqual(result["status"], "Completed")
        self.assertEqual(frappe.db.get_value("Payment Request", self.pr.name, "status"), "Cancelled")
        self.assertEqual(frappe.db.get_value("Integration Request", integration.name, "status"), "Completed")
        output = frappe.parse_json(frappe.db.get_value("Integration Request", integration.name, "output"))
        self.assertEqual(output["allocation"], "Unallocated")
        self.assertEqual(output["reference"], self.pr.name)
        self.assertEqual(output["amount"], 12000)
        logs = frappe.get_all("Notification Log", filters={"document_name": self.pr.name},
                              fields=["for_user", "title", "description"])
        self.assertTrue(logs)
        self.assertTrue(any("PAY-011" in (row.title or "") and self.pr.name in (row.description or "")
                            and "12000" in (row.description or "") for row in logs))

    def test_completed_provider_payment_for_failed_request_is_unallocated_and_not_paid(self):
        _, integration, _ = self.order()
        self.pr.db_set({"status": "Failed", "failed_reason": "PAY-006: expired"})
        result = self.ipn(integration)
        self.assertEqual(result["status"], "Completed")
        self.assertEqual(frappe.db.get_value("Payment Request", self.pr.name, "status"), "Failed")
        self.assertEqual(frappe.db.get_value("Integration Request", integration.name, "status"), "Completed")

    def test_late_completed_callback_is_idempotent_for_pay_011_notification(self):
        _, integration, _ = self.order()
        self.pr.db_set({"docstatus": 2, "status": "Cancelled"})
        self.ipn(integration)
        self.ipn(integration)
        logs = frappe.get_all("Notification Log", filters={"document_name": self.pr.name,
                                                            "title": ["like", "PAY-011%"]})
        self.assertEqual(len(logs), 1)

    def test_phone_success_publishes_native_pos_event_after_commit(self):
        with patch.object(frappe, "publish_realtime") as publish:
            self.ipn()
        kwargs = publish.call_args.kwargs
        self.assertEqual(kwargs["event"], "process_phone_payment")
        self.assertTrue(kwargs["after_commit"])
        self.assertTrue(kwargs["message"]["success"])
        self.assertEqual(kwargs["message"]["amount"], 12000)

    def test_poller_does_not_abandon_gateway_requests_after_24_hours(self):
        _, integration, _ = self.order()
        old = self.make_pr()
        _, old_ir, _ = self.order(pr=old)
        old_ir.db_set("creation", now_datetime() - timedelta(hours=25))
        other = frappe.get_doc({"doctype": "Integration Request", "integration_request_service": "Not EA",
                               "status": "Queued", "data": "{}"}).insert(ignore_permissions=True)
        self.common.poll_queued()
        self.assertEqual(frappe.db.get_value("Payment Request", self.pr.name, "status"), "Paid")
        self.assertEqual(frappe.db.get_value("Payment Request", old.name, "status"), "Paid")
        self.assertEqual(frappe.db.get_value("Integration Request", other.name, "status"), "Queued")

    def test_poller_fails_terminal_unpaid_orders_including_sparse_pesapal_responses(self):
        for provider, statuses in (("Pesapal", ("FAILED", "REVERSED")),
                                   ("Flutterwave", ("failed", "cancelled"))):
            for status in statuses:
                with self.subTest(provider=provider, status=status):
                    self.pr = self.make_pr(provider=provider)
                    _, integration, _ = self.order(provider)
                    self.provider_status = status
                    self.sparse_unpaid = True
                    self.common.poll_queued()
                    self.assert_unpaid("PAY-006", integration, "Failed")
                    self.assertIn("PAY-006", frappe.db.get_value("Payment Request", self.pr.name, "failed_reason"))

    def test_default_expiry_fails_sparse_invalid_pesapal_order_after_60_minutes(self):
        _, integration, _ = self.order()
        self.provider_status = "pending"
        self.sparse_unpaid = True
        integration.db_set("creation", now_datetime() - timedelta(minutes=61))
        self.common.poll_queued()
        self.assert_unpaid("PAY-006", integration, "Failed")
        self.assertIn("expired", (integration.error or "").lower())

    def test_young_sparse_invalid_pesapal_order_remains_open(self):
        _, integration, _ = self.order()
        self.provider_status = "pending"
        self.sparse_unpaid = True
        integration.db_set("creation", now_datetime() - timedelta(minutes=59))
        self.common.poll_queued()
        self.assertEqual(frappe.db.get_value("Integration Request", integration.name, "status"), "Queued")
        self.assertEqual(frappe.db.get_value("Payment Request", self.pr.name, "status"), "Requested")

    def test_configurable_expiry_uses_provider_order_age_and_boundary(self):
        _, integration, _ = self.order()
        self.provider_status = "pending"
        created = now_datetime()
        integration.db_set("creation", created)
        self.pr.db_set("creation", created - timedelta(days=2))
        with patch.dict(frappe.conf, {"ea_payments_expiry_minutes": 90}):
            with patch.object(self.common, "now_datetime", return_value=created + timedelta(minutes=90)):
                self.common.poll_queued()
            self.assertEqual(frappe.db.get_value("Payment Request", self.pr.name, "status"), "Requested")
            with patch.object(self.common, "now_datetime", return_value=created + timedelta(minutes=91)):
                self.common.poll_queued()
            self.assert_unpaid("PAY-006", integration, "Failed")

    def test_expired_flutterwave_pending_or_new_order_fails(self):
        for status in ("pending", "new"):
            with self.subTest(status=status):
                self.pr = self.make_pr(provider="Flutterwave")
                _, integration, _ = self.order("Flutterwave")
                integration.db_set("creation", now_datetime() - timedelta(minutes=61))
                self.provider_status = status
                self.common.poll_queued()
                self.assert_unpaid("PAY-006", integration, "Failed")

    def test_expired_provider_outage_does_not_claim_payment_failed(self):
        _, integration, _ = self.order()
        integration.db_set("creation", now_datetime() - timedelta(hours=25))
        self.http_error = True
        self.common.poll_queued()
        self.assert_unpaid("PAY-006", integration)

    def test_terminal_failure_never_downgrades_paid_or_cancelled_request(self):
        for status, docstatus in (("Paid", 1), ("Cancelled", 2)):
            with self.subTest(status=status):
                self.pr = self.make_pr()
                _, integration, _ = self.order()
                self.pr.db_set({"status": status, "docstatus": docstatus})
                self.provider_status = "FAILED"
                self.common.reverify(integration.name, "Pesapal")
                self.assertEqual(frappe.db.get_value("Payment Request", self.pr.name, "status"), status)

    def test_failed_provider_foreign_reference_does_not_release_open_request(self):
        _, integration, _ = self.order()
        self.provider_status = "FAILED"
        self.provider_reference = "ANOTHER-PR"
        self.ipn(integration)
        self.assert_unpaid("PAY-003", integration)

    def test_ipn_poller_race_uses_real_row_lock_and_settles_once(self):
        _, integration, _ = self.order()
        frappe.db.commit()
        site = frappe.local.site
        sites_path = frappe.local.sites_path
        cls = type(self.pr)
        original = cls.set_as_paid
        calls = []
        barrier = threading.Barrier(2)
        def paid(doc):
            # The poller can also settle unrelated demo requests on this site.
            if doc.name == self.pr.name:
                calls.append(doc.name)
            return original(doc)
        def worker(poll):
            frappe.init(site=site, sites_path=sites_path)
            try:
                frappe.connect()
                frappe.set_user("Administrator")
                barrier.wait(timeout=20)
                if poll:
                    self.common.poll_queued()
                else:
                    self.pesapal.ipn(OrderTrackingId=integration.name, OrderMerchantReference=self.pr.name)
                frappe.db.commit()
            finally:
                frappe.destroy()
        try:
            with patch.object(cls, "set_as_paid", paid), ThreadPoolExecutor(max_workers=2) as pool:
                results = [pool.submit(worker, poll) for poll in (False, True)]
                for result in results:
                    result.result(timeout=40)
            frappe.db.rollback()
            self.assertEqual(calls, [self.pr.name])
            self.assertEqual(frappe.db.get_value("Payment Request", self.pr.name, "status"), "Paid")
        finally:
            frappe.db.delete("Integration Request", {"name": integration.name})
            frappe.db.delete("Payment Request", {"name": self.pr.name})
            frappe.db.commit()
    def test_pesapal_ack_is_top_level_json_not_only_frappe_message(self):
        frappe.local.response = frappe._dict({"type": "json"})
        self.ipn()
        self.assertEqual(frappe.response.get("orderTrackingId"), "TRACK-" + self.pr.name)
        self.assertEqual(frappe.response.get("status"), 200)

    def test_malformed_signed_webhook_refuses_without_traceback(self):
        from werkzeug.test import EnvironBuilder
        from werkzeug.wrappers import Request
        for body in (["invalid"], {"data": ["invalid"]}):
            with patch.object(frappe.local, "request", Request(EnvironBuilder(
                method="POST", json=body, headers={"verif-hash": "demo-hash"}).get_environ()), create=True):
                self.assertEqual(self.flutterwave.webhook()["code"], "PAY-003")

    def test_simultaneous_order_requests_return_same_order(self):
        frappe.db.commit()
        site = frappe.local.site
        sites_path = frappe.local.sites_path
        barrier = threading.Barrier(2)
        def worker():
            frappe.init(site=site, sites_path=sites_path)
            try:
                frappe.connect()
                frappe.set_user("Administrator")
                settings = frappe.get_doc("Pesapal Settings")
                barrier.wait(timeout=20)
                url = settings.get_payment_url(reference_doctype="Payment Request", reference_docname=self.pr.name,
                    payer_email="guest@example.test", payer_name="Demo Guest")
                frappe.db.commit()
                return url
            finally:
                frappe.destroy()
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(worker) for _ in range(2)]
                urls = [future.result(timeout=40) for future in futures]
            self.assertEqual(urls[0], urls[1])
            self.assertEqual(sum(c[1].endswith("SubmitOrderRequest") for c in self.http_calls), 1)
        finally:
            frappe.db.rollback()
            frappe.db.delete("Integration Request", {"reference_docname": self.pr.name})
            frappe.db.delete("Payment Request", {"name": self.pr.name})
            frappe.db.commit()
