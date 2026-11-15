-- Storefront category ordering. Replaying this migration preserves saved order.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'categories' AND column_name = 'display_order'
    ) THEN
        ALTER TABLE categories ADD COLUMN display_order INTEGER NOT NULL DEFAULT 2147483647;
        WITH positions AS (
            SELECT category_id,
                   row_number() OVER (PARTITION BY parent_category_id ORDER BY name, category_id) - 1 AS position
            FROM categories
        )
        UPDATE categories AS category
        SET display_order = positions.position
        FROM positions WHERE category.category_id = positions.category_id;
    END IF;
END $$;
