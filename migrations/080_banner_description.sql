-- 080_banner_description.sql
-- A short optional paragraph under a banner's title and subtitle. The web
-- storefront's wide hero slide has room for it; the mobile carousel ignores it.

ALTER TABLE banners ADD COLUMN IF NOT EXISTS description VARCHAR(300);
