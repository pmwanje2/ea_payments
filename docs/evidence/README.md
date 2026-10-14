# P1 proof-lab evidence

Scope: approved [#3445 section 6.1](https://github.com/Maximus-Tech-Enterprise-Apps/maximus-siteops/issues/3445), tracker #3712 P1 only. No SiteOps, URY or live-system changes.

## Revisions and TDD

- Initial RED: `5a4e20bc692b780ee66f6c88dcd0b77ad27f6c21` (tests only); [capture](red-initial.txt): 33 tests, 33 failures for the missing Pesapal Settings DocType, exit 1. Because no app existed yet, the scratch `red_runner.py` connected to the real Frappe site and ran unittest discovery on the tests-only directory; this was not a bench app-install or import failure.
- Initial GREEN: `6fb2a2c08a40a233de72a510677412c3bb406c12`; [capture](green-initial.txt): 33 tests passed using `bench --site p1.localhost run-tests --app ea_payments`. This commit also corrects the race-test connection harness to use Frappe v16's `init/connect/destroy`; assertions are unchanged.
- Additional tests-only RED commits: `dccbae6`, `96a0ce5`, `8965f08`; [capture](red-hardening.txt): 36 tests, 1 failure and 2 errors exposing malformed callback handling, missing top-level Pesapal acknowledgement and concurrent order snapshot races.
- Hardening GREEN: `33d2359ace5b2cb59fa123b4e8c005bce48dac14`; [capture](green-hardening.txt): 36 passed.
- Test-only isolation correction: `b7e3dae28a229da2eac0d5f5024d7eca1bc73165`. The [preceding run](test-isolation-failure.txt) counted settlement of an unrelated queued screenshot-demo request. The spy now counts only its own Payment Request; the native poller and production code are unchanged.
- [Final capture](green-final.txt): 36 tests passed in 3.213 seconds, no failures/errors/skips. Captured at `b7e3dae`; subsequent evidence-only commits do not change app/test source.
- [Build](build-final.txt): `bench build --app ea_payments` passed.
- [Gitleaks](gitleaks.txt): 8 implementation/evidence commits scanned, no leaks. The final evidence commit is scanned again before push.

Text captures preserve command output with ANSI styling, carriage returns, traceback indentation tabs and trailing whitespace normalized. All provider and SMS HTTP calls were stubbed; credentials and all records are disposable demo data, not provider sandbox transactions.

## Environment and cleanup

- Compose project `p1-ea-payments-ekqdcahp`; image `frappe/erpnext:v16.50.0`, image ID `sha256:0c84219faf21600d824408daf39c4bddd21d0ef4898a4aae36bf3cc57b95f5e2`.
- Runtime: Frappe 16.52.0, ERPNext 16.50.0, Python 3.14.8, MariaDB 11.8. The image omits Frappe/ERPNext Git metadata; these are runtime versions, not source-SHA attestations.
- payments version-16 `cca07d9f9392e2ea0e521c5975151db9e4b6c321`.
- Only listener: `127.0.0.1:19371`; site `p1.localhost`.
- Fresh headless Chrome/CDP profile, 1440x1100 captures. [Capture log](screenshots.txt) confirms its removal.
- Cookie-free guest HTTP calls additionally verified [successful acknowledgement](guest-ipn.json) and [PAY-001 refusal](guest-refusal.json). The authenticated Desk screenshot flow used Administrator; it is not ordinary-cashier-role acceptance.
- [Teardown](teardown.txt): exact project `docker compose -p p1-ea-payments-ekqdcahp -f compose.yaml down -v` removed its three containers, three volumes and network. Follow-up label queries returned no project resources. Temporary Chrome profile is absent.

## Screenshots

1. [Pesapal Settings saved, Sandbox, masked credentials](01-pesapal-settings-sandbox.png)
2. [Native Payment Gateway](02-payment-gateway.png)
3. [Phone Mode of Payment](03-phone-mode-of-payment.png)
4. [Payment Request Requested](04-payment-request-requested.png)
5. [Same Payment Request Paid after verified stub IPN](05-payment-request-paid.png)
6. [Integration Request Completed](06-integration-request-completed.png)
7. [PAY-001 amount mismatch refusal](07-pay-001-refusal.png)
8. [Refused Payment Request remains Requested](08-refused-payment-stays-requested.png)

## CANNOT-VERIFY / out of scope

- Pesapal real sandbox: no public-demo-key transaction was performed; HTTP is stubbed.
- Flutterwave real sandbox: no credentials available; HTTP is stubbed.
- Independent verification-specialist initially could not execute commands: `bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted`. No independent PASS is claimed.
- CodeRabbit review rejected the connection: `Invalid organization`.
- Ordinary-role acceptance, ring0/live deployment, URY changes, PAY-007 configuration and Email-channel accounting acceptance belong to later tracker items and are not proven here.

## Changed-file manifest

Production Python: **297 physical lines**, including blank/comment lines and hooks/version metadata (no tests). Exactly two DocType JSON files. No extra Transaction DocType, ERPNext imports, refunds/payouts or service.

```text
.gitignore
README.md
pyproject.toml
ea_payments/__init__.py
ea_payments/common.py
ea_payments/hooks.py
ea_payments/modules.txt
ea_payments/patches.txt
ea_payments/public/.gitkeep
ea_payments/ea_payments/__init__.py
ea_payments/ea_payments/doctype/__init__.py
ea_payments/ea_payments/doctype/pesapal_settings/__init__.py
ea_payments/ea_payments/doctype/pesapal_settings/pesapal_settings.json
ea_payments/ea_payments/doctype/pesapal_settings/pesapal_settings.py
ea_payments/ea_payments/doctype/flutterwave_settings/__init__.py
ea_payments/ea_payments/doctype/flutterwave_settings/flutterwave_settings.json
ea_payments/ea_payments/doctype/flutterwave_settings/flutterwave_settings.py
ea_payments/tests/__init__.py
ea_payments/tests/test_gateways.py
docs/evidence/README.md
docs/evidence/01-pesapal-settings-sandbox.png
docs/evidence/02-payment-gateway.png
docs/evidence/03-phone-mode-of-payment.png
docs/evidence/04-payment-request-requested.png
docs/evidence/05-payment-request-paid.png
docs/evidence/06-integration-request-completed.png
docs/evidence/07-pay-001-refusal.png
docs/evidence/08-refused-payment-stays-requested.png
docs/evidence/red-initial.txt
docs/evidence/green-initial.txt
docs/evidence/red-hardening.txt
docs/evidence/green-hardening.txt
docs/evidence/test-isolation-failure.txt
docs/evidence/green-final.txt
docs/evidence/build-final.txt
docs/evidence/gitleaks.txt
docs/evidence/screenshots.txt
docs/evidence/guest-ipn.json
docs/evidence/guest-refusal.json
docs/evidence/teardown.txt
```
