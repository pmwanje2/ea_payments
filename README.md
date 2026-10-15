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
enable the scheduler: open requests are checked every five minutes until they
settle or expire, and expired/failed/cancelled ones only during the late-payment
window (below); after that the poller stops and only callbacks can still arrive.
Pesapal's IPN registration is saved when the first order is made.
Changing Environment or either credential clears it, so the next order registers
a new IPN; after changing the public hostname, re-enter the consumer secret.
Pesapal status is decided by `status_code` (its response always carries an
`error` object): 0 stays pending until expiry, 1 settles, 2/3 fail as PAY-006.

An unpaid checkout expires after 60 minutes by default, measured from the
Integration Request's creation (the provider order), not the Payment Request's
creation. Configure positive whole minutes with
`bench --site SITE set-config ea_payments_expiry_minutes 60`.
Invalid or nonpositive values use the 60-minute default; values above 1440 (one
day, Flutterwave's maximum checkout session) are capped at 1440.
Pesapal FAILED/REVERSED, Flutterwave failed/cancelled, and confirmed pending
orders older than that limit set the Integration Request to `Failed` and natively
cancel the still-open Payment Request (its `failed_reason` says why). Cancelling
releases the requested amount, so a new phone request for the same bill works,
and it releases open-request settlement fences such as PAY-010. A Flutterwave
`No transaction was found` answer for our own tx_ref means the checkout was never
started, so it counts as unpaid. Other provider/authentication errors are not
proof of nonpayment: they leave the Payment Request open and are retried.

To stop late payments at the source, Flutterwave checkouts are created with
`configuration.session_duration` equal to the expiry (the current Standard API
reference nests it there). Whether that closes the hosted checkout is unverified
until a Flutterwave test key proves it. An expired or failed Pesapal order is
cancelled once with the documented `Transactions/CancelOrder` endpoint, queued
with `frappe.enqueue(..., enqueue_after_commit=True)` so the request rows are
not locked during the provider timeouts (best effort; local expiry does not
depend on it).

The poller re-checks cancelled/failed requests for late payment for 48 hours after
expiry (`ea_payments_late_payment_window_hours`, measured from order creation plus
the expiry). A verified,
matching success for a cancelled or Failed Payment Request never calls native
authorization or `set_as_paid`, posts money, or announces POS success. The native
Integration Request is `Completed`, with `Completed - Unallocated`, amount,
provider reference and PAY-011 in its JSON output (no native schema change).
Each enabled user with the Accounts Manager role (or, if there is none, the System
Manager role) receives one native
Notification Log per integration: "Phone payment received for cancelled Payment
Request X (amount, provider reference). Refund or allocate it." Repeated callbacks
do not duplicate the alert. No refund or allocation happens automatically.

Stable refusals: PAY-001 amount, PAY-002 currency, PAY-003 reference,
PAY-004 signature, PAY-005 SMS gateway absent, PAY-006 provider error.
Failed/cancelled payments never mark the request Paid. Payment Request's own
`set_as_paid` preserves native Phone and Email accounting behavior; the cashier
still submits the bill. POS enforcement (PAY-007) and site deployment are outside
this app's scope.

Tests: `bench --site SITE run-tests --app ea_payments` on a disposable ERPNext v16
site with a default company. Provider HTTP and SMS transport are stubbed; the
database, gateway registration, Payment Request and row locks are real.
