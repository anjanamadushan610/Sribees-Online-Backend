# Coupon countdown, app-open coupon popup, expiry reminder

Owner request 2026-11-10 (reference: Shein "My Coupons" + 11.11 popup).
Owner decisions (MCQ, 2026-11-10):

| Question | Answer |
|---|---|
| Live HH:MM:SS timer starts when the coupon has less than | **72 hours** left |
| App-open popup frequency | **once a day** (again the same day only if a new coupon appears) |
| "Shop Now" on the popup | **auto-collects** the shown coupons into My Coupons |
| Expiry push reminder | **yes**, only to customers who collected it and have not used it, ~3 h before end, respects the Offers switch |

## Guardrails

- Server-driven: `/coupons/evaluate` decides the expiry display mode and words it
  (en/si/ta). Clients only draw. Thresholds change on the server, no app release.
- "Now" comes from the server `Date` header (`ServerClock` mobile, `serverNow()` web);
  display zone is Asia/Colombo. Countdown = `ends_at - serverNow()`, no zone conversion.
- Coupon validation and discount maths are **unchanged**. At zero the card moves to
  Expired client-side and the list reloads; checkout still refuses an expired code.
- Popup never shows to guests, never over onboarding / permission prompts / a push or
  deep-link open. One shared 1 s ticker per screen, stopped when off screen.
- Reminder push is a marketing push (category `offers`), deduplicated in the DB
  (`coupon_expiry_reminders`), never sent twice.
- Old app builds ignore the new fields.

## Backend (development)

1. Migration 107: `coupons.show_in_popup`, `coupons.popup_priority`, table
   `coupon_expiry_reminders(coupon_id, user_id, sent_at)`.
2. `coupon_presenter.expiry()` -> card field `expiry {ends_at, mode: timer|days|date,
   text, prefix}` + `is_new` (created < 48 h) + `expiring_soon` (< 72 h).
3. `GET /coupons/spotlight` (best 3 popup coupons, `show` decided by the daily cap),
   `POST /coupons/spotlight/seen`, `POST /coupons/collect-many`.
4. `coupon_reminder_worker` every 5 min.
5. CORS `expose_headers: ["Date"]` so browsers can read the server clock.
6. Admin API: `show_in_popup`, `popup_priority` on create/update/list.

## Clients

- Mobile: `CouponCountdown` widget (boxed digits), chips All / Expiring soon / New,
  `CouponSpotlightDialog` on home, "Shop Now" -> collect-many -> Offers (My Coupons).
- Web (ecommerce-frontend): `serverNow()` from axios responses, same countdown in
  `CouponCard`, same popup on `/shop`.
- Admin: popup switch + priority on the coupon form.

## Done (pass/fail)

- `pytest tests/ -k "coupon_expiry or spotlight or coupon_reminder"` green, plus the
  existing coupon/presenter tests and `test_every_language_has_every_line`.
- Migration 107 replays twice on a throwaway Postgres without error.
- Mobile `flutter test test/coupon_countdown_test.dart`: fake clock runs a 72 h coupon
  to 0 and the card shows Expired; `flutter analyze` clean.
- Web `npm run build` + lint green.
- Screenshot of the Offers screen with a timer card next to the reference.
