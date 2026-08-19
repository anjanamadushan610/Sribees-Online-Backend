-- 036_normalize_unit_labels.sql
--
-- Normalise the pack-size suffix in translated product copy to the Latin unit
-- symbols Sri Lankan packaging already prints: "1 kg", "500 g", "1 L".
--
-- The Sinhala and Tamil copy seeded by 035 spelled the unit out in-script
-- ("කි.ග්‍රෑ. 1", "500 கி"). QA found the abbreviated in-script forms hard to
-- read, and they also made already-long names longer — which is part of what
-- pushed the product cards into truncating.
--
-- Plain REPLACE over the known unit tokens rather than a regex: the seed set is
-- small and fixed, and a literal replace cannot misfire on a product name that
-- happens to contain the same letters. Idempotent — a second run matches
-- nothing, because the tokens it looks for are gone.

BEGIN;

-- --- Sinhala -----------------------------------------------------------------
UPDATE products SET
  name_si              = replace(replace(replace(replace(replace(replace(replace(
                           coalesce(name_si, ''),
                           'කි.ග්‍රෑ. 1', '1 kg'), 'කි.ග්‍රෑ. 5', '5 kg'),
                           'ග්‍රෑම් 200', '200 g'), 'ග්‍රෑම් 250', '250 g'),
                           'ග්‍රෑම් 400', '400 g'), 'ග්‍රෑම් 500', '500 g'),
                           'ලීටර් 1', '1 L'),
  short_description_si = replace(replace(replace(replace(replace(replace(replace(
                           coalesce(short_description_si, ''),
                           'කි.ග්‍රෑ. 1', '1 kg'), 'කි.ග්‍රෑ. 5', '5 kg'),
                           'ග්‍රෑම් 200', '200 g'), 'ග්‍රෑම් 250', '250 g'),
                           'ග්‍රෑම් 400', '400 g'), 'ග්‍රෑම් 500', '500 g'),
                           'ලීටර් 1', '1 L'),
  description_si       = replace(replace(replace(replace(replace(replace(replace(
                           coalesce(description_si, ''),
                           'කි.ග්‍රෑ. 1', '1 kg'), 'කි.ග්‍රෑ. 5', '5 kg'),
                           'ග්‍රෑම් 200', '200 g'), 'ග්‍රෑම් 250', '250 g'),
                           'ග්‍රෑම් 400', '400 g'), 'ග්‍රෑම් 500', '500 g'),
                           'ලීටර් 1', '1 L')
WHERE name_si IS NOT NULL OR short_description_si IS NOT NULL OR description_si IS NOT NULL;

-- --- Tamil -------------------------------------------------------------------
-- "கிகி" (kg) is replaced before the bare "கி" (g) tokens so that "1 கிகி"
-- cannot be half-rewritten into "1 gகி".
UPDATE products SET
  name_ta              = replace(replace(replace(replace(replace(replace(replace(replace(
                           coalesce(name_ta, ''),
                           '1 கிகி', '1 kg'), '5 கிகி', '5 kg'),
                           '750 மிலி', '750 ml'), '1 லி', '1 L'),
                           '200 கி', '200 g'), '250 கி', '250 g'),
                           '400 கி', '400 g'), '500 கி', '500 g'),
  short_description_ta = replace(replace(replace(replace(replace(replace(replace(replace(
                           coalesce(short_description_ta, ''),
                           '1 கிகி', '1 kg'), '5 கிகி', '5 kg'),
                           '750 மிலி', '750 ml'), '1 லி', '1 L'),
                           '200 கி', '200 g'), '250 கி', '250 g'),
                           '400 கி', '400 g'), '500 கி', '500 g'),
  description_ta       = replace(replace(replace(replace(replace(replace(replace(replace(
                           coalesce(description_ta, ''),
                           '1 கிகி', '1 kg'), '5 கிகி', '5 kg'),
                           '750 மிலி', '750 ml'), '1 லி', '1 L'),
                           '200 கி', '200 g'), '250 கி', '250 g'),
                           '400 கி', '400 g'), '500 கி', '500 g')
WHERE name_ta IS NOT NULL OR short_description_ta IS NOT NULL OR description_ta IS NOT NULL;

-- Empty strings would defeat the per-field English fallback, so put NULLs back
-- where coalesce() above turned a genuinely absent translation into ''.
UPDATE products SET name_si = NULLIF(name_si, ''), short_description_si = NULLIF(short_description_si, ''),
                    description_si = NULLIF(description_si, ''), name_ta = NULLIF(name_ta, ''),
                    short_description_ta = NULLIF(short_description_ta, ''), description_ta = NULLIF(description_ta, '');

COMMIT;
