# Card payment at delivery (COD conversion)

This is a coordinated, opt-in feature. `DELIVERY_CARD_PAYMENT_ENABLED` defaults to
false. Do not enable production until both updated mobile apps and Express are
available and the development device test has passed.

## Contract and money ownership

Only the authenticated owner of an unpaid COD order that is out for delivery may
start a card payment. The merchant persists a unique intent before reserving
`POST /ecommerce/shipments/{waybill}/card-payment` under the order branch's courier
credentials. Express checks client/environment ownership, exact amount, rider,
shipment state and previous cash collection under the same row lock used by
rider cash collection and delivery transitions.

Operations are `reserve`, `paid`, `failed`, with `reference`, decimal `amount`,
and a verified `gateway_reference` for paid settlement. Express is not a payment
gateway: only the authenticated merchant can attest a server-verified result.
An idempotent paid settlement makes collectible COD zero, retaining the original
amount/reference for audit. It never creates a rider cash-liability row.

The Online backend keeps the existing order/status/shipment and verifies Marx
server-to-server. A gateway receipt and the existing Finance outbox event commit
together before Express is allowed to complete delivery. The existing ordered
Finance feed books that receipt before delivered revenue; no separate rider cash
receipt is created. Repeated customer taps resume the same checkout.

## Recovery and operational rules

- Pending gateway outcomes NEVER unblock cash by age, browser close or a lost
  network response. Only a definitive gateway failure releases the reservation.
- A background sweep retries gateway checks and lost Express acknowledgements.
  An interrupted start whose checkout URL was never committed/exposed is safely
  released after five minutes; a started checkout with a gateway reference is
  reconciled indefinitely until a definitive answer.
- If a gateway contradicts a definitive failure with a later paid answer, the
  existing late-payment policy credits the money to the customer wallet once;
  it does not silently turn an already-released COD parcel into prepaid.
- Do not collect cash or hand over goods while the rider screen says card
  verification is pending. Old Rider builds must not be used for this feature.
- Keep the branch's Express account/environment configuration stable while a
  payment is pending. Rotate credentials within the same merchant/environment;
  do not move an in-flight shipment into another merchant or environment.
- An unresolved gateway/Express incident needs support reconciliation using the
  order, payment-attempt reference and gateway reference. Never clear a hold by
  editing database fields or trusting a screenshot of a payment.

## Rollout and acceptance

1. Express development: apply Alembic revision d7c0a1b2e310 and deploy the endpoint.
2. Online development: apply replay-safe migration 107, leaving the flag false.
3. Install updated Customer and Rider test builds against development services.
4. Enable the flag in development only. Use provider sandbox/test credentials.
5. Test success, gateway decline, browser cancellation, disconnect/reconnect,
   repeated taps, duplicate callbacks, cash-vs-card race, stale rider screen,
   lost courier acknowledgement and Finance reconciliation. Confirm no cash is
   asked for after a verified card payment. Confirm normal COD/prepaid/returns.
6. Obtain owner approval before a coordinated production rollout. No production
   feature activation or real-card test is part of these PRs.

Local validation cannot replace the two-device development test. The Rider CI
APK uses debug signing; it is a test artifact, not a Play Store release.

## Validation when sandbox credentials are unavailable

The disposable CI database can exercise the customer payment endpoints with
`tests/integration/fake_marx.py` as an in-process HTTP simulator. The actual
Marx request builder, response parser and payment reconciliation service run.
`tests/integration/test_delivery_card.py` covers success, pending/declined
answers, gateway outages, lost courier acknowledgements and forged returns.
Express's own database tests exercise the rider cash/card exclusion and paid
response. These suites use synthetic credentials and fixture return URLs only.
They do not require a registered Return URL or changes to deployed settings.

This verifies application payment handling. Real hosted checkout, 3DS, merchant
authorization and a physical two-device handover remain separate acceptance
checks before production activation.

For an owner-approved live test using the existing merchant registration,
development deployment accepts `reuse_existing_marx=true`. It copies the three
existing Marx secret values to the development environment without printing
them, requires no in-flight development payment attempts, and checks that the
configured Return URL exactly matches the existing value using a digest.
Gateway methods and delivery-card payments remain disabled during preparation.
An ordinary development deployment restores the original development settings.
Live payment activation and the test customer/rider must be arranged separately.

## Hosted payment methods at delivery

`GET /payments/orders/{order_id}/methods` returns the authenticated owner's
currently available online methods. The updated mobile client displays their
names, descriptions and generic icons, then sends the chosen `method` to the
existing `/pay` endpoint. Omitting a method retains the original card behavior.
A pending attempt returns only its original method and resumes the same page.
Switching methods during verification is rejected.

Methods come from the shared enabled catalog and registered gateway adapters.
Bank Transfer remains disabled until Marx enables the merchant service. A QR
provider must have a real hosted checkout adapter, verified settlement and an
enabled catalog entry before appearing. No speculative QR provider is enabled.
After this mobile version is installed, additional supported hosted methods
can appear through backend changes. Native QR scanning would require separate
mobile functionality.
