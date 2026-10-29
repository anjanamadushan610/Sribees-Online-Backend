# Linked Partner Wallet rollout

## Approved rules

- A signed-in customer submits an application; only a Super Admin approves it.
- Approval creates a Partner linked uniquely to that exact customer UUID. Email matching never links accounts automatically.
- Partner is an explicit role, even with no Master Partner assigned. Only Master Partners may recruit.
- The existing customer invite code becomes the linked Partner code. Existing links continue to work.
- Existing referred customers move to the linked Partner with their original referral dates.
- Unpaid pending/claimable customer commissions move to the Partner. Already-paid customer credits stay in Sribees Wallet and are never credited twice.
- Mature linked-Partner commission credits Partner Wallet automatically; pending amounts remain unavailable until the existing return window ends.
- Partner Wallet -> Sribees Wallet is atomic and one-way. Shopping credit cannot be withdrawn or transferred back.
- Bank withdrawal requests reserve funds immediately. Finance pays externally, then a Super Admin records the payment reference. Rejection releases funds once.
- Reversals debit the original wallet. A debt can make Partner Wallet negative; further spending is blocked until sufficient balance exists.
- Legacy unlinked Partner accounts keep their existing ledger/payment flow. Linking an existing legacy identity requires a separately verified support migration; this implementation does not guess ownership from email.

## Transaction guarantees

Money uses Decimal and cents. Mutations use database row locks and append-only entries with unique per-Partner references. Client request UUIDs remain stable across uncertain-response retries. A reused key with different amount/details returns a conflict. A failed shopping credit rolls back the Partner debit. Customer/Partner/Admin token realms remain separate, and closed or blocked linked customers cannot use Partner login.

## API

Customer: GET /wallet/partner; POST /wallet/partner/apply; POST /wallet/partner/transfer-to-sribees; POST /wallet/partner/withdrawals.

Partner Portal: GET /partner/wallet; POST /partner/wallet/transfer-to-sribees; POST /partner/wallet/withdrawals.

Super Admin: GET /admin/partners/applications; POST /admin/partners/applications/{id}/decision; GET /admin/partners/withdrawals; POST /admin/partners/withdrawals/{id}/decision; GET /admin/partners/{id}/wallet.

No endpoint accepts a customer/partner beneficiary id from a customer money request. There is no reverse-transfer endpoint.

## Rollout gates — do not skip

1. Run backend unit/security/Postgres concurrency tests and migration rehearsal twice. Run mobile, Admin and Partner Portal tests/builds.
2. Apply additive migration 095 through the existing deployment process. No paid customer balance is backfilled into cash.
3. Keep LINKED_PARTNER_WALLET_ENABLED=false until a controlled test account upgrade, transfer, withdrawal rejection and manual payout-recording flow are reviewed.
4. If Finance outbox is enabled, its consumer must implement the following contract before PARTNER_WALLET_FINANCE_READY=true. Otherwise the new endpoints refuse rollout.
5. Enable LINKED_PARTNER_WALLET_ENABLED=true only after the controlled test and accounting review. Do not use real customer accounts for destructive testing.

## Finance consumer contract (needs Finance repository verification)

- New wallet.movement reason: partner_wallet_transfer. This is a transfer of liability from Partner funds to shopping credit, not a fresh commission expense.
- New partner.wallet.movement event: entry_id, partner_id, kind, reference, signed amount, balance_after. Commission credits, reversals, reserved withdrawals and released reservations must be posted idempotently by entry_id.
- New partner.withdrawal.paid event: withdrawal_id, partner_id, amount, payment_reference. This records external settlement; it must not debit the reserved balance again.
- Existing commission.changed events still describe accrual/status changes. Avoid booking the same commission expense a second time when it reaches Partner Wallet.
- Reconcile Partner Wallet balance against entry sum; reserved pending withdrawals are not spendable. Reconcile paid withdrawals against actual bank references.

No production accounts or money have been migrated by development/testing commands. The feature defaults to closed.
