# Paystack add-on — MT5 Signal Bot 01

This package is an **integration starter**, not a fully activated payment system.
It deliberately does not replace your existing `app.py`, signal engine, scheduler,
or Telegram access logic. Review and connect the TODOs before enabling live checkout.

## Included

- `paystack_addon/paystack_routes.py`: Flask blueprint for checkout initialization,
  server-side transaction verification, and signed webhook validation.
- `.env.paystack.example`: environment variable template.
- `requirements-paystack.txt`: dependencies for the add-on.
- This README: installation and safe deployment checklist.

## Before you start

1. Create a Git branch, e.g. `feature/paystack-integration`.
2. Back up your existing repository and Railway variables.
3. Confirm the exact SQLite database path used by your existing `db.py`.
4. Read your existing subscription functions in `db.py` and Telegram invitation flow
   in `telegram_client.py`. Do not replace these with new implementations.
5. Confirm Paystack permits your specific paid trading-signal service before accepting
   payments. Describe the service accurately during onboarding.

## Add the files

Copy `paystack_addon/paystack_routes.py` into your repository, preserving the folder.
Add the values from `.env.paystack.example` to Railway Variables; do not commit real keys.

Install `requests` if it is not already in your `requirements.txt`. The starter uses
Flask and requests, which your existing Flask app is expected to have.

## Connect the blueprint

In `app.py`, after creating the existing Flask app object, add:

```python
from paystack_addon.paystack_routes import paystack_bp
app.register_blueprint(paystack_bp)
```

If your Flask app object is named something other than `app`, adapt the registration.
Make sure your deployment start command imports that same app object.

## Required integration work before enabling

The two `TODO` blocks in the starter must be completed. Build one shared function in your
existing codebase that:

1. Loads the payment row and confirms the Paystack verification result, exact amount,
   currency, plan and reference.
2. Atomically claims the payment for fulfillment to prevent duplicate access grants.
3. Calls the existing database subscription function and Telegram invite workflow.
4. Stores the Telegram/subscription outcome and supports retry/reconciliation if the
   process stops after payment verification.
5. Uses the same fulfillment function from both `/paystack/webhook` and `/paystack/return`.

Do not set `PAYSTACK_ENABLED=true` until these TODOs are complete and tested. In this
starter, the return route only verifies payment; the webhook does not activate access.

## Railway variables

```dotenv
PAYSTACK_SECRET_KEY=sk_test_your_test_secret_key
PAYSTACK_CURRENCY=ZAR
PAYSTACK_ENABLED=false
PAYSTACK_CALLBACK_URL=https://YOUR-RAILWAY-DOMAIN/paystack/return
DATABASE_PATH=YOUR_EXISTING_SQLITE_DATABASE_PATH
```

`PAYSTACK_CALLBACK_URL` must be your actual public HTTPS Railway domain. `DATABASE_PATH`
must point to the same persistent database used by your existing bot. Do not blindly set
it to `subscriptions.db` if your existing app stores the database elsewhere.

Use a test secret key for initial testing. Keep secret keys only in Railway Variables,
never in GitHub files or logs.

## Checkout API

The starter accepts a POST to `/paystack/start` with form fields or JSON:

```json
{
  "email": "customer@example.com",
  "whatsapp": "+27XXXXXXXXX",
  "plan": "daily"
}
```

Valid plan IDs and current server-controlled example prices:

- `daily`: R100, 12 hours
- `monthly`: R1,500, 30 days
- `premium_monthly`: R3,000, 30 days

The endpoint returns `authorization_url`. Redirect the customer's browser to that URL.

These prices are configured in code and must be reviewed before launch. The two monthly
plans are one-time 30-day purchases in this starter, not automatic recurring billing.

## Webhook configuration

In the Paystack dashboard, configure the webhook endpoint:

`https://YOUR-RAILWAY-DOMAIN/paystack/webhook`

The starter validates `x-paystack-signature` using HMAC-SHA512 over the raw request body
and the secret key. It also verifies the transaction through Paystack's API. Never grant
access based only on a browser redirect or webhook payload.

## Test checklist

- Checkout rejects unknown plans and malformed email/WhatsApp.
- Browser-supplied amount cannot change the server price.
- Invalid webhook signatures are rejected.
- Successful transactions must match reference, amount, and currency.
- Failed, pending, wrong-currency and wrong-amount transactions do not grant access.
- Replayed webhook events do not create duplicate subscriptions.
- A failed Telegram invitation can be retried without charging the customer again.
- Existing signal generation, scheduled jobs, manual EFT approvals and PayFast routes
  continue to work.
- Test on a branch and use Paystack test mode before live mode.

## Official docs

- https://paystack.com/docs/api/transaction/
- https://paystack.com/docs/payments/webhooks/
