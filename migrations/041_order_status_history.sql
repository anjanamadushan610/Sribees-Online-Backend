-- Migration 041: Create order_status_history table and backfill history for existing orders.

CREATE TABLE IF NOT EXISTS order_status_history (
    history_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id UUID NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    old_status VARCHAR(50),
    new_status VARCHAR(50) NOT NULL,
    changed_by VARCHAR(255) DEFAULT 'System',
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- Ensure history_id has DEFAULT gen_random_uuid() if pre-created by SQLAlchemy create_all()
ALTER TABLE order_status_history ALTER COLUMN history_id SET DEFAULT gen_random_uuid();

CREATE INDEX IF NOT EXISTS idx_order_status_history_order_id ON order_status_history(order_id);
CREATE INDEX IF NOT EXISTS idx_order_status_history_created_at ON order_status_history(created_at);

-- Backfill initial "Placed" status entry for existing orders
INSERT INTO order_status_history (history_id, order_id, old_status, new_status, changed_by, notes, created_at)
SELECT 
    gen_random_uuid(),
    order_id,
    NULL,
    'pending',
    'Customer',
    'Order placed by customer',
    created_at
FROM orders
WHERE NOT EXISTS (
    SELECT 1 FROM order_status_history h WHERE h.order_id = orders.order_id AND h.new_status = 'pending'
);

-- Backfill "Shipped" status entries for existing shipped orders
INSERT INTO order_status_history (history_id, order_id, old_status, new_status, changed_by, notes, created_at)
SELECT 
    gen_random_uuid(),
    order_id,
    'processing',
    'shipped',
    'System (Fulfillment)',
    'Order dispatched to courier',
    shipped_at
FROM orders
WHERE shipped_at IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM order_status_history h WHERE h.order_id = orders.order_id AND h.new_status = 'shipped'
);

-- Backfill "Delivered" status entries for existing delivered orders
INSERT INTO order_status_history (history_id, order_id, old_status, new_status, changed_by, notes, created_at)
SELECT 
    gen_random_uuid(),
    order_id,
    'shipped',
    'delivered',
    'System (Delivery)',
    'Order successfully delivered to customer',
    delivered_at
FROM orders
WHERE delivered_at IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM order_status_history h WHERE h.order_id = orders.order_id AND h.new_status = 'delivered'
);
