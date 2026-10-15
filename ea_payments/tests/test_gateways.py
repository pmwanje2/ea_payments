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
        elif url.endswith("Transactions/CancelOrder"):
            response = {"status": "200", "message": "Ok"}  # documented Pesapal API 3.0 response
        elif "/transactions/" in url and self.provider_status in ("not_found", "auth_error"):
            # Documented v3 error bodies: verify_by_reference answers HTTP 400 for an
            # unknown tx_ref; a bad secret key answers HTTP 401.
            status_code, message = ((400, "No transaction was found for this id")
                                    if self.provider_status == "not_found"
                                    else (401, "Invalid authorization key"))
            return self.response({"status": "error", "message": message, "data": None}, status_code)
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
        return self.response(response)

    def response(self, body, status_code=200):
        import requests
        class Response:
            def raise_for_status(self):
                if self.status_code >= 400:
                    raise requests.HTTPError(str(self.status_code) + " Client Error")
            def json(self):
                return body
        result = Response()
        result.status_code = status_code
        return result

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
        if status == "Cancelled":
            self.assertEqual(frappe.db.get_value("Payment Request", self.pr.name, "docstatus"), 2)
        integration.reload()
        self.assertEqual(integration.status, "Failed")
        self.assertIn(code, integration.error)

    def real_invoice(self):
        """A submitted native Sales Invoice for the default company (12000 UGX)."""
        company = frappe.get_doc("Company", frappe.defaults.get_global_default("company"))
        for dt, name, values in (
            ("Customer Group", "All Customer Groups", {"customer_group_name": "All Customer Groups", "is_group": 1}),
            ("Territory", "All Territories", {"territory_name": "All Territories", "is_group": 1}),
            ("Item Group", "All Item Groups", {"item_group_name": "All Item Groups", "is_group": 1}),
            ("Customer Group", "EA Test Guests", {"customer_group_name": "EA Test Guests", "is_group": 0,
                                                  "parent_customer_group": "All Customer Groups"}),
            ("Territory", "EA Test Uganda", {"territory_name": "EA Test Uganda", "is_group": 0,
                                             "parent_territory": "All Territories"}),
            ("Item Group", "EA Test Meals", {"item_group_name": "EA Test Meals", "is_group": 0,
                                             "parent_item_group": "All Item Groups"}),
            ("UOM", "Nos", {"uom_name": "Nos", "must_be_whole_number": 1}),
            ("Price List", "EA Test Selling", {"price_list_name": "EA Test Selling", "selling": 1,
                                               "enabled": 1, "currency": "UGX"}),
            ("Fiscal Year", str(now_datetime().year), {"year": str(now_datetime().year),
                "year_start_date": f"{now_datetime().year}-01-01", "year_end_date": f"{now_datetime().year}-12-31"}),
            ("Customer", "EA Test Guest", {"customer_name": "EA Test Guest", "customer_type": "Individual",
                                           "customer_group": "EA Test Guests", "territory": "EA Test Uganda"}),
            ("Item", "EA Test Meal", {"item_code": "EA Test Meal", "item_name": "EA Test Meal",
                                      "item_group": "EA Test Meals", "stock_uom": "Nos", "is_stock_item": 0}),
        ):
            if not frappe.db.exists(dt, name):
                frappe.get_doc({"doctype": dt, **values}).insert(ignore_permissions=True)
        invoice = frappe.get_doc({"doctype": "Sales Invoice", "company": company.name, "customer": "EA Test Guest",
            "currency": "UGX", "conversion_rate": 1, "debit_to": company.default_receivable_account,
            "selling_price_list": "EA Test Selling", "price_list_currency": "UGX", "plc_conversion_rate": 1,
            "cost_center": company.cost_center, "items": [{"item_code": "EA Test Meal", "qty": 1, "rate": 12000,
            "income_account": company.default_income_account, "cost_center": company.cost_center}]})
        invoice.insert(ignore_permissions=True)
        invoice.submit()
        return invoice

    def native_phone_request(self, invoice):
        # The host's native whitelisted method, called as a client would (no ERPNext import).
        account = frappe.db.get_value("Payment Gateway Account",
                                      {"payment_gateway": "Pesapal", "payment_channel": "Phone"}, "name")
        return frappe.call("erpnext.accounts.doctype.payment_request.payment_request.make_payment_request",
                           dt="Sales Invoice", dn=invoice.name, payment_gateway_account=account,
                           phone_number="256700000001", mute_email=1, submit_doc=1, return_doc=1)

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
        self.assertEqual(frappe.db.get_value("Payment Request", self.pr.name, "status"), "Cancelled")

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
                self.assert_unpaid("PAY-006", integration, "Cancelled")

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
        self.assertEqual(frappe.db.get_value("Payment Request", self.pr.name, "status"), "Cancelled")

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

    def test_cancelled_request_with_pending_provider_is_refused(self):
        _, integration, _ = self.order()
        self.pr.db_set("docstatus", 2)
        self.provider_status = "pending"
        result = self.ipn(integration)
        self.assertEqual(result["code"], "PAY-003")

    def test_completed_provider_payment_for_cancelled_request_is_unallocated_and_alerted(self):
        _, integration, _ = self.order()
        self.pr.db_set({"docstatus": 2, "status": "Cancelled"})
        result = self.ipn(integration)
        self.assertEqual(result["status"], 200)
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
        self.assertEqual(result["status"], 200)
        self.assertEqual(frappe.db.get_value("Payment Request", self.pr.name, "status"), "Failed")
        self.assertEqual(frappe.db.get_value("Integration Request", integration.name, "status"), "Completed")

    def test_late_completed_callback_is_idempotent_for_pay_011_notification(self):
        _, integration, _ = self.order()
        self.pr.db_set({"docstatus": 2, "status": "Cancelled"})
        self.ipn(integration)
        self.ipn(integration)
        logs = frappe.get_all("Notification Log", filters={"document_name": self.pr.name,
                                                            "title": ["like", "PAY-011%"]}, fields=["for_user"])
        managers = frappe.get_all("Has Role", filters={"role": "Accounts Manager", "parenttype": "User"}, pluck="parent")
        enabled = frappe.get_all("User", filters={"enabled": 1, "name": ["in", managers]}, pluck="name")
        self.assertCountEqual([row.for_user for row in logs], enabled)

    def enabled_users_with(self, role):
        users = frappe.get_all("Has Role", filters={"role": role, "parenttype": "User"}, pluck="parent")
        return frappe.get_all("User", filters={"enabled": 1, "name": ["in", users]}, pluck="name")

    def test_pay_011_falls_back_to_system_managers_without_an_accounts_manager(self):
        _, integration, _ = self.order()
        self.pr.db_set({"docstatus": 2, "status": "Cancelled"})
        # Rolled back in tearDown; reverify itself does not commit.
        frappe.db.delete("Has Role", {"role": "Accounts Manager", "parenttype": "User"})
        self.assertEqual(self.enabled_users_with("Accounts Manager"), [])
        self.common.reverify(integration.name, "Pesapal")
        logs = frappe.get_all("Notification Log", filters={"document_name": self.pr.name,
                                                            "title": ["like", "PAY-011%"]}, pluck="for_user")
        self.assertTrue(logs)
        self.assertCountEqual(logs, self.enabled_users_with("System Manager"))

    def test_poller_detects_late_success_for_native_cancelled_and_failed_integrations(self):
        for provider in ("Pesapal", "Flutterwave"):
            for status, docstatus in (("Cancelled", 2), ("Failed", 1)):
                with self.subTest(provider=provider, status=status):
                    self.pr = self.make_pr(provider=provider)
                    _, integration, _ = self.order(provider)
                    self.pr.db_set({"status": status, "docstatus": docstatus})
                    integration.db_set("status", status)
                    original = type(self.pr).set_as_paid
                    def paid(doc, original=original):
                        if doc.name == self.pr.name:
                            raise AssertionError("no posting for late payment")
                        return original(doc)
                    with patch.object(type(self.pr), "set_as_paid", paid):
                        self.common.poll_queued()
                    integration.reload()
                    self.assertEqual(integration.status, "Completed")
                    self.assertEqual(frappe.parse_json(integration.output)["allocation"], "Unallocated")
                    self.assertEqual(frappe.db.get_value("Payment Request", self.pr.name, "status"), status)

    def test_late_success_never_posts_money_or_publishes_pos_success(self):
        _, integration, _ = self.order()
        self.pr.db_set({"status": "Cancelled", "docstatus": 2})
        before = {dt: frappe.db.count(dt) for dt in ("Payment Entry", "GL Entry")}
        with patch.object(type(self.pr), "on_payment_authorized", side_effect=AssertionError("no authorization"), create=True), \
             patch.object(type(self.pr), "set_as_paid", side_effect=AssertionError("no posting")), \
             patch.object(frappe, "publish_realtime") as publish:
            self.ipn(integration)
        for dt, count in before.items():
            self.assertEqual(frappe.db.count(dt), count)
        self.assertFalse(any(call.kwargs.get("event") == "process_phone_payment" for call in publish.call_args_list))

    def test_late_success_mismatch_is_not_recorded_as_unallocated_receipt(self):
        for values, code in (({"provider_amount": 11999}, "PAY-001"),
                             ({"provider_currency": "USD"}, "PAY-002"),
                             ({"provider_reference": "OTHER"}, "PAY-003")):
            with self.subTest(code=code):
                self.pr = self.make_pr()
                _, integration, _ = self.order()
                self.pr.db_set({"status": "Cancelled", "docstatus": 2})
                with patch.multiple(self, **values):
                    result = self.ipn(integration)
                self.assertEqual(result["code"], code)
                self.assertFalse(frappe.db.exists("Notification Log", {"document_name": self.pr.name}))

    def test_expiry_followed_by_late_payment_is_unallocated(self):
        _, integration, _ = self.order()
        integration.db_set("creation", now_datetime() - timedelta(minutes=61))
        self.provider_status = "pending"
        self.common.poll_queued()
        self.assert_unpaid("PAY-006", integration, "Cancelled")
        self.provider_status = "success"
        self.common.poll_queued()
        self.assertEqual(frappe.db.get_value("Payment Request", self.pr.name, "status"), "Cancelled")
        integration.reload()
        self.assertEqual(integration.status, "Completed")
        self.assertEqual(frappe.parse_json(integration.output)["allocation"], "Unallocated")

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
                    self.assert_unpaid("PAY-006", integration, "Cancelled")
                    self.assertIn("PAY-006", frappe.db.get_value("Payment Request", self.pr.name, "failed_reason"))

    def test_default_expiry_fails_sparse_invalid_pesapal_order_after_60_minutes(self):
        _, integration, _ = self.order()
        self.provider_status = "pending"
        self.sparse_unpaid = True
        integration.db_set("creation", now_datetime() - timedelta(minutes=61))
        self.common.poll_queued()
        self.assert_unpaid("PAY-006", integration, "Cancelled")
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
            self.assert_unpaid("PAY-006", integration, "Cancelled")

    def test_invalid_expiry_configuration_uses_60_minutes_without_stopping_poller(self):
        self.provider_status = "pending"
        for value in ("not-minutes", -5):
            with self.subTest(value=value), patch.dict(frappe.conf, {"ea_payments_expiry_minutes": value}):
                self.pr = self.make_pr()
                _, integration, _ = self.order()
                integration.db_set("creation", now_datetime() - timedelta(minutes=30))
                try:
                    self.common.poll_queued()
                except (TypeError, ValueError) as error:
                    self.fail("Invalid expiry configuration stopped the poller: " + type(error).__name__)
                self.assertEqual(frappe.db.get_value("Payment Request", self.pr.name, "status"), "Requested")
                integration.db_set("creation", now_datetime() - timedelta(minutes=61))
                self.common.poll_queued()
                self.assert_unpaid("PAY-006", integration, "Cancelled")

    def test_expired_flutterwave_pending_or_new_order_fails(self):
        for status in ("pending", "new"):
            with self.subTest(status=status):
                self.pr = self.make_pr(provider="Flutterwave")
                _, integration, _ = self.order("Flutterwave")
                integration.db_set("creation", now_datetime() - timedelta(minutes=61))
                self.provider_status = status
                self.common.poll_queued()
                self.assert_unpaid("PAY-006", integration, "Cancelled")

    def flutterwave_order(self, age_minutes):
        self.pr = self.make_pr(provider="Flutterwave")
        _, integration, _ = self.order("Flutterwave")
        integration.db_set("creation", now_datetime() - timedelta(minutes=age_minutes))
        return integration

    def test_expiry_natively_cancels_request_so_bill_can_be_phone_paid_again(self):
        invoice = self.real_invoice()
        first = self.native_phone_request(invoice)
        integration = frappe.get_doc("Integration Request", {"reference_docname": first.name})
        integration.db_set("creation", now_datetime() - timedelta(minutes=61))
        self.provider_status = "pending"
        self.common.reverify(integration.name, "Pesapal")
        first.reload()
        self.assertEqual((first.docstatus, first.status), (2, "Cancelled"))
        self.assertIn("expired", first.failed_reason.lower())
        self.assertEqual(frappe.db.get_value("Integration Request", integration.name, "status"), "Failed")
        second = self.native_phone_request(invoice)
        self.assertEqual((second.docstatus, second.status, second.grand_total), (1, "Requested", 12000))
        self.provider_status = "success"
        self.common.reverify(integration.name, "Pesapal")
        output = frappe.parse_json(frappe.db.get_value("Integration Request", integration.name, "output"))
        self.assertEqual(output["allocation"], "Unallocated")
        self.assertTrue(frappe.db.exists("Notification Log", {"document_name": first.name,
                                                              "title": ["like", "PAY-011%"]}))
        self.assertEqual(frappe.db.get_value("Payment Request", first.name, "status"), "Cancelled")
        self.assertEqual(frappe.db.get_value("Payment Request", second.name, "status"), "Requested")

    def test_guest_ipn_terminal_failure_natively_cancels_request(self):
        _, integration, _ = self.order()
        self.provider_status = "FAILED"
        frappe.set_user("Guest")
        try:
            self.ipn(integration)
        finally:
            frappe.set_user("Administrator")
        self.assert_unpaid("PAY-006", integration, "Cancelled")

    def test_expired_flutterwave_checkout_never_opened_is_unpaid(self):
        integration = self.flutterwave_order(61)
        self.provider_status = "not_found"
        self.common.poll_queued()
        integration.reload()
        self.assertEqual(integration.status, "Failed")
        self.assertIn("expired", integration.error.lower())
        self.assertNotEqual(frappe.db.get_value("Payment Request", self.pr.name, "status"), "Requested")

    def polled(self, integration):
        return any(c[2].get("params", {}).get("orderTrackingId") == integration.name for c in self.http_calls)

    def cancel_calls(self):
        return [c for c in self.http_calls if c[1].endswith("Transactions/CancelOrder")]

    def run_queued_provider_cancels(self, integration_name):
        """Run CancelOrder jobs that were queued to start after commit."""
        from frappe.utils.background_jobs import get_queue

        ran = 0
        for qname in ("short", "default"):
            for job in list(get_queue(qname).jobs):
                payload = job.kwargs or {}
                kwargs = payload.get("kwargs") or {}
                if payload.get("method") != "ea_payments.common.cancel_provider_order":
                    continue
                if kwargs.get("integration_name") != integration_name:
                    continue
                job.delete()
                self.common.cancel_provider_order(**kwargs)
                ran += 1
        return ran

    def expired_cancelled_order(self, age):
        self.pr = self.make_pr()
        _, integration, _ = self.order()
        self.pr.db_set({"status": "Cancelled", "docstatus": 2})
        integration.db_set({"status": "Failed", "error": "PAY-006: Checkout expired without payment.",
                            "creation": now_datetime() - age})
        return integration

    def test_late_payment_window_bounds_polling_after_expiry(self):
        self.provider_status = "pending"
        inside = self.expired_cancelled_order(timedelta(minutes=60, hours=47))
        outside = self.expired_cancelled_order(timedelta(minutes=60, hours=49))
        self.common.poll_queued()
        self.assertTrue(self.polled(inside))
        self.assertFalse(self.polled(outside))

    def test_late_payment_window_is_configurable(self):
        self.provider_status = "pending"
        with patch.dict(frappe.conf, {"ea_payments_late_payment_window_hours": 2}):
            inside = self.expired_cancelled_order(timedelta(minutes=60, hours=1))
            outside = self.expired_cancelled_order(timedelta(minutes=60, hours=3))
            self.common.poll_queued()
        self.assertTrue(self.polled(inside))
        self.assertFalse(self.polled(outside))

    def test_expiry_is_capped_at_24_hours_so_open_requests_are_not_polled_past_a_day(self):
        _, integration, _ = self.order()
        integration.db_set("creation", now_datetime() - timedelta(hours=25))
        self.provider_status = "pending"
        with patch.dict(frappe.conf, {"ea_payments_expiry_minutes": 3000}):
            self.common.poll_queued()
        self.assert_unpaid("PAY-006", integration, "Cancelled")

    def test_already_failed_order_is_not_rewritten_on_every_poll(self):
        integration = self.expired_cancelled_order(timedelta(hours=2))
        before = frappe.db.get_value("Integration Request", integration.name, "modified")
        self.provider_status = "pending"
        self.common.poll_queued()
        self.assertEqual(frappe.db.get_value("Integration Request", integration.name, "modified"), before)

    def test_pesapal_order_is_cancelled_at_the_provider_once_on_expiry(self):
        _, integration, _ = self.order()
        integration.db_set("creation", now_datetime() - timedelta(minutes=61))
        self.provider_status = "pending"
        self.common.poll_queued()
        self.common.poll_queued()
        # The provider call must not run inside the poll that still holds the request rows.
        self.assertEqual(self.cancel_calls(), [])
        self.assertEqual(self.run_queued_provider_cancels(integration.name), 1)
        cancels = self.cancel_calls()
        self.assertEqual([c[2]["json"] for c in cancels], [{"order_tracking_id": integration.name}])
        self.assertEqual(cancels[0][0], "POST")

    def test_pesapal_cancel_order_http_starts_only_after_commit(self):
        _, integration, _ = self.order()
        integration.db_set("creation", now_datetime() - timedelta(minutes=61))
        self.provider_status = "pending"
        seen = []
        state = {"commits": 0, "inside": False}
        original_http = self.http
        original_commit = frappe.db.commit

        def watch(method, url, **kwargs):
            if url.endswith("Transactions/CancelOrder"):
                seen.append((state["commits"], state["inside"]))
            return original_http(method, url, **kwargs)

        def commit(*args, **kwargs):
            state["inside"] = True
            try:
                return original_commit(*args, **kwargs)
            finally:
                state["inside"] = False
                state["commits"] += 1

        with patch("requests.request", side_effect=watch), patch.object(frappe.db, "commit", commit):
            self.common.poll_queued()
            self.assertEqual(seen, [])
            self.assertGreaterEqual(state["commits"], 1)
            self.assertEqual(self.run_queued_provider_cancels(integration.name), 1)
        self.assertEqual(seen, [(state["commits"], False)])
        self.assertEqual([c[2]["json"] for c in self.cancel_calls()], [{"order_tracking_id": integration.name}])

    def test_deferred_cancel_enqueue_failure_does_not_abort_polling(self):
        # Redis fails when the after-commit enqueue actually runs, not at registration.
        import redis
        from rq import Queue
        expired = []
        for _ in range(2):
            self.pr = self.make_pr()
            _, integration, _ = self.order()
            integration.db_set("creation", now_datetime() - timedelta(minutes=61))
            expired.append((self.pr.name, integration))
        self.provider_status = "pending"
        with patch.object(Queue, "enqueue_call", side_effect=redis.exceptions.ConnectionError("queue down")):
            try:
                self.common.poll_queued()
            except Exception as error:
                self.fail("Deferred cancel enqueue failure aborted polling: " + type(error).__name__)
        for pr_name, integration in expired:
            with self.subTest(pr=pr_name):
                self.assertEqual(frappe.db.get_value("Payment Request", pr_name, ["status", "docstatus"]),
                                 ("Cancelled", 2))
                self.assertEqual(frappe.db.get_value("Integration Request", integration.name, "status"), "Failed")
        self.assertEqual(self.cancel_calls(), [])

    def test_deferred_cancel_enqueue_failure_is_recorded_as_an_error_log(self):
        # Redis fails when the after-commit enqueue runs. The failure must be an
        # Error Log: this runtime drops ea_payments warnings. The message is the
        # Integration Request name and the exception type only.
        import redis
        from rq import Queue
        _, integration, _ = self.order()
        integration.db_set("creation", now_datetime() - timedelta(minutes=61))
        self.provider_status = "pending"
        secret = "queue down redis://user:must-not-log@10.1.2.3:6379"
        with patch.object(Queue, "enqueue_call", side_effect=redis.exceptions.ConnectionError(secret)):
            try:
                self.common.poll_queued()
            except Exception as error:
                self.fail("Deferred cancel enqueue failure aborted polling: " + type(error).__name__)
        self.assertEqual(frappe.db.get_value("Payment Request", self.pr.name, ["status", "docstatus"]),
                         ("Cancelled", 2))
        self.assertEqual(frappe.db.get_value("Integration Request", integration.name, "status"), "Failed")
        self.assertEqual(self.cancel_calls(), [])
        # Other failures in this process also record a log, and the insert survives
        # rollback once a later commit lands. Match this Integration Request only.
        expected = integration.name + ": ConnectionError"
        logs = frappe.get_all("Error Log", filters={
            "method": "PAY-012 provider cancel not queued", "error": expected,
        }, fields=["method", "error"])
        self.assertEqual([(row.method, row.error) for row in logs],
                         [("PAY-012 provider cancel not queued", expected)])
        leaked = frappe.get_all("Error Log", filters={"error": ["like", "%must-not-log%"]}, pluck="name")
        self.assertEqual(leaked, [])

    def test_replayed_cancel_job_does_not_send_cancel_order_again(self):
        _, integration, _ = self.order()
        integration.db_set("creation", now_datetime() - timedelta(minutes=61))
        self.provider_status = "pending"
        self.common.poll_queued()
        self.assertEqual(self.run_queued_provider_cancels(integration.name), 1)
        sent = frappe.parse_json(frappe.db.get_value("Integration Request", integration.name, "data"))
        self.assertTrue(sent.get("provider_cancel_sent"))
        # A finished job can be re-enqueued (deduplicate only covers queued/started jobs) or replayed.
        self.common.cancel_provider_order(integration_name=integration.name)
        self.common.cancel_at_provider(frappe.get_doc("Integration Request", integration.name))
        frappe.db.commit()
        self.run_queued_provider_cancels(integration.name)
        self.assertEqual([c[2]["json"] for c in self.cancel_calls()], [{"order_tracking_id": integration.name}])
        data = frappe.parse_json(frappe.db.get_value("Integration Request", integration.name, "data"))
        self.assertEqual(data["checkout_url"], sent["checkout_url"])

    def test_provider_cancel_failure_does_not_block_local_expiry(self):
        _, integration, _ = self.order()
        integration.db_set("creation", now_datetime() - timedelta(minutes=61))
        self.provider_status = "pending"
        original = self.http
        def cancel_down(method, url, **kwargs):
            if url.endswith("Transactions/CancelOrder"):
                self.http_calls.append((method, url, kwargs))
                raise RuntimeError("provider down")
            return original(method, url, **kwargs)
        with patch("requests.request", side_effect=cancel_down):
            self.common.poll_queued()
            # Local expiry must already be done before the provider call is attempted.
            self.assert_unpaid("PAY-006", integration, "Cancelled")
            self.assertEqual(self.cancel_calls(), [])
            self.run_queued_provider_cancels(integration.name)
        self.assertEqual(len(self.cancel_calls()), 1)
        self.assert_unpaid("PAY-006", integration, "Cancelled")

    def test_flutterwave_customer_phone_uses_documented_field(self):
        self.pr.db_set("payment_gateway", "Flutterwave")
        self.order("Flutterwave", phone_number="256700000009", sender="256700000002")
        payload = [c[2]["json"] for c in self.http_calls if c[1].endswith("/payments")][-1]
        customer = payload["customer"]
        self.assertEqual(customer.get("phone_number"), "256700000009")
        self.assertNotIn("phonenumber", customer)

    def test_flutterwave_checkout_session_lasts_only_until_local_expiry(self):
        for configured, expected in ((None, 60), (90, 90), (3000, 1440)):
            with self.subTest(configured=configured), patch.dict(frappe.conf, {"ea_payments_expiry_minutes": configured}):
                self.flutterwave_order(0)
                payload = [c[2]["json"] for c in self.http_calls if c[1].endswith("/payments")][-1]
                self.assertEqual((payload.get("configuration") or {}).get("session_duration"), expected)
                self.assertNotIn("session_duration", payload)
                self.assertNotIn("configurations", payload)

    def test_young_flutterwave_checkout_never_opened_stays_open(self):
        integration = self.flutterwave_order(30)
        self.provider_status = "not_found"
        self.common.poll_queued()
        self.assertEqual(frappe.db.get_value("Integration Request", integration.name, "status"), "Queued")
        self.assertEqual(frappe.db.get_value("Payment Request", self.pr.name, "status"), "Requested")

    def test_expired_flutterwave_auth_error_stays_transient(self):
        integration = self.flutterwave_order(61)
        self.provider_status = "auth_error"
        self.common.poll_queued()
        self.assert_unpaid("PAY-006", integration)
        self.assertEqual(integration.error, "PAY-006: " + self.common.MESSAGES["PAY-006"])

    def test_expired_provider_outage_does_not_claim_payment_failed(self):
        _, integration, _ = self.order()
        integration.db_set("creation", now_datetime() - timedelta(hours=25))
        self.http_error = True
        self.common.poll_queued()
        self.assert_unpaid("PAY-006", integration)

    def test_poller_retries_provider_outage_then_expires_confirmed_unpaid_order(self):
        _, integration, _ = self.order()
        integration.db_set("creation", now_datetime() - timedelta(minutes=61))
        self.http_error = True
        self.common.poll_queued()
        self.assert_unpaid("PAY-006", integration)
        self.http_error = False
        self.provider_status = "pending"
        self.common.poll_queued()
        self.assert_unpaid("PAY-006", integration, "Cancelled")

    def test_poller_retries_provider_outage_then_settles_confirmed_success(self):
        _, integration, _ = self.order()
        self.http_error = True
        self.common.poll_queued()
        self.assert_unpaid("PAY-006", integration)
        self.http_error = False
        self.common.poll_queued()
        self.assertEqual(frappe.db.get_value("Payment Request", self.pr.name, "status"), "Paid")

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
