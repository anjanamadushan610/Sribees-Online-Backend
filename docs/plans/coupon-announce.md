# Coupon announcement push (owner, 2026-11-13)

## Why
The admin coupon form showed an "Automated Dispatch & Notification Panel"
(SMS / Email / Push + template) that the server never implemented: whatever was
set there was dropped on save and nobody was told about the coupon. The owner
asked for it to actually work.

## Owner's decisions (Sinhala MCQ, 2026-11-13)
| Question | Answer |
|---|---|
| What to do with the fake panel | Make it work |
| Channel | App push only (no SMS / email) |
| When | When the coupon starts (valid_from; at once if already started) |
| Who, "All customers" | Same as Push Campaigns: network coupon -> everyone; branch coupon -> customers that branch has served; members-only -> that level and up |
| Words | Generated from the coupon, en / si / ta, no admin typing |
| Customers added later to a Specific list | Only the new ones get it |
| Tap | Opens the Offers screen (existing `screen: coupons` route; no app update) |
| Hidden-code (non-public) "All customers" coupons | Never announced: only Public coupons and Specific Customers coupons |
| Switch default | OFF |

## Guardrails
- One push per customer per coupon, ever: `coupon_announcements` PK, row written
  before the push (same pattern as `coupon_expiry_reminders`).
- Offers switch respected (category `offers`); blocked / closed / inactive
  accounts never.
- Not sent to someone who cannot use it: per-user limit already reached, or a
  first-order-only coupon for someone who has ordered.
- "All" / members-only coupons are announced once (`coupons.announced_at`); a
  Specific coupon keeps sweeping while live so later additions (and people who
  sign up later with a listed number) get theirs.
- Inactive, expired, not-started or budget-depleted coupons send nothing.
- Saving `notify_customers=true` on a hidden "All" / members-only coupon is
  refused with a reason.

## Done = these pass
- `tests/test_coupon_announce.py` + `tests/integration/test_coupon_announce.py`:
  audience per type, once only, later additions only, Offers switch, not-started
  / expired / inactive, per-user limit, first-order, hidden-code refusal.
- Migration 116 replayed twice on a prod-shaped DB.
- i18n template test (all three languages, same placeholders).
- Full backend suite and admin vitest + build green.
