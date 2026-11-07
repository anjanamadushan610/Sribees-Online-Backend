-- Migration 039: Fix CASH_ON_DELIVERY payment status before delivery
-- COD orders must remain pending until the order status becomes delivered.

-- Data write REMOVED 2026-11-07 (owner's decision): every deploy replays every
-- migration, so this one-off COD payment-status fix ran again on each release and could change live
-- data. The file stays so the replay list does not change.
