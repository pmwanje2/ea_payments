# EA Payments

MIT gateway adapters for Pesapal API 3 and Flutterwave v3, using Frappe v16,
`frappe/payments` and native Payment Request / Integration Request documents.
Only UGX whole units are supported. No ERPNext imports, payment ledger,
Transaction DocType, refunds or payouts.

Install `payments` first (qualified pin: `cca07d9f9392e2ea0e521c5975151db9e4b6c321`),
then `bench --site SITE install-app ea_payments`. Save Pesapal Settings and/or
Flutterwave Settings as System Manager. Credentials belong in the encrypted
Password fields, never in fixtures. Saving registers a native Phone gateway and
Mode of Payment; ERPNext's existing gateway-enabled hook creates its account.
Configure an Email-channel Payment Gateway Account for invoice links. Phone
requests require native SMS Settings and an authorized SMS sender role.

Allow public callbacks only at:

* `/api/method/ea_payments.ea_payments.doctype.pesapal_settings.pesapal_settings.ipn` (GET/POST)
* `/api/method/ea_payments.ea_payments.doctype.flutterwave_settings.flutterwave_settings.webhook` (POST)

Both callbacks re-query the provider before settlement. Flutterwave additionally
requires its configured `verif-hash`. Configure the site's public `host_name` and
enable the scheduler: queued requests younger than 24 hours are checked every
five minutes. Pesapal's IPN registration is saved when the first order is made;
re-register it when changing environment or public hostname.

Stable refusals: PAY-001 amount, PAY-002 currency, PAY-003 reference,
PAY-004 signature, PAY-005 SMS gateway absent, PAY-006 provider error.
Failed/cancelled payments never mark the request Paid. Payment Request's own
`set_as_paid` preserves native Phone and Email accounting behavior; the cashier
still submits the bill. POS enforcement (PAY-007) and site deployment are outside
this app's scope.

Tests: `bench --site SITE run-tests --app ea_payments` on a disposable ERPNext v16
site with a default company. Provider HTTP and SMS transport are stubbed; the
database, gateway registration, Payment Request and row locks are real.
