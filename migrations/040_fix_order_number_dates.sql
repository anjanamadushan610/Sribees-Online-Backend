-- Migration 040: Align created_at timestamps with order_number dates in Asia/Colombo timezone
-- Resolves midnight boundary mismatch between order number date component and displayed local date.

-- Data write REMOVED 2026-11-07 (owner's decision): every deploy replays every
-- migration, so this one-off order-date shift ran again on each release and could change live
-- data. The file stays so the replay list does not change.
