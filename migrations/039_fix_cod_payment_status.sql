-- Migration 039: Fix CASH_ON_DELIVERY payment status before delivery
-- COD orders must remain pending until the order status becomes delivered.

UPDATE orders
SET payment_status = 'pending'
WHERE payment_method = 'cash_on_delivery'
  AND status NOT IN ('delivered', 'refunded', 'cancelled')
  AND payment_status = 'paid';
