import hmac

import frappe
from frappe.utils import get_url

from ea_payments.common import GatewaySettings, MESSAGES, provider_request, refuse, reverify

API = "https://api.flutterwave.com/v3"


class FlutterwaveSettings(GatewaySettings):
    provider = "Flutterwave"

    def headers(self):
        return {"Authorization": "Bearer " + self.get_password("secret_key"), "Content-Type": "application/json"}

    def create_order(self, pr, amount, args):
        response = provider_request("POST", API + "/payments", headers=self.headers(), json={
            "tx_ref": pr.name, "amount": amount, "currency": "UGX",
            "redirect_url": get_url("/payment-success"), "payment_options": "mobilemoneyuganda,card",
            "customer": {"email": args.get("payer_email") or pr.email_to,
                         "phonenumber": args.get("phone_number") or args.get("sender") or pr.phone_number,
                         "name": args.get("payer_name") or "Guest"}})
        url = (response.get("data") or {}).get("link")
        if not url:
            frappe.throw("PAY-006: " + MESSAGES["PAY-006"])
        return pr.name, url

    def verify(self, integration):
        response = provider_request("GET", API + "/transactions/verify_by_reference", headers=self.headers(),
                                    params={"tx_ref": integration.reference_docname})
        status = response.get("data") or {}
        return {"success": status.get("status") == "successful",
                "pending": status.get("status") in ("pending", "new"),
                "amount": status.get("amount"), "currency": status.get("currency"),
                "reference": status.get("tx_ref")}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def webhook():
    settings = frappe.get_doc("Flutterwave Settings")
    expected = settings.get_password("webhook_secret_hash", raise_exception=False) or ""
    incoming = frappe.request.headers.get("verif-hash", "")
    if not expected or not hmac.compare_digest(incoming.encode(), expected.encode()):
        return refuse("PAY-004")
    body = frappe.request.get_json(silent=True) or {}
    reference = (body.get("data") or {}).get("tx_ref")
    result = reverify(reference, "Flutterwave")
    frappe.db.commit()
    return result
