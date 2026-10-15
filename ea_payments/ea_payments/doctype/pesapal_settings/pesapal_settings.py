import frappe
from frappe.utils import get_url

from ea_payments.common import GatewaySettings, MESSAGES, provider_request, refuse, reverify

CALLBACK = "/api/method/ea_payments.ea_payments.doctype.pesapal_settings.pesapal_settings.ipn"


class PesapalSettings(GatewaySettings):
    provider = "Pesapal"

    def validate(self):
        # The registered IPN id belongs to one environment and merchant.
        if any(self.has_value_changed(f) for f in ("environment", "consumer_key", "consumer_secret")):
            self.ipn_id = ""

    def headers(self):
        self.api = ("https://cybqa.pesapal.com/pesapalv3/api" if self.environment == "Sandbox"
                    else "https://pay.pesapal.com/v3/api")
        token = provider_request("POST", self.api + "/Auth/RequestToken", json={
            "consumer_key": self.get_password("consumer_key"),
            "consumer_secret": self.get_password("consumer_secret")})
        if not token.get("token"):
            frappe.throw("PAY-006: " + MESSAGES["PAY-006"])
        return {"Authorization": "Bearer " + token["token"], "Content-Type": "application/json"}

    def create_order(self, pr, amount, args):
        headers = self.headers()
        if not self.ipn_id:
            response = provider_request("POST", self.api + "/URLSetup/RegisterIPN", headers=headers,
                                        json={"url": get_url(CALLBACK), "ipn_notification_type": "GET"})
            if not response.get("ipn_id"):
                frappe.throw("PAY-006: " + MESSAGES["PAY-006"])
            self.db_set("ipn_id", response["ipn_id"])
        order = provider_request("POST", self.api + "/Transactions/SubmitOrderRequest", headers=headers,
            json={"id": pr.name, "currency": "UGX", "amount": amount, "description": "Payment " + pr.name,
                  "callback_url": get_url("/payment-success"), "notification_id": self.ipn_id,
                  "billing_address": {"email_address": args.get("payer_email") or pr.email_to,
                      "phone_number": args.get("phone_number") or args.get("sender") or pr.phone_number,
                      "country_code": "UG", "first_name": args.get("payer_name") or "Guest"}})
        if not order.get("order_tracking_id") or not order.get("redirect_url"):
            frappe.throw("PAY-006: " + MESSAGES["PAY-006"])
        return order["order_tracking_id"], order["redirect_url"]

    def verify(self, integration):
        headers = self.headers()
        status = provider_request("GET", self.api + "/Transactions/GetTransactionStatus", headers=headers,
                                  params={"orderTrackingId": integration.name}, check_error=False)
        # 0 INVALID/pending -> stay Queued, 1 COMPLETED, 2 FAILED, 3 REVERSED; else transport/auth error.
        code = status.get("status_code")
        if code not in (0, 1, 2, 3):
            frappe.throw("PAY-006: " + MESSAGES["PAY-006"])
        return {"success": code == 1, "pending": code == 0, "failed": code in (2, 3),
                "amount": status.get("amount"), "currency": status.get("currency"),
                "reference": status.get("merchant_reference"), "provider_reference": integration.name}

    def cancel_order(self, integration):
        # Documented API 3.0 OrderCancellation: pending or failed orders only, once.
        headers = self.headers()
        provider_request("POST", self.api + "/Transactions/CancelOrder", headers=headers,
                         json={"order_tracking_id": integration.name})


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
def ipn(OrderTrackingId=None, OrderMerchantReference=None, OrderNotificationType="IPNCHANGE", **kwargs):
    reference = frappe.db.get_value("Integration Request", OrderTrackingId or "", "reference_docname")
    if not reference or reference != OrderMerchantReference:
        result = refuse("PAY-003")
    else:
        result = reverify(OrderTrackingId, "Pesapal")
    frappe.db.commit()
    ack = {"orderNotificationType": OrderNotificationType, "orderTrackingId": OrderTrackingId,
            "orderMerchantReference": OrderMerchantReference,
            "status": 200 if not result.get("code") else 500,
            **({"code": result["code"]} if result.get("code") else {})}
    frappe.response.update(ack)
    return ack
