# MT5-Style Signals Subscription Bot

Delivers BTC/USD, USD/CHF, and Gold (XAU/USD) trade signals to a private
Telegram channel, gated by PayFast subscription payments (daily R100 /
monthly R1500, adjust in `.env`).

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
free bonus day added to their subscription â€” no manual work needed, it's
handled in `add_or_extend_subscription()` in `db.py`.

Referral links look like:
`https://your-domain/pay/daily?email=FRIEND_EMAIL&ref=THEIRCODE`

## New: Free daily proof-signal

Create a **second Telegram channel**, set it to **Public** (not private),
add your bot as admin, and put its ID in `TELEGRAM_FREE_CHANNEL_ID`.

Once a day, the scheduler automatically posts one real, triggered signal
(the same quality as what paid subscribers get) to this public channel with
a link to subscribe. This lets people see genuine signal quality before
paying â€” the single biggest trust-builder in a market full of anonymous,
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
   or a VPS behind Nginx/Caddy with Let's Encrypt) â€” PayFast's notify_url
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
   Invite emailed.` â€” and check your inbox for the email
6. Confirm the invite link actually adds you to the Telegram channel
7. Once this full loop works, switch `PAYFAST_MODE=live` and swap in your
   real merchant ID/key

## What you still need to do before taking real money

- **Email delivery**: now wired up via SMTP in `email_client.py` â€” if using
  Gmail, you'll need to generate an "App Password" (Google Account â†’
  Security â†’ 2-Step Verification â†’ App Passwords), not your normal password.
- **PayFast ITN validation**: PayFast's docs require you to also verify the
  notification against their validate endpoint and check the source IP, not
  just trust the POST body. See PayFast's ITN documentation before going live.
- **Strategy validation**: the EMA/RSI crossover in `signal_engine.py` is a
  starting template, not a backtested edge â€” paper trade it for a couple of
  weeks against your own judgment before charging people for it.
- **Compliance**: charging for trading signals in South Africa may fall
  under FSCA/FAIS regulation â€” get a compliance opinion before scaling past
  a handful of users.
- **Refund/cancellation flow**: daily subscribers who don't renew should
  just lapse naturally (handled), but you'll want a clear cancellation path
  for monthly subscribers.

## File overview
- `price_feed.py` â€” pulls live candles from TwelveData
- `signal_engine.py` â€” EMA/RSI strategy, generates BUY/SELL/SL/TP
- `telegram_client.py` â€” posts signals, manages channel invites/removals
- `email_client.py` â€” emails the invite link to a subscriber after payment
- `db.py` â€” SQLite subscriber tracking
- `app.py` â€” Flask server: payment links + PayFast webhook
- `scheduler.py` â€” background job: checks signals + prunes expired access
