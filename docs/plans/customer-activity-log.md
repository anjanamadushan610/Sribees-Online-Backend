# PLAN: customer activity log (app + web)

Owner decisions: 2026-11-19, two Sinhala MCQ rounds. Trigger: a new customer (SB-23980) spent 21 minutes as a
guest before signing up, and nothing recorded what they looked at.

## Task

Record what each shopper does in the customer app and on www.sribeesonline.com, from the first app open (guest or
signed in). Super Admin sees one timeline per customer and per guest install.

## Rules (owner)

| # | Rule |
|---|------|
| R1 | **What (full summary):** app open / background, every screen with time spent, product opened, product image swipe and zoom, search text, category / sub-category opened, add to cart / remove / quantity change, wishlist, banner and offer taps, checkout steps, scroll depth per screen (deepest % and items seen, sent once when the screen closes, not every scroll movement). |
| R2 | **Where:** Android/iOS app and the web store. |
| R3 | **Guest link:** events carry the install id (`guest_…` / `web_…`). When that install signs in, its earlier guest events show on the customer's timeline. |
| R4 | **Kept forever.** No age-based delete. Removed only when the customer deletes the account (Play/App Store rule). One plain table with time indexes (BRIN on `received_at`); partition later if it grows past tens of millions of rows. |
| R5 | **Who sees it:** Super Admin only. Admin customer drawer gets an "Activity" tab; App Users install row gets the same timeline for guests. |
| R6 | **Law:** privacy policy (en/si/ta) and Play Data safety updated before the app version that sends events goes to production. |
| R7 | **No opt-out switch** (owner's choice). |
| R8 | **Release:** development first; main, prod and Play only after asking the owner. |

## Design

- **Backend** migration 121 `activity_events`: `id`, `device_id`, `user_id`
  NULL for guests, `platform`, `app_version`, `session_id`, `event`, `screen`, `product_id`, `category_id`,
  `props jsonb` (small, schema-checked per event), `occurred_at` (client time, clamped), `received_at`.
  `POST /app/activity/batch` open to guests like the heartbeat: max 100 events, props max 12 flat keys (text cut at 200), 120 batches/min per address
  (many phones share a carrier address); `user_id` only from the token, never the body; unknown event names rejected.
  `GET /admin/activity?user_id=|device_id=` (Super Admin) returns one merged timeline: the user's own events +
  events from every install that user ever signed in on (R3), Colombo time.
- **Mobile** `lib/core/services/activity_log.dart`: in-memory queue, flushed every 30 s, on background and at 50
  events; saved to SharedPreferences when offline (cap 2,000, oldest dropped). Screens are found automatically by
  `ActivityRouteObserver` (route widget `ProductDetailsScreen` -> `product_details`), shell tabs renamed by
  `HomeScreen`; one `screen` event when a screen goes off show, with seconds, `scroll_pct`, `scroll_px` (outermost
  vertical list, from `ActivityScrollListener` around the navigator). Guest events are flushed before a sign-in
  stores the new token, so they stay marked guest.
- **Web** (`ecommerce-frontend`) `src/lib/activity.ts` + `components/WebActivity`: same endpoint, platform `web`,
  browser id = the presence id, fetch keepalive, pages named in `utils/activityPages.ts`; cart/wishlist/coupon/order
  services and the auth store log their own events.
- **Admin** "Activity" tab in `CustomerProfileDrawer`, and in the App Users install detail: day groups, icons per
  event, product names linked.
- **Not logged:** passwords, OTPs, card data, addresses typed, chat text (already in `assistant_chat_log`).

## Guardrails

- Logging must never slow or break the app: send failures are silent, the queue is bounded.
- No customer data accepted from the body beyond the event fields above.
- Every migration safe to replay (`IF NOT EXISTS`), no data writes.
- Server cost: estimate rows/day on dev before prod; owner told the disk use after one week live.

## Done (each must pass)

1. Backend tests: batch accepts guest + signed-in, rejects unknown event / >100 events / foreign user id in body;
   timeline merges guest events after sign-in. Full local suite green except the known 5 referral tests.
2. Migration 121 replayed twice on a throwaway Postgres 18 without error.
3. Mobile `flutter analyze` clean + unit tests for queue/flush/offline cap; on the dev API, a fresh install →
   browse as guest → open a product → swipe images → search → sign in: admin timeline shows all of it in order
   under that customer.
4. Web: same walk on the dev web build shows on the timeline with platform "web".
5. Admin: Activity tab renders for a customer with events and for one with none (screenshot).
6. Privacy policy en/si/ta live text names the activity log; Data safety answers written for the owner.
7. Owner says yes before main / prod / Play.

## Status (2026-11-19)

- Built on `feat/activity-log` in backend, mobile, admin (310) and web; done-checks 1, 2, 5 (unit level) and the
  policy text pass locally. Next: development deploy, then the on-device walk (check 3) and the web walk (check 4).
- Event names: `app/services/activity_service.py` `EVENTS` is the list; a client sending another name gets 422.
