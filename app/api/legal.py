"""
SRIBEESonline - Legal Pages

Mounted at the site root (not under /api), same reasoning as share_links.py:
this needs one exact, stable URL to paste into the Play Console / App Store
listing, not something namespaced under /api/v1 that could move.

  GET /privacy          — Privacy Policy (public, no auth).
  GET /account-deletion — what closing an account removes and what is kept.
  GET /support          — the App Store support URL: how to reach us.

Static HTML, not app_content-driven: a store-listing legal URL must not 404
or go blank because of an app_content row being deactivated or unseeded, and
it changes rarely enough that a code deploy is the right cost for editing it.
"""
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["Legal"])

_PRIVACY_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Privacy Policy - Sribees Online</title>
<style>
  body { font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif; max-width: 760px; margin: 0 auto; padding: 32px 20px 64px; color: #1a1a2e; line-height: 1.6; }
  h1 { font-size: 26px; margin-bottom: 4px; }
  .meta { color: #6b6b80; font-size: 14px; margin-bottom: 32px; }
  h2 { font-size: 18px; margin-top: 32px; border-bottom: 1px solid #eee; padding-bottom: 6px; }
  ul { padding-left: 20px; }
  li { margin-bottom: 6px; }
  a { color: #c2185b; }
</style>
</head>
<body>
<h1>Privacy Policy</h1>
<p class="meta">Sribees Online &middot; Effective 19 November 2026</p>

<p>Sribees Digitel Solutions (the software arm of Sribees Lanka (Pvt) Ltd,
"Sribees", "we", "us") operates the Sribees Online app and website. This
policy explains what information we collect from customers, why, and who we
share it with.</p>

<h2>Information we collect</h2>
<ul>
<li><strong>Account details:</strong> full name, email address and phone
number. When you sign in by phone, we verify it using an SMS code.
A National Identity Card (NIC) number is not required to register or shop in
the app. Some existing accounts may have an NIC number provided under the
previous registration process.</li>
<li><strong>Delivery address:</strong> the address(es) you save for
delivery.</li>
<li><strong>Orders &amp; purchases:</strong> your order history, items
purchased, delivery status, and returns/refunds.</li>
<li><strong>Payment information:</strong> when you pay by card or bank,
payment is completed on our payment processor's own secure hosted page
(Marx IPG); Sribees does not receive or store your full card number. We do
store the outcome of the payment (success/failure, amount, date) against
your order.</li>
<li><strong>Photos:</strong> a photo you choose to attach, for example when
requesting a return or when messaging our support chat. You choose the
photo yourself via your phone's photo picker or camera each time; we do not
access your photo library beyond the single photo you pick.</li>
<li><strong>AI shopping assistant:</strong> if you use the in-app
assistant, the messages you type and any photo you choose to send it (for
example, a photo of an empty spice jar) are used to answer you. We keep what
you write, the assistant's replies, the products it showed you and whether you
then added or ordered them, to answer you and to improve the shop. This is kept
until you close your account; clearing the chat in the app removes it from your
screen only. Photos sent to the assistant are not stored by us.
Preferences you explicitly ask it to remember (favourite brands, ingredients
to avoid, household size, budget) and reminders you set are kept until you
ask it to forget them or close your account.</li>
<li><strong>Support messages:</strong> messages you send in the in-app
support/help chat, including any support ticket you raise and the contact
number you give on it.</li>
<li><strong>Sign-in via Google or Apple:</strong> if you choose to sign in
this way, we receive the name and email address your Google or Apple
account shares with us.</li>
<li><strong>Push notification token &amp; device information:</strong> a
device token (via Firebase Cloud Messaging) so we can send you order
updates and offers, and basic device/app information (device model, OS
version, app version) used for crash and error reporting (via Sentry) and
to make sure the app is working correctly.</li>
<li><strong>How you use the app and website:</strong> the screens and pages
you open and how long you stay, how far you scroll, the products and product
photos you look at, what you search for, changes to your cart and saved items,
offers and coupons you tap, and the steps of checkout. This is recorded from
the moment you open the app or website, including before you sign in, against
a random identifier for that install or browser; once you sign in on it, it is
linked to your account. We use it to understand what shoppers look for and to
improve the shop. It is kept while your account is open and deleted when you
close your account. It never includes passwords, sign-in codes, card details or
the messages you type.</li>
<li><strong>Referral &amp; install information:</strong> a referral code you
enter or share, and, on Android, install-referrer information used to
credit the correct referral.</li>
</ul>

<h2>How we use this information</h2>
<ul>
<li>To create and secure your account, and to prevent fraud and duplicate
accounts.</li>
<li>To process and deliver your orders, and to handle returns and refunds.</li>
<li>To send order updates, delivery updates, and (only if you have not
opted out) offers and promotions, by push notification and/or SMS.</li>
<li>To respond to your support messages and tickets.</li>
<li>To diagnose crashes and errors and improve the app.</li>
<li>To understand which products and screens shoppers use, so we can improve
the range, search and layout of the shop.</li>
<li>To credit referral rewards and loyalty points correctly.</li>
</ul>

<h2>Who we share information with</h2>
<p>We do not sell your personal information. We share it only with the
service providers that help us run the app, each acting under our
instructions:</p>
<ul>
<li><strong>Marx IPG</strong> &mdash; our payment gateway, to process card
and bank payments.</li>
<li><strong>Google (Firebase)</strong> &mdash; to deliver push notifications
and, if you choose, Google Sign-In.</li>
<li><strong>Google (Gemini API)</strong> &mdash; to understand and answer
messages and photos you send to the AI shopping assistant. We use Google's
paid API service, under which Google does not use this content to train or
improve its models.</li>
<li><strong>Apple</strong> &mdash; only if you choose Sign in with Apple.</li>
<li><strong>Sentry</strong> &mdash; crash and error reporting, configured to
exclude personal data by default.</li>
<li>Our SMS gateway provider &mdash; to deliver OTP and order-update text
messages.</li>
<li>Delivery riders/branches, limited to what is needed to deliver your
order (name, phone, address).</li>
</ul>
<p>We do not use your information for advertising, we do not track you
across other companies' apps or websites, and the app contains no
advertising SDKs.</p>
<p>We may also disclose information if required by Sri Lankan law or to
protect the rights, property, or safety of Sribees, our customers, or the
public.</p>

<h2>Data retention</h2>
<p>We keep account and order data for as long as your account is active.
If you delete your account, we remove your profile and personal data
straight away and keep only the records we are required to keep &mdash;
your previously supplied NIC (if any), past orders (with the delivery address and contact number used
for them) and payment and wallet records &mdash; for five years after the
account is closed, to meet tax, accounting and delivery-dispute obligations.
They are not used for marketing or any other new purpose. See
<a href="/account-deletion">Account &amp; Data Deletion</a> for the full
list.</p>

<h2>Your choices</h2>
<ul>
<li>You can review and update your profile, addresses, and notification
preferences at any time in the app under Profile &gt; Settings.</li>
<li>You can turn off promotional/marketing notifications while still
receiving order and delivery updates.</li>
<li>You can delete your account at any time, in-app (Profile &gt; Settings
&gt; Delete Account) or by email &mdash;
see <a href="/account-deletion">Account &amp; Data Deletion</a> for what is
removed and what we are required to keep.</li>
</ul>

<h2>Children's privacy</h2>
<p>Sribees Online is not directed at,
or intended for use by, children.</p>

<h2>Security</h2>
<p>We use industry-standard measures (encryption in transit, access
controls) to protect your information, but no method of transmission or
storage is 100% secure.</p>

<h2>Changes to this policy</h2>
<p>We may update this policy from time to time. Material changes will be
reflected by a new effective date at the top of this page.</p>

<h2>Contact us</h2>
<p>Questions about this policy or your data can be sent to
<a href="mailto:support@sribeesonline.com">support@sribeesonline.com</a>, or raised as
a support ticket in the app.</p>

</body>
</html>
"""


@router.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
async def privacy_policy() -> HTMLResponse:
    return HTMLResponse(content=_PRIVACY_HTML)


_ACCOUNT_DELETION_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Account &amp; Data Deletion - Sribees Online</title>
<style>
  body { font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif; max-width: 760px; margin: 0 auto; padding: 32px 20px 64px; color: #1a1a2e; line-height: 1.6; }
  h1 { font-size: 26px; margin-bottom: 4px; }
  .meta { color: #6b6b80; font-size: 14px; margin-bottom: 32px; }
  h2 { font-size: 18px; margin-top: 32px; border-bottom: 1px solid #eee; padding-bottom: 6px; }
  ol, ul { padding-left: 20px; }
  li { margin-bottom: 6px; }
  a { color: #c2185b; }
  .box { background: #f7f3f5; border-radius: 10px; padding: 16px 18px; margin: 16px 0; }
</style>
</head>
<body>
<h1>Account &amp; Data Deletion</h1>
<p class="meta">Sribees Online</p>

<p>You can ask us to delete your Sribees Online account and personal data at
any time, in either of the two ways below.</p>

<h2>Option 1 - In the app (fastest)</h2>
<ol>
<li>Open the Sribees Online app and sign in.</li>
<li>Go to <strong>Profile &gt; Settings &gt; Delete Account</strong>.</li>
<li>We send a one-time code by SMS to the phone number on your account, to
confirm it's really you before anything is removed.</li>
<li>Enter the code to confirm. Your account is closed immediately.</li>
</ol>

<h2>Option 2 - By email (no app or sign-in needed)</h2>
<p>Email <a href="mailto:support@sribeesonline.com">support@sribeesonline.com</a> from
any address, with:</p>
<ul>
<li>the phone number registered on the account, and</li>
<li>the subject line "Delete my Sribees account".</li>
</ul>
<p>We verify the request against the account and process it, generally
within 7 days and no later than 30 days. We may contact you at the number
on file to confirm the request before deleting anything.</p>

<div class="box">
<h2 style="margin-top:0; border-bottom:none;">What is removed</h2>
<p>Straight away: your name, phone numbers and email, saved addresses,
wishlist, notifications inbox, cart, support tickets, the AI assistant's
chat history, saved preferences and reminders, the record of how you used
the app and website, and your push notification token. You are signed out
everywhere. If you signed in with Google or Apple,
that link is removed, and Sign in with Apple is also revoked with Apple.
Your phone number, email and Google or Apple account are released and may
be used to open a new account.</p>

<h2>What we are required to keep, for five years</h2>
<p>Your previously supplied NIC (if any), your past orders (including the delivery address and contact
number each was sent to), and payment and wallet records stay in our
records for five years after the account is closed. Sri Lankan tax,
accounting, and delivery-dispute rules require us to keep them. They are no
longer linked to an account you (or anyone) can sign in to, and are not
used for marketing or any other new purpose.</p>
</div>

<p>See the full <a href="/privacy">Privacy Policy</a> for more detail on
what we collect and why. Questions: <a href="mailto:support@sribeesonline.com">support@sribeesonline.com</a>.</p>

</body>
</html>
"""


@router.get("/account-deletion", response_class=HTMLResponse, include_in_schema=False)
async def account_deletion() -> HTMLResponse:
    return HTMLResponse(content=_ACCOUNT_DELETION_HTML)


_SUPPORT_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Support - Sribees Online</title>
<style>
  body { font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif; max-width: 760px; margin: 0 auto; padding: 32px 20px 64px; color: #1a1a2e; line-height: 1.6; }
  h1 { font-size: 26px; margin-bottom: 4px; }
  .meta { color: #6b6b80; font-size: 14px; margin-bottom: 32px; }
  h2 { font-size: 18px; margin-top: 32px; border-bottom: 1px solid #eee; padding-bottom: 6px; }
  ul { padding-left: 20px; }
  li { margin-bottom: 6px; }
  a { color: #c2185b; }
</style>
</head>
<body>
<h1>Support</h1>
<p class="meta">Sribees Online</p>

<p>Need help with an order, a delivery, a return or your account? We're
here to help.</p>

<h2>Contact us</h2>
<ul>
<li><strong>Email:</strong> <a href="mailto:support@sribeesonline.com">support@sribeesonline.com</a></li>
<li><strong>In the app:</strong> open <strong>Help &amp; Support</strong> and raise a
support ticket. Please include your order number if it is about an
order.</li>
</ul>

<h2>Your account and data</h2>
<ul>
<li><a href="/account-deletion">Delete your account</a> &mdash; in the app
or by email.</li>
<li><a href="/privacy">Privacy Policy</a> &mdash; what we collect and
why.</li>
</ul>
</body>
</html>
"""


@router.get("/support", response_class=HTMLResponse, include_in_schema=False)
async def support() -> HTMLResponse:
    return HTMLResponse(content=_SUPPORT_HTML)
