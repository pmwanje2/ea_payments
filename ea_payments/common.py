"""Shared native audit, lock and settlement. Never persist raw PSP responses."""
from datetime import timedelta
from decimal import Decimal, InvalidOperation

import frappe
import requests
from frappe.core.doctype.sms_settings.sms_settings import send_sms
from frappe.model.document import Document
from frappe.utils import call_hook_method, escape_html, get_datetime, now_datetime
from payments.utils import create_payment_gateway


MESSAGES = {
    "PAY-001": "Amount must equal the Payment Request in whole UGX.",
    "PAY-002": "Only matching UGX currency is accepted.",
    "PAY-003": "Unknown or foreign payment reference.",
    "PAY-004": "Invalid webhook signature.",
    "PAY-005": "Configure native SMS Settings before requesting phone payment.",
    "PAY-006": "Provider could not confirm the payment.",
}


def _setting(key, default, maximum):
    """Positive whole-number site config, else the default; capped at maximum."""
    try:
        value = int(frappe.conf.get(key) or default)
    except (TypeError, ValueError):
        value = default
    return min(value if value > 0 else default, maximum)


def expiry_minutes():
    # 1440 = Flutterwave's maximum checkout session; open requests never outlive a day.
    return _setting("ea_payments_expiry_minutes", 60, 1440)


def late_window_hours():
    return _setting("ea_payments_late_payment_window_hours", 48, 24 * 30)


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


def provider_request(method, url, check_error=True, accept_statuses=(), **kwargs):
    """check_error=False: caller decides from the body (Pesapal status always carries an error object)."""
    try:
        response = requests.request(method, url, timeout=20, **kwargs)
        if response.status_code not in accept_statuses:
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
    if (pr.docstatus not in (1, 2) or pr.payment_request_type != "Inward"
            or pr.payment_gateway != integration.integration_request_service
            or (verified.get("reference") != pr.name and not (
                verified.get("reference") is None and (verified.get("pending") or verified.get("failed"))))):
        return refuse("PAY-003", integration)
    if verified.get("pending") or verified.get("failed"):
        if integration.status == "Completed":
            return {"status": "Completed"}
        expired = now_datetime() > get_datetime(integration.creation) + timedelta(minutes=expiry_minutes())
        if verified.get("failed") or expired:
            reason = ("PAY-006: Checkout expired without payment." if expired and verified.get("pending")
                      else "PAY-006: Provider reported a failed, reversed or cancelled payment.")
            if (integration.status, integration.error) != ("Failed", reason):
                integration.db_set({"status": "Failed", "error": reason})
            if pr.docstatus == 1 and pr.status in ("Requested", "Initiated"):
                # Native cancel releases the requested amount, so the bill can be
                # phone-paid again (ERPNext counts submitted requests' outstanding).
                # Callbacks run as Guest; ERPNext's own cancel_old_payment_requests
                # cancels with ignore_permissions the same way.
                pr.db_set("failed_reason", reason)
                pr.flags.ignore_permissions = True
                pr.cancel()
                cancel_at_provider(integration)
            return {"status": "Failed", "code": "PAY-006", "message": reason}
        if pr.docstatus == 2:
            return refuse("PAY-003", integration)
        return {"status": "Queued"}
    if verified.get("currency") != "UGX" or pr.currency != "UGX":
        return refuse("PAY-002", integration)
    try:
        amount = whole_ugx(verified.get("amount"))
        if amount != whole_ugx(pr.grand_total):
            return refuse("PAY-001", integration)
    except frappe.ValidationError:
        return refuse("PAY-001", integration)
    if not verified.get("success"):
        return refuse("PAY-006", integration)
    if pr.docstatus == 2 or pr.status in ("Cancelled", "Failed"):
        return unallocated(pr, integration, verified, amount)
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


def cancel_at_provider(integration):
    """Best effort, once, where the provider documents it (Pesapal CancelOrder).
    Queue the call until after commit: it can take two 20-second timeouts, and
    settle() is still holding the Payment Request and Integration Request locks.
    A late success is still caught by the callback or the late-payment poll."""
    settings = frappe.get_doc(integration.integration_request_service + " Settings")
    if not hasattr(settings, "cancel_order"):
        return
    try:
        frappe.enqueue(
            "ea_payments.common.cancel_provider_order",
            queue="short",
            timeout=120,
            enqueue_after_commit=True,
            integration_name=integration.name,
            job_id="ea-payments-cancel-" + integration.name,
            deduplicate=True,
        )
    except Exception:
        # Redis or queue setup must not roll back the local expiry already done above.
        pass


def cancel_provider_order(integration_name):
    integration = frappe.get_doc("Integration Request", integration_name)
    settings = frappe.get_doc(integration.integration_request_service + " Settings")
    if hasattr(settings, "cancel_order"):
        try:
            settings.cancel_order(integration)
        except frappe.ValidationError:
            pass


def unallocated(pr, integration, verified, amount):
    """Native audit + Accounts alert only. Never authorize, post or refund money."""
    reference = str(verified.get("provider_reference") or integration.name)
    integration.db_set({"status": "Completed", "error": "", "output": frappe.as_json({
        "status": "Completed - Unallocated", "allocation": "Unallocated", "code": "PAY-011",
        "amount": amount, "currency": "UGX", "reference": pr.name, "provider_reference": reference})})
    state = "cancelled" if pr.docstatus == 2 or pr.status == "Cancelled" else "failed"
    message = (f"PAY-011: Phone payment received for {state} Payment Request {pr.name} "
               f"({amount} UGX, provider reference {reference}). Refund or allocate it.")
    for user in enabled_users_with("Accounts Manager") or enabled_users_with("System Manager"):
        key = {"for_user": user, "document_type": "Payment Request", "document_name": pr.name,
               "source_doctype": "Integration Request", "source_name": integration.name}
        if not frappe.db.exists("Notification Log", key):
            frappe.get_doc({"doctype": "Notification Log", **key,
                            "title": "PAY-011: Unallocated phone payment",
                            "description": escape_html(message)}).insert(ignore_permissions=True)
    return {"status": "Completed", "allocation": "Unallocated"}


def enabled_users_with(role):
    users = frappe.get_all("Has Role", filters={"role": role, "parenttype": "User"}, pluck="parent")
    return frappe.get_all("User", filters={"enabled": 1, "name": ["in", users]}, pluck="name") if users else []


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
    # Open requests settle or expire within expiry_minutes (at most a day); cancelled or
    # failed ones are re-checked for late receipts only during the late-payment window.
    oldest = now_datetime() - timedelta(minutes=expiry_minutes(), hours=late_window_hours())
    for row in frappe.get_all("Integration Request", filters={
        "integration_request_service": ["in", ["Pesapal", "Flutterwave"]],
        "status": ["in", ["Queued", "Failed", "Cancelled"]],
        "creation": [">", oldest],
    }, fields=["name", "integration_request_service", "reference_docname", "status", "error"]):
        transient_error = row.status == "Failed" and row.error == "PAY-006: " + MESSAGES["PAY-006"]
        if row.status != "Queued" and not transient_error and frappe.db.get_value(
                "Payment Request", row.reference_docname, "status") not in ("Failed", "Cancelled"):
            continue
        reverify(row.name, row.integration_request_service)
        frappe.db.commit()
