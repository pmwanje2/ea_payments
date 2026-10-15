import hmac

import frappe
from frappe.utils import get_url

from ea_payments.common import GatewaySettings, MESSAGES, expiry_minutes, provider_request, refuse, reverify

API = "https://api.flutterwave.com/v3"


class FlutterwaveSettings(GatewaySettings):
    provider = "Flutterwave"

    def headers(self):
        return {"Authorization": "Bearer " + self.get_password("secret_key"), "Content-Type": "application/json"}

    def create_order(self, pr, amount, args):
        response = provider_request("POST", API + "/payments", headers=self.headers(), json={
            "tx_ref": pr.name, "amount": amount, "currency": "UGX",
            "redirect_url": get_url("/payment-success"), "payment_options": "mobilemoneyuganda,card",
            # POST /v3/payments reference nests the session limit here. The guide sample
            # still shows a top-level key and customer.phonenumber; the reference does not.
            # Closing the hosted checkout is unverified until a test key proves it.
            "configuration": {"session_duration": expiry_minutes()},
            "customer": {"email": args.get("payer_email") or pr.email_to,
                         "phone_number": args.get("phone_number") or args.get("sender") or pr.phone_number,
                         "name": args.get("payer_name") or "Guest"}})
        url = (response.get("data") or {}).get("link")
        if not url:
            frappe.throw("PAY-006: " + MESSAGES["PAY-006"])
        return pr.name, url

    def verify(self, integration):
        response = provider_request("GET", API + "/transactions/verify_by_reference", headers=self.headers(),
                                    params={"tx_ref": integration.reference_docname},
                                    check_error=False, accept_statuses=(400,))
        if response.get("status") != "success":
            # Documented 400 for a tx_ref with no transaction: the checkout was never
            # started, so it is unpaid and local expiry applies. Anything else is transient.
            if "no transaction was found" in str(response.get("message") or "").lower():
                return {"pending": True, "reference": None}
            frappe.throw("PAY-006: " + MESSAGES["PAY-006"])
        status = response.get("data") or {}
        return {"success": status.get("status") == "successful",
                "pending": status.get("status") in ("pending", "new"),
                "failed": status.get("status") in ("failed", "cancelled"),
                "amount": status.get("amount"), "currency": status.get("currency"),
                "reference": status.get("tx_ref"),
                "provider_reference": status.get("flw_ref") or status.get("id")}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def webhook():
    settings = frappe.get_doc("Flutterwave Settings")
    expected = settings.get_password("webhook_secret_hash", raise_exception=False) or ""
    incoming = frappe.request.headers.get("verif-hash", "")
    if not expected or not hmac.compare_digest(incoming.encode(), expected.encode()):
        return refuse("PAY-004")
    body = frappe.request.get_json(silent=True) or {}
    if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
        return refuse("PAY-003")
    reference = (body.get("data") or {}).get("tx_ref")
    result = reverify(reference, "Flutterwave")
    frappe.db.commit()
    return result
