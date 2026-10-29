# New-customer Partner referral promotions

This branch is stacked on `codex/linked-partner-wallet`. No production offer is activated.

## Approved behavior

Only newly registered customers using a Partner/Master Partner code qualify. Super Admin alone configures campaigns. An existing customer's referral relationship, past wallet balance or order history never backfills a benefit.

Campaigns may target all Partner codes, one Partner/Master code, or a Master and their direct Partners. The first eligible campaign wins: direct code, then Master team, then common campaign; offers do not stack. A customer's creation time must be at or after both campaign creation and enrollment start, and they must have no previous orders, including cancelled orders. A customer can receive only one acquisition grant.

Configuration includes enrollment start/end, maximum new customers, benefit validity in days (1–365), minimum basket, free-delivery order count (or no count limit during validity), maximum eligible courier fee, total delivery budget, and an optional fixed/percentage personal coupon with redemption limit and maximum per-order saving. “One month” is configured as 30 days, not a calendar-month boundary. Validity starts at account registration, not each later order.

Granted customer terms are snapshots. Editing rules changes future grants; it does not rewrite existing grants. Pausing stops enrollment. Existing delivery grants continue subject to expiry, uses and the campaign's remaining delivery budget. Budget cannot be reduced below already reserved/spent delivery subsidy; maximum enrollment cannot be reduced below already enrolled customers.

Personal coupons reuse the existing authoritative coupon validator, budget and redemption machinery, are private and user-targeted, and appear automatically in My Coupons. They cannot be shared with another customer. Maximum coupon exposure is bounded by maximum customers × coupon uses × coupon cap; this is separate from the delivery budget.

## Checkout and accounting

Always obtain/confirm a genuine courier quote. Preserve the business courier charge in `orders.courier_quote_amount`; waive only customer shipping and record `orders.referral_delivery_discount`. Recompute total and wallet usage with zero customer shipping. The same server catalogue prices govern cart/quote/create eligibility; raw shared courier caches never store a customer's waived price.

Checkout reservations lock the customer, then campaign and grant. The final use and budget are reserved with the order in one transaction. Stable order IDs make reservations idempotent. Concurrent checkouts cannot spend the same last use. If a new client expected free delivery and eligibility has expired/exhausted, refuse and request a fresh preview rather than silently charge shipping.

Only an unshipped cancelled/payment-failed order restores the delivery use and budget, once. Delivered/refunded or already shipped parcels do not restore the subsidy. Awaiting-payment orders reserve uses until the existing cancellation lifecycle resolves them. The promotion does not introduce a separate payment timeout.

Finance contract adds `order.status.amounts.referral_delivery_subsidy`. Finance must distinguish customer shipping from courier cost and merchant acquisition expense, including cancellations, without double-counting coupon discounts already represented in existing order events.

## Rollout

1. Review and merge prerequisite linked-wallet changes and this coordinated backend/Admin/mobile feature.
2. Apply additive migration 096; rehearse every migration twice using CI.
3. Keep `REFERRAL_PROMOTIONS_ENABLED=false` by default. Prepare paused campaigns.
4. When Finance outbox is enabled, verify the consumer accounting contract before `REFERRAL_PROMOTIONS_FINANCE_READY=true`. Execution stays disabled otherwise. Never infer readiness from merely having created a repository.
5. Controlled test: new account/code, private coupon isolation, cart/checkout agreement, simultaneous final-use orders, cancellation release and budget exhaustion. No real bank transfer or live order needed.
6. Enable the reviewed rollout, then activate campaigns. Activation cannot be backdated and active campaigns cannot be saved while the execution gate is off.

API: Super Admin GET/POST `/admin/referral-promotions`, PUT `/admin/referral-promotions/{id}`. Customer GET `/referrals/benefits`; existing POST `/referrals/redeem` enrolls atomically.
