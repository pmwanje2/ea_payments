"""Shared native audit, lock and settlement. Never persist raw PSP responses."""
from datetime import timedelta
from decimal import Decimal, InvalidOperation

import frappe
import requests
from frappe.core.doctype.sms_settings.sms_settings import send_sms
from frappe.model.document import Document
from frappe.utils import call_hook_method, now_datetime
from payments.utils import create_payment_gateway


MESSAGES = {
    "PAY-001": "Amount must equal the Payment Request in whole UGX.",
    "PAY-002": "Only matching UGX currency is accepted.",
    "PAY-003": "Unknown or foreign payment reference.",
    "PAY-004": "Invalid webhook signature.",
    "PAY-005": "Configure native SMS Settings before requesting phone payment.",
    "PAY-006": "Provider could not confirm the payment.",
}


def refuse(code, integration=None):
    message = code + ": " + MESSAGES[code]
    if integration and integration.status != "Completed":
        integration.db_set({"status": "Failed", "error": message})
    return {"status": "Failed", "code": code, "message": message}


def whole_ugx(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount != amount.to_integral_value() or amount <= 0:
            raise ValueError
        return int(amount)
    except (InvalidOperation, ValueError, TypeError):
        frappe.throw("PAY-001: " + MESSAGES["PAY-001"])


def provider_request(method, url, check_error=True, **kwargs):
    """check_error=False: caller decides from the body (Pesapal status always carries an error object)."""
    try:
        response = requests.request(method, url, timeout=20, **kwargs)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or (check_error and (
                data.get("error") or data.get("status") in ("error", "failed"))):
            raise ValueError
        return data
    except Exception:
        # Exception text and provider payloads can include merchant secrets.
        frappe.throw("PAY-006: " + MESSAGES["PAY-006"])


class GatewaySettings(Document):
    def validate_transaction_currency(self, currency):
        if currency != "UGX":
            frappe.throw("PAY-002: " + MESSAGES["PAY-002"])

    def on_update(self):
        create_payment_gateway(self.provider, settings=self.doctype)
        call_hook_method("payment_gateway_enabled", gateway=self.provider, payment_channel="Phone")
        if frappe.db.exists("DocType", "Mode of Payment") and not frappe.db.exists("Mode of Payment", self.provider):
            accounts = []
            for gateway in frappe.get_all("Payment Gateway Account", filters={"payment_gateway": self.provider}, fields=["payment_account"]):
                company = frappe.db.get_value("Account", gateway.payment_account, "company")
                if company and not any(row["company"] == company for row in accounts):
                    accounts.append({"company": company, "default_account": gateway.payment_account})
            frappe.get_doc({"doctype": "Mode of Payment", "mode_of_payment": self.provider,
                            "type": "Phone", "enabled": 1, "accounts": accounts}).insert(ignore_permissions=True)

    def get_payment_url(self, **kwargs):
        name = kwargs.get("reference_docname")
        if kwargs.get("reference_doctype") != "Payment Request" or not name or not frappe.db.exists("Payment Request", name):
            frappe.throw("PAY-003: " + MESSAGES["PAY-003"])
        for attempt in range(2):
            try:
                pr = frappe.get_doc("Payment Request", name, for_update=True)
                if pr.payment_gateway != self.provider or pr.docstatus == 2 or pr.payment_request_type != "Inward":
                    frappe.throw("PAY-003: " + MESSAGES["PAY-003"])
                self.validate_transaction_currency(pr.currency)
                amount = whole_ugx(pr.grand_total)
                existing = frappe.db.get_value("Integration Request", {
                    "reference_doctype": "Payment Request", "reference_docname": pr.name,
                    "integration_request_service": self.provider,
                }, "name", for_update=True)
                break
            except frappe.QueryDeadlockError:
                if attempt:
                    raise
                frappe.db.rollback()  # No provider request or local write has occurred.
        if existing:
            return frappe.parse_json(frappe.get_doc("Integration Request", existing, for_update=True).data)["checkout_url"]
        order_id, url = self.create_order(pr, amount, kwargs)
        integration = frappe.get_doc({
            "doctype": "Integration Request", "integration_request_service": self.provider,
            "reference_doctype": "Payment Request", "reference_docname": pr.name,
            "status": "Queued", "data": frappe.as_json({"checkout_url": url,
                "request_amount": amount, "currency": "UGX", "reference": pr.name}),
        })
        integration.flags._name = order_id
        integration.insert(ignore_permissions=True)
        return url

    def request_for_payment(self, **kwargs):
        if not frappe.db.get_single_value("SMS Settings", "sms_gateway_url"):
            frappe.throw("PAY-005: " + MESSAGES["PAY-005"])
        phone = kwargs.get("phone_number") or kwargs.get("sender")
        url = self.get_payment_url(**kwargs)
        send_sms(receiver_list=[phone], msg="Complete your payment: " + url)
        return url


def settle(integration, verified):
    """Serialize IPN/poller races on the ERP authority row, not on PSP state."""
    name = integration.reference_docname
    if integration.reference_doctype != "Payment Request" or not frappe.db.exists("Payment Request", name):
        return refuse("PAY-003", integration)
    pr = frappe.get_doc("Payment Request", name, for_update=True)
    integration = frappe.get_doc("Integration Request", integration.name, for_update=True)
    if (pr.docstatus != 1 or pr.payment_request_type != "Inward"
            or pr.payment_gateway != integration.integration_request_service
            or verified.get("reference") != pr.name):
        return refuse("PAY-003", integration)
    if verified.get("currency") != "UGX" or pr.currency != "UGX":
        return refuse("PAY-002", integration)
    try:
        amount = whole_ugx(verified.get("amount"))
        if amount != whole_ugx(pr.grand_total):
            return refuse("PAY-001", integration)
    except frappe.ValidationError:
        return refuse("PAY-001", integration)
    if verified.get("pending"):
        return {"status": "Queued"}
    if not verified.get("success"):
        return refuse("PAY-006", integration)
    integration.db_set({"status": "Completed", "error": "", "output": frappe.as_json({
        "amount": amount, "currency": "UGX", "reference": pr.name, "status": "Completed"})})
    if pr.status != "Paid":
        pr.run_method("on_payment_authorized", "Completed")
        pr.reload()
        if pr.status != "Paid":
            pr.set_as_paid()
        if pr.payment_channel == "Phone":
            frappe.publish_realtime(event="process_phone_payment", doctype=pr.reference_doctype,
                docname=pr.reference_name, user=pr.owner, after_commit=True,
                message={"amount": amount, "success": True, "failure_message": ""})
    return {"status": "Completed"}


def reverify(name, provider):
    if not isinstance(name, str) or not frappe.db.exists("Integration Request", {
        "name": name, "integration_request_service": provider}):
        return refuse("PAY-003")
    integration = frappe.get_doc("Integration Request", name)
    try:
        verified = frappe.get_doc(provider + " Settings").verify(integration)
    except frappe.ValidationError:
        return refuse("PAY-006", integration)
    try:
        return settle(integration, verified)
    except frappe.QueryDeadlockError:
        # MariaDB snapshot isolation rejects an older snapshot after the other
        # callback wins the lock. This entrypoint has made no changes yet.
        frappe.db.rollback()
        return settle(frappe.get_doc("Integration Request", name), verified)


def poll_queued():
    for row in frappe.get_all("Integration Request", filters={
        "integration_request_service": ["in", ["Pesapal", "Flutterwave"]],
        "status": "Queued", "creation": [">", now_datetime() - timedelta(hours=24)],
    }, fields=["name", "integration_request_service"]):
        reverify(row.name, row.integration_request_service)
        frappe.db.commit()
