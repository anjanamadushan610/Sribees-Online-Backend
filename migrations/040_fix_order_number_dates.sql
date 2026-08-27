-- Migration 040: Align created_at timestamps with order_number dates in Asia/Colombo timezone
-- Resolves midnight boundary mismatch between order number date component and displayed local date.

UPDATE orders
SET created_at = created_at - INTERVAL '6 hours'
WHERE order_number LIKE 'SO-%'
  AND to_char(created_at AT TIME ZONE 'Asia/Colombo', 'YYYYMMDD') != substring(order_number from 4 for 8);
