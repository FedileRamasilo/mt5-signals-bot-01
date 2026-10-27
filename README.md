# MT5-Style Signals Subscription Bot

Delivers BTC/USD, USD/CHF, Gold (XAU/USD), and USD/ZAR trade signals to a private
Telegram channel, gated by PayFast subscription payments (daily R100 /
monthly R1500, adjust in `.env`).

## IMPORTANT: run ONE service (not web + worker)

Two separate Railway services cannot share a database file, so signals logged by
a worker never reached the website, and expired people were never removed.
The signal/expiry jobs now run inside the web service (`python app.py`).
- Delete the old "worker" service in Railway.
- Add a Volume to the web service mounted at `/data` and set
  `DATABASE_PATH=/data/subscribers.db`, so redeploys don't wipe your records.
- The web service needs ALL variables (TwelveData, Telegram, Brevo, bank details...).

## Subscriber website (no WhatsApp or Telegram needed)

Send people to `/signup`. They enter their email, pay by card/instant (Paystack) or bank transfer,
and view their signals on `/signals` - the open signals with entry / stop-loss / take-profit, recently
closed ones with WIN/LOSS, and a live countdown of their paid time. The page refreshes itself.

- **Signing in:** right after a card payment they are signed in automatically (once). After that they
  enter their email on `/login` and get a one-tap link (no passwords). Links work once and expire.
- **Bank transfer:** you approve as usual; the approve page emails them a sign-in link and also shows it
  to you. `/admin/login-link?contact=their@email&secret=...` makes a fresh link if an email never arrives.
- **Protection:** signals are hidden the moment paid time ends (they see a Renew button instead);
  a login works on at most `MAX_DEVICES` devices (default 2) so one login can't be handed round a group.
- **Telegram** is just an optional button on the dashboard.
- Needs `FLASK_SECRET_KEY` (long random value) and Brevo (`BREVO_API_KEY`, `FROM_EMAIL`) for the emails.

## Paystack: automatic card / instant payments (optional)

Set `PAYSTACK_SECRET_KEY` and `/subscribe` shows a **Pay now - card / instant** button next to
bank transfer. After they pay, they land on a page with their personal join link straight away -
no approving or WhatsApp step from you. Bank transfer keeps working exactly as before.

- Every payment is double-checked with Paystack (amount, currency, paid) before access is given.
- If a customer pays and closes the page, Paystack's webhook still grants access; `/admin/members`
  then shows a **Send link** WhatsApp button for them.
- Test with your TEST key first, then swap in the LIVE key.
- Webhook URL to set in the Paystack dashboard: `https://YOUR-DOMAIN/paystack/webhook`

## How selling works now (WhatsApp, personal one-use links)

1. Advertise ONE link everywhere: `/subscribe`. The visitor types their WhatsApp number and gets
   the bank-transfer page with their own reference. They also appear on `/admin/pending`.
2. You see the payment in your bank app, open `/admin/pending`, tap **Approve**.
3. The approve page has a **Send on WhatsApp** button: WhatsApp opens with the message and the
   subscriber's personal link already written. You just tap send.
4. The personal link (`/join/<token>`) works for ONE person, once. The first person to open it
   gets a one-time Telegram join button. If it is forwarded, anyone else who opens it is sent to pay.
5. When the 12 hours end the bot removes them and the old link is dead. To renew they pay again
   and you approve again - they get a NEW link. `/admin/members` has a one-tap WhatsApp "Remind" button.

Admin pages (all need `?secret=YOUR_ADMIN_SECRET`):
- `/admin/pending` - who is waiting for you to confirm payment
- `/admin/approve?contact=27821234567&plan=daily` - approve (the Pending page links here)
- `/admin/members` - active/expired, who has joined, are the background jobs alive
- `/admin/revenue` - what you made today and over 7 days
- `/admin/test-signal?symbol=BTC` - preview a signal in your channel

Note: the 12 hours start when YOU approve, so approve and send promptly.

## New: 12-hour access periods (was 24 hours)

The "daily" plan now grants 12 hours of access instead of 24 - subscribers
pay twice a day instead of once. `DAILY_PRICE` is now the price per
12-hour period (e.g. R150 twice a day = R300/day total, same as before).
The Telegram invite link itself also now expires after 12 hours if unused,
matching the new access window.

Note: this roughly doubles how often you (or PayFast, once fully wired up)
need to process a payment per subscriber - worth knowing since it affects
your admin workload with manual EFT approvals.

## New: Revenue dashboard

See exactly how much you've made, broken down by day and plan. Visit
`/admin/revenue?secret=YOUR_ADMIN_SECRET` for today's total plus a 7-day
history. Every approved payment (EFT or PayFast) now records its amount
automatically.

## New: Public performance dashboard (real, auto-tracked win rate)

Every signal posted gets logged, and a background job checks live prices
every 15 minutes to see if TP or SL has been hit - recording each signal
as a win or loss automatically, no manual tracking needed.

Visit `/performance` for a public page showing total signals, wins,
losses, win rate, and a table of recent signals with their outcomes.

This is worth linking to from your marketing/sales messages - a real,
unfiltered track record (including losses) is the single biggest thing
that separates a trustworthy signals service from the anonymous channels
that dominate this market. Share it as: `https://your-domain/performance`

Also added: `/admin/test-signal?secret=YOUR_ADMIN_SECRET&symbol=BTC` posts
a sample signal to your private channel immediately, so you can check the
formatting without waiting for a real EMA/RSI crossover to trigger.

## New: Manual bank transfer (launch today, before PayFast is fully working)

If PayFast integration is still being debugged, you can launch immediately
using direct bank transfers instead:

1. Set your real banking details in `.env` / Railway: `BANK_NAME`,
   `BANK_ACCOUNT_HOLDER`, `BANK_ACCOUNT_NUMBER`, `BANK_BRANCH_CODE`,
   `BANK_ACCOUNT_TYPE`
2. Set `ADMIN_SECRET` to a long random string only you know
3. Send people to `/pay-eft/daily?email=them@example.com` (or `/pay-eft/monthly?email=...`)
   instead of `/pay/daily` - this shows your banking details and a unique
   reference number for them to use
4. When you see the payment land in your banking app, match the reference
   number, then visit:
   `/admin/approve?email=them@example.com&plan=daily&secret=YOUR_ADMIN_SECRET`
   This instantly grants them access and emails their Telegram invite link -
   same as the automatic PayFast flow does.

This is manual work on your end (checking your bank app, clicking one link
per subscriber) but it needs zero payment gateway debugging and works today.
Switch back to the automatic `/pay/daily` PayFast flow once that's fully
tested - the two can run side by side, so you don't have to pick just one.

## New: Referral system

Every subscriber gets a personal referral link (included in their welcome
email, and available anytime at `/my-referrals?email=them@example.com`).
When someone signs up through that link, the referrer automatically gets a
free bonus day added to their subscription — no manual work needed, it's
handled in `add_or_extend_subscription()` in `db.py`.

Referral links look like:
`https://your-domain/pay/daily?email=FRIEND_EMAIL&ref=THEIRCODE`

## New: Free daily proof-signal

Create a **second Telegram channel**, set it to **Public** (not private),
add your bot as admin, and put its ID in `TELEGRAM_FREE_CHANNEL_ID`.

Once a day, the scheduler automatically posts one real, triggered signal
(the same quality as what paid subscribers get) to this public channel with
a link to subscribe. This lets people see genuine signal quality before
paying — the single biggest trust-builder in a market full of anonymous,
unverifiable signal channels.

## How it works
1. A customer visits `yourdomain.com/pay/daily?email=them@example.com`
2. They pay via PayFast
3. PayFast calls your `/payfast/notify` webhook to confirm payment
4. The server generates a one-time Telegram invite link and (once you wire up
   an email provider) emails it to them
5. `scheduler.py` runs in the background: every 15 min it checks BTC/USD/CHF/gold
   for a trade setup and posts it to the channel; every hour it removes
   subscribers whose access has expired

## Setup

1. **Get API keys**
   - TwelveData (free tier): https://twelvedata.com/pricing
   - Telegram bot: message @BotFather, `/newbot`, save the token
   - Create a private Telegram channel, add your bot as admin
   - PayFast merchant account: https://www.payfast.co.za

2. **Install dependencies**
   ```
   pip install -r requirements.txt
   ```

3. **Configure**
   Copy `.env.example` to `.env` and fill in your real keys:
   ```
   cp .env.example .env
   ```

4. **Test the price feed and strategy locally**
   ```
   python price_feed.py
   ```

5. **Run the webhook server**
   ```
   python app.py
   ```
   Deploy this somewhere with a public HTTPS URL (Railway, Render, Fly.io,
   or a VPS behind Nginx/Caddy with Let's Encrypt) — PayFast's notify_url
   must be publicly reachable.

6. **Run the scheduler** (separate process, can run on the same server)
   ```
   python scheduler.py
   ```

## Testing with PayFast sandbox

1. In `.env`, set `PAYFAST_MODE=sandbox`
2. Use PayFast's published sandbox merchant ID and key (from their sandbox
   docs, not your real merchant account) for `PAYFAST_MERCHANT_ID` /
   `PAYFAST_MERCHANT_KEY` while testing
3. Deploy (or run locally with a tool like `ngrok` to expose your local
   server temporarily) and visit:
   `https://your-domain/pay/daily?email=youremail@example.com`
4. Complete a test payment on PayFast's sandbox checkout (it uses fake card
   details, listed on PayFast's sandbox page)
5. Check your Railway logs (or terminal) for `Subscriber ... paid for daily.
   Invite emailed.` — and check your inbox for the email
6. Confirm the invite link actually adds you to the Telegram channel
7. Once this full loop works, switch `PAYFAST_MODE=live` and swap in your
   real merchant ID/key

## What you still need to do before taking real money

- **Email delivery**: uses Brevo's HTTPS API in `email_client.py`, not SMTP.
  Railway (and most hosts on free/hobby plans) block outbound SMTP entirely
  to prevent spam abuse - no SMTP config would ever work there, regardless
  of credentials. Sign up free at brevo.com (300 emails/day free), verify
  your account, generate an API key under SMTP & API > API Keys, and set
  BREVO_API_KEY and FROM_EMAIL in your `.env` / Railway variables.
- **PayFast ITN validation**: now implemented in `/payfast/notify` - it
  verifies the signature (catches tampering), checks the paid amount matches
  the plan price (catches manipulated amounts), and confirms with PayFast's
  own validate endpoint that the notification is genuine, before granting
  any access. This means you no longer need `/admin/approve` for PayFast
  payments specifically - they auto-approve safely. (Source IP checking is
  the one extra layer PayFast's docs also suggest, not added here since
  Railway's outbound IPs aren't fixed - the three checks above cover the
  practical risk.)
- **Strategy validation**: the EMA/RSI crossover in `signal_engine.py` is a
  starting template, not a backtested edge — paper trade it for a couple of
  weeks against your own judgment before charging people for it.
- **Compliance**: charging for trading signals in South Africa may fall
  under FSCA/FAIS regulation — get a compliance opinion before scaling past
  a handful of users.
- **Refund/cancellation flow**: daily subscribers who don't renew should
  just lapse naturally (handled), but you'll want a clear cancellation path
  for monthly subscribers.

## File overview
- `price_feed.py` — pulls live candles from TwelveData
- `signal_engine.py` — EMA/RSI strategy, generates BUY/SELL/SL/TP
- `telegram_client.py` — posts signals, manages channel invites/removals
- `email_client.py` — emails the invite link to a subscriber after payment
- `db.py` — SQLite subscriber tracking
- `app.py` — Flask server: payment links + PayFast webhook
- `scheduler.py` — background job: checks signals + prunes expired access
