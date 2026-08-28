# SRIBEESonline → SribeesExpress: open items after integrating

**From:** SRIBEESonline (grocery e-commerce platform)
**To:** the team/agent building the SribeesExpress delivery system
**Against:** `Ecommerce Integration API — Integration Contract`, revision of
2026-08-28 (`/api/v1/ecommerce/*`)
**Date:** 2026-08-28
**Supersedes:** `SRIBEESEXPRESS_INTEGRATION_GAPS.md` (historical — do not work
from it)

---

## 0. Status

**We have integrated.** The client is written, merged and tested against the
full contract: location resolution with cached `city_id`, rates, booking,
tracking, the §7a reconciliation sweep, `PATCH` corrections, cancellation,
pickup-location registration, and signed inbound webhooks with replay
protection and `event_id` dedupe. Verified end to end against a local double
built to your contract, including every error branch.

We are **not blocked on code**. We are blocked on **sandbox keys** and on
**§1 below**.

Two things from the last round are closed and need no further work from you:

- **Handover code on COD — withdrawn.** You declined it; we accept that and
  are not pursuing it. We send `handover_code` on prepaid shipments only.
- **Webhook retry (§6.1) — accepted as won't-build.** See §5 for the one
  consequence we need you to acknowledge.

| # | Item | Type | Blocking? |
|---|---|---|---|
| 1 | COD remittance / settlement | Not built | **Yes — blocks COD launch** |
| 2 | `updated_at` is missing from the shipment response | **Contract bug** | **Yes — breaks the §7a sweep** |
| 3 | Failure reason is free text only | Partially built | No, but costly |
| 4 | Same-day / express service tier | Not built | No — revenue we can't sell |
| 5 | §7a is now load-bearing | Acknowledgement | No |
| 6 | Cold chain | Answer needed | No |
| 7 | Sandbox & keys | Operational | **Yes — nothing to test against** |
| 8 | Field-level questions from building the client | Clarifications | No |

---

## 1. BLOCKER — COD remittance / settlement

Your response: *"Won't build yet — need your payout policy first."*

**Respectfully, we think that is the wrong way round.** SribeesExpress holds
our customers' cash. The cadence, the deductions and what "paid" means
operationally are your commercial terms as the carrier — we cannot author a
policy for money we never touch. A merchant does not tell a courier when the
courier will pay them.

What we can tell you is exactly what we need to **read**, and none of it
depends on the policy being finalised:

1. **Which shipments a payout covers.** Without this we cannot tie a bank
   credit to any order, and our accounts will not close.
2. **What was deducted**, and on what basis.
3. **When it landed**, with a bank reference we can match.

Two endpoints, from the previous round, unchanged:

```http
GET /ecommerce/remittances?from=&to=&limit=&offset=
GET /ecommerce/remittances/{remittance_id}/shipments
```

Plus a `remittance_id` on the shipment (null until settled), so we can answer
"has this order's cash reached us?" without a date-range scan.

**If the full thing is too far out, the minimum viable version is smaller than
you might think.** A single read-only endpoint answering *"which of my
delivered COD shipments have not yet been remitted, and what is the
outstanding total?"* would unblock us. That needs no payout policy at all — it
is arithmetic over data you already hold:

```http
GET /ecommerce/cod-balance
```
```json
{
  "currency": "LKR",
  "collected_not_remitted": 184300.00,
  "shipment_count": 41,
  "oldest_collection_at": "2026-08-21T11:04:00+00:00"
}
```

We would take that today and reconcile the bank side manually until the full
remittance view exists.

**Please also just tell us in prose**, even informally: how often do you pay
merchants, what comes off the top, and what happens to COD cash on a
`returned` shipment? We need those three answers to model our cash flow before
launch, and they are not a build task.

---

## 2. BLOCKER (contract bug) — `updated_at` is filterable but never returned

This one came straight out of implementing your §7a sweep, and we think it is
an oversight rather than a decision.

**§7a accepts `updated_since`.** The response is documented as *"the same
per-shipment shape as §7's response (`ShipmentOut`)"* — and `ShipmentOut`
contains `created_at` but **no `updated_at`**. §8's read does not return one
either. Grep the contract: `updated_at` appears nowhere outside the query
parameter's own name.

**Why that breaks the sweep.** A reconciliation poll advances a cursor to the
newest `updated_at` it has seen, then passes that back as `updated_since` next
run. With no `updated_at` in the payload there is nothing to advance the
cursor *to*. The only fallbacks are:

- `created_at` — wrong; a shipment booked Monday and delivered Friday would
  drag the cursor back to Monday on every sweep, so it never advances at all
  and the query window grows without bound.
- our own wall clock at the *end* of the run — which silently skips any
  shipment updated between your query executing and our cursor write. For a
  COD order that is the `delivered` event, and with no webhook retry (§10.4)
  there is no second chance to learn it happened.

What we actually do is take **our own clock at the moment the sweep starts**,
before the first request, and rewind it a further minute on the next read.
Start-of-run is what makes it safe — anything you update mid-sweep is stamped
at or after that instant, so the following run still catches it — and the
rewind absorbs clock skew between your host and ours.

Both are mitigations, not a fix. They cost us a re-read of one minute's worth
of shipments on every sweep, and they trade correctness for a dependency on
two clocks agreeing. With `updated_at` in the payload, none of that is
necessary.

**The fix is one field.** Add `updated_at` to `ShipmentOut`, so it appears on
§7, §7a and §8 alike:

```json
{
  "shipment_id": "SXP0000012345",
  "created_at": "2026-08-25T12:35:00+00:00",
  "updated_at": "2026-08-28T15:02:00+00:00"
}
```

You are already maintaining this value — `updated_since` could not filter on
it otherwise. It just is not serialized.

**Two follow-ups while you are in there:**

- Is `updated_since` **inclusive or exclusive**? Inclusive is what we want
  (re-reading one row is free; skipping one is not), but we need to know which
  it is to size the rewind correctly.
- Does `updated_at` change on **every** field write, or only on a status
  transition? A `PATCH` (§8a) that corrects a phone number should bump it, or
  our sweep will never see the correction.

---

## 3. Failure reason — free text is not enough to act on

You built `failure_note` and said a stable enum would misrepresent what the
rider app actually collects, since it only offers free text today. That is a
fair and honest answer, and we would rather have the honest field than a fake
enum.

But it leaves us unable to **act**. For a grocery order a failed delivery is a
decision we have minutes to make — refund, re-dispatch, or write off the
perishables — and "the rider typed something" does not drive that. We are not
going to keyword-match Sinhala and English free text in production and call it
business logic.

**We are not asking you to change the rider app.** We are asking for the
smallest thing that lets us branch:

**(a) A coarse reason code alongside the note**, set by whoever/whatever
already triages these on your side, even if it is derived rather than picked:

```json
{
  "status": "failed",
  "failure_reason": "customer_unreachable",
  "failure_note": "Called 3 times, no answer"
}
```

Four values would cover almost all of our decisions:
`customer_unreachable` · `address_problem` · `customer_refused` (includes
refusing to pay COD) · `other`.

**(b) Failing that**, just tell us **whether the shipment will be re-attempted
or not**, which is a boolean and is something your system must already know:

```json
{ "status": "failed", "will_retry": true, "next_attempt_at": "2026-08-29T10:00:00+00:00" }
```

That single field is worth most of the value: it is the difference between
"tell the customer it's coming tomorrow" and "refund them now."

**And two questions that need no build at all:**

- How many attempts do you make before a shipment becomes `returned`?
- Can we ask for **one attempt only** on a given shipment? For perishables we
  would often rather have an immediate return than a parcel riding around in a
  van for two days.

---

## 4. Same-day / express service tier

You built `requested_delivery_date` / `requested_delivery_window` as
capture-only, explicitly not scheduled or enforced, with no rider-job-card
visibility. Understood — and we will **not** show a customer a guaranteed
window on that basis. We send the fields and treat them as a note.

The gap that remains is commercial rather than technical: **there is no
service level we can price.** `POST /rates` prices on weight alone, so we
cannot offer same-day or express delivery at a higher fee, because there is no
way to ask you for one or to be quoted for one.

Same-day grocery delivery is a product we want to sell, and it is the single
biggest thing on this list that would make us *more* money for you rather than
just cost you engineering.

When you are ready, the shape we would need is small:

```http
POST /ecommerce/rates   { "service_level": "same_day" }
```
→ a different `amount_lkr`, and a `service_level` echoed on the booked
shipment.

**What we need from you now is not the build — it is a yes/no and a rough
horizon.** If same-day is not on your roadmap this year, we will design the
storefront around standard delivery only and stop holding space for it.

---

## 5. §7a is now load-bearing infrastructure — please treat it that way

We accept that webhook retry (§6.1) needs a job queue you do not have, and we
are not asking again.

But please register the consequence: because there is no retry, **`GET
/ecommerce/shipments` is not a convenience endpoint for us — it is our source
of truth for whether a delivery happened.** Webhooks are a latency
optimisation on top of it.

That means:

- If §7a is removed, renamed, or its semantics change, we stop learning about
  deliveries — silently. No error, no alarm; orders just quietly never close.
- If §7a gets rate-limited more aggressively than the rest of the API, the
  same thing happens under load, which is exactly when it matters most.

**We are asking for two commitments, not code:**

1. Tell us before you change §7a's shape or availability.
2. Confirm §7a shares the account's normal rate limit and is not separately
   throttled.

Related and cheap, if it is ever easy: a way to re-drive a failed push from
the existing delivery log —
`POST /ecommerce/webhook-endpoints/{id}/deliveries/{delivery_id}/replay`. Not
a request, just a note that it would pay for itself the first time an endpoint
of ours has a bad ten minutes.

---

## 6. Cold chain — we need the answer, not the feature

You built `handling` as capture-only tags: no surcharge, no cold-chain
service, no rider-app visibility. Fine as a first step.

The question we asked and still need answered is the one that changes our
product, not your code:

> **Does SribeesExpress operate any temperature-controlled or expedited
> handling for perishables at all — today, or planned?**

If the answer is no, we will cap which grocery categories ship through this
API (no frozen, no chilled dairy, no ice cream) and say so on the storefront.
That is a decision we would rather make deliberately now than discover after
the first melted delivery and the refund that follows.

A secondary, smaller ask: if `handling` tags never reach the rider, they are
currently write-only metadata. Even printing them on the job card — "PERISHABLE
— deliver first" — would make the field mean something.

---

## 7. Sandbox and keys — the actual thing blocking us today

Everything above is a roadmap conversation. **This is what is stopping us this
week.**

- [ ] **Issue us a sandbox key** (`sk_test_...`). We have built and tested
      against a local double; we now need to test against you.
- [ ] **Tell us the production rate-limit ceiling** you can give us, and
      confirm whether the limit is **per key or per account** — we want
      separate sandbox and production keys without splitting one budget.
- [ ] **Confirm `/locations/resolve` counts against the same limit.** We call
      it on address save rather than on checkout precisely to stay clear of
      the hot path, but we should know.
- [ ] **Confirm `429` always carries `Retry-After`**, including on burst
      rejections. Our client honours it and retries once; without the header
      it degrades to treating you as down.
- [ ] **Sandbox isolation:** you said sandbox rows are real rows, tagged. Do
      sandbox shipments ever appear in remittances (§1) or invoices? And do
      sandbox and live share the `merchant_order_ref` namespace — i.e. can a
      test booking with ref `FC-20260828-A7K2M9` collide with a live order of
      the same ref later?
- [ ] **Webhook registration:** we will register our endpoint over HTTPS and
      capture the once-shown secret. Confirm rotating it invalidates the old
      one **immediately** (the contract says so; we want it confirmed before
      we build the rotation runbook).

---

## 8. Field-level questions from writing the client

None of these are blockers. All of them are things we had to guess at, and a
wrong guess surfaces as a `422` in production rather than in review.

### Lengths and formats

1. **`recipient_name` and `recipient_phone` max lengths** are not documented —
   only `recipient_address` (255). What are they? We truncate the address; we
   currently do not truncate the other two.
2. **Phone format.** Is `+94771234567` (E.164) required, or is `0771234567`
   accepted? Our users enter both. If you normalise, say so; if you validate,
   we need to normalise before sending.
3. **`handling`** — free-form, or is there a vocabulary you actually
   recognise? We send `["perishable"]`. If unrecognised tags are silently
   dropped we would rather know than assume they landed.
4. **`requested_delivery_window`** — is `"09:00-12:00"` a parsed format or an
   opaque string? If parsed, what separator/format, and what timezone is it
   read in?

### Idempotency and identifiers

5. **Does `merchant_order_ref` idempotency ever expire?** We rely on retrying a
   timed-out booking with the same ref. If the dedupe window is bounded (24h?
   30 days?), a delayed retry after an outage would double-book.
6. **Is a `quote_id` single-use permanently?** We see `quote_already_used`, and
   we handle it by re-quoting. Confirming it can never be reused lets us stop
   retrying it at all.
7. **`pickup_location_id` uniqueness.** If we lose our stored id and re-run our
   registration sync, will `POST /ecommerce/pickup-locations` with an identical
   `name`/`address` create a **duplicate**, or return the existing one? We
   currently `PATCH` when we hold an id and `POST` when we don't — if POST is
   not idempotent on name, a restore-from-backup would silently fork our branch
   registry.
8. **Deactivated pickup locations.** `DELETE` soft-deactivates. Shipments
   already booked snapshot the address, so they are fine — but can a
   deactivated `pickup_location_id` still be *sent* on a new booking, and does
   it 404 or silently fall back to the account default? We would much rather it
   errored.

### §7a specifics

9. **Is there a maximum `limit`?** Documented default is 50; we send 100.
10. **Can `status_filter` take more than one value?** We want "everything not
    yet terminal" and currently fetch all statuses and filter client-side,
    which is more rows than either of us needs. Comma-separated would do.
11. **Do terminal shipments stay in the list forever?** If a
    `delivered`/`cancelled` shipment from a year ago still matches an
    unfiltered sweep, our pagination cost grows without bound. A server-side
    retention window, or confirmation that `updated_since` is the intended
    guard, would settle it.
12. **Do §7a rows really carry the `handover` and `cod` blocks?** The contract
    says the same shape as §7 minus `history`, which implies yes — and our
    sweep depends on it, because that is how we learn a COD collection when a
    webhook is lost. Please confirm explicitly; this is the single most
    load-bearing assumption in our integration.

### Behaviour

13. **Can `cod.collected` ever flip on a shipment that is not `delivered`** —
    e.g. cash taken, then the handover fails? We treat collected and delivered
    as independent, which we believe matches §8, but we would like it
    confirmed rather than inferred.
14. **What happens to `cod` on a `returned` shipment** that had already been
    collected? Does `collected` revert to `false`, or stay `true` with a
    refund handled out of band?

---

## 9. What we need back

In priority order, and most of it is answers rather than code:

| Priority | Item | What we need |
|---|---|---|
| 1 | **Sandbox key** (§7) | Issue it |
| 2 | **`updated_at` on `ShipmentOut`** (§2) | One field — please treat as a bug fix |
| 3 | **COD remittance** (§1) | Your payout terms in prose + the minimal `cod-balance` read |
| 4 | **§8.12** — confirm `handover`/`cod` on §7a rows | Yes/no |
| 5 | **§3** — `will_retry` or a coarse reason code | Whichever is cheaper for you |
| 6 | **§4, §6** — same-day tier, cold chain | Yes/no + rough horizon |
| 7 | **§8** — the rest of the field questions | Answers, no build |

Please reply per item with **will build** (+ rough date) / **already works
this way** (+ where) / **won't build** (+ what we should do instead). The
last one is a perfectly good answer — several of your round-1 answers were
exactly that and they let us design around the constraint instead of waiting
on it.
