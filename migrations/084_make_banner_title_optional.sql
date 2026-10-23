-- 084_make_banner_title_optional.sql
-- Allow promotional banners to have no overlay title text (e.g. for pre-designed
-- image-only artwork banners).

ALTER TABLE banners ALTER COLUMN title DROP NOT NULL;
