# SRIBEESonline → SribeesExpress: open items

**From:** SRIBEESonline (grocery e-commerce platform)
**To:** the team/agent building the SribeesExpress delivery system
**Against:** `Ecommerce Integration API — Integration Contract`, revision of
2026-08-28 (round-2 response)
**Last updated:** 2026-08-30
**History:** round 1 = `SRIBEESEXPRESS_INTEGRATION_GAPS.md` (closed, historical)

---

## 0. Round 2 is closed — thank you

Everything you shipped in the round-2 response is **integrated, and verified
end to end** against a local double built to the revised contract:

| What you did | Our side |
|---|---|
| `updated_at` on `ShipmentOut` (confirmed bug, fixed) | Sweep cursor now advances from **your** timestamp, not our clock — see §4 |
| COD remittances + `cod-balance` + `remittance_id` | Client + admin reads built; `remittance_id` persisted per order |
| `will_retry` | Persisted; this is the field our refund-vs-wait decision branches on |
| `status_filter` comma-separated, `limit` max 200 | Client sends lists; limit clamped |
| `updated_since` inclusive | Cursor replays the boundary instead of rewinding blindly |
| `pickup_location_name_already_registered` (409) | Our registration sync treats it as "already exists" and recovers the id by name |
| `pickup_location_deactivated` (409) | Handled |
| Field maxima (150 / 30 / 255) | Truncated client-side before booking |
| All 14 field-level answers | Applied |

Two answers we acted on rather than just noted:

- **Cold chain: none, none planned.** Understood and accepted. We are capping
  frozen, chilled and ice-cream categories out of what ships through this API.
  That is a product decision on our side, now made — no further ask.
- **Payout terms.** Thank you for stating them plainly. Weekly,
  delivery-charge-only deduction, ops-triggered rather than scheduled,
  returned-COD carries forward. That is enough to model our cash flow.

**Nothing in this document is blocking your engineering.** The one thing
blocking *us* is §1, and it is a deployment, not a feature.

---

## 1. THE blocker — a reachable sandbox

You issued a sandbox key through the real code path and then told us straight
that it only ever existed against a throwaway local database, because this
backend has not been deployed anywhere we can reach. We appreciate that being
said out loud rather than left for us to discover against a dead URL.

So the position is simply: **we cannot call you yet.** Everything on our side
is written and green against a local double, and none of it has touched your
actual service.

What we need, whenever the deploy happens:

- [ ] A **base URL** we can reach.
- [ ] A **sandbox key** (`sk_test_...`) that lives against that deployment.
- [ ] Confirmation of which contract revision that deployment is running — we
      are built against the 2026-08-28 round-2 revision, including
      `updated_at`, `will_retry`, `remittance_id`, `cod-balance` and the two
      new pickup-location `409`s. If the deployed build predates any of those,
      tell us which, and we will gate rather than guess.

No rush implied — we just cannot start real integration testing before it, and
we would rather wait for a correct deploy than a fast one.

---

## 2. Sandbox COD is not isolated from billing — please flag it before we test

You told us, again straight, that `environment` is tagged on rows but nothing
downstream filters on it: **a sandbox booking's collected COD would show up in
`cod-balance` and in a real remittance run.**

We are not asking you to fix this before the deploy. We are asking for one
thing so it does not quietly become a money problem:

> **Tell us the day real COD starts flowing through your production
> remittances.** Until then we will exercise COD freely in sandbox. After
> then, sandbox COD tests become a reconciliation hazard for both of us, and
> we will stop until isolation exists.

If isolation lands before that day, better still — but the warning is what we
actually need, not the feature.

---

## 3. The question you asked us: one-attempt-only for perishables

You asked whether a per-order "attempt once, then return" flag is worth
prioritising next. **Our answer: yes, but it is not urgent — put it behind
sandbox isolation (§2) and anything on your own critical path.**

The reasoning, so you can weigh it properly:

- Our exposure is real but narrow. Most of a grocery basket survives a second
  attempt; it is the chilled and frozen lines that do not, and per §0 we are
  no longer shipping those through this API at all. So the flag would today
  protect fresh produce and bakery — perishable enough to matter, not
  perishable enough to be an emergency.
- `will_retry` already covers the *reactive* half. We learn a delivery failed
  and whether a retry is coming, and can act on it. What is missing is only
  the *proactive* half — telling you up front not to bother.
- If it ever becomes cheap, the shape we would want is a boolean on
  `POST /ecommerce/shipments` (`max_attempts: 1`, or
  `return_on_first_failure: true`), not a new endpoint.

Two smaller ones from your round-2 response, same priority — noted, not
pushed:

- `handling` tags reaching the rider job card. Right now they are write-only
  metadata; printing "PERISHABLE" on the card is most of the value.
- The webhook-delivery replay endpoint. We have the §7a sweep as our source of
  truth, so this is a support convenience, not a correctness need.

---

## 4. How we use `updated_at` — worth knowing, in case it constrains you

You fixed the missing field, and it changed our sweep for the better. Since
§5's commitment means you will tell us before §7a changes, here is exactly
what we now depend on, so you know what "changing it" would break:

- **The cursor is the newest `updated_at` we saw**, replayed on the next call.
  It is deliberately *your* clock: a row you touch after our sweep is stamped
  later than anything we just read, so it cannot slip past the boundary, and
  no assumption about two machines agreeing on the time is involved.
- **We rely on `updated_since` being inclusive**, which you confirmed. That is
  what makes replaying the boundary safe rather than a skip.
- **We rely on `updated_at` bumping on every field write**, which you also
  confirmed — a `PATCH` that corrects a phone number has to move it, or the
  correction never reaches us.
- We still re-read a **one-minute window** each sweep. Not distrust: a row can
  be stamped before our query runs and only become visible once it commits,
  and that row would otherwise sit permanently just behind the cursor.
- We keep a fallback to our own start-of-run clock if a page ever comes back
  with no `updated_at` at all. If the field regresses, our sweep degrades
  instead of freezing its cursor and re-reading your whole account forever.

Nothing to do here. It is written down so that if `updated_at` semantics ever
have to change, you can see what it costs us before it lands.

---

## 5. One number we owe you

You asked for our expected volume so you can set the production key's
rate-limit ceiling at issuance. We will send a real figure once we have run a
week of production checkout traffic against the default 60/min and measured
the actual `/rates` and `/locations/resolve` call rate.

Our rough expectation is that `/rates` dominates — it is the only one on the
checkout hot path — and that `/locations/resolve` is negligible, since we call
it when an address is saved rather than on every order. We would rather give
you a measured number than an invented one.

---

## 6. Response requested

Only §1 needs an answer, and only when the deploy is done: base URL, sandbox
key, and which contract revision it runs.

§2 needs a heads-up rather than a reply — the day production COD goes live.

§3 is our answer to your question, not a new ask.
