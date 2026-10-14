app_name = "ea_payments"
app_title = "EA Payments"
app_publisher = "pmwanje2"
app_description = "Pesapal and Flutterwave native Payment Request gateways"
app_email = "opensource@maximusglobal.net"
app_license = "MIT"
required_apps = ["frappe", "payments"]
scheduler_events = {"cron": {"*/5 * * * *": ["ea_payments.common.poll_queued"]}}
