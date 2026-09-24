-- Migration 035: multilingual product & category content
--
-- Customers pick a language (si/ta/en) in onboarding or Settings; catalog
-- content must follow that choice, not just the app chrome. Three languages
-- are a fixed product decision, so the translations live in flat columns on
-- the row rather than a join table — every product/category read is already
-- branch-filtered and paginated, and adding a per-language join to those
-- queries buys nothing at three locales.
--
-- The existing `name`/`description`/`short_description` columns stay as the
-- ENGLISH values and remain NOT NULL where they already were: they are the
-- fallback whenever a translation is missing, so a half-translated catalog
-- degrades to English instead of showing blanks.
--
-- Schema is created by SQLAlchemy Base.metadata.create_all() on fresh DBs; run
-- this against pre-existing databases. Safe to re-run.

-- ---------------------------------------------------------------------------
-- 1. Columns
-- ---------------------------------------------------------------------------

ALTER TABLE products
    ADD COLUMN IF NOT EXISTS name_si VARCHAR(255),
    ADD COLUMN IF NOT EXISTS name_ta VARCHAR(255),
    ADD COLUMN IF NOT EXISTS description_si TEXT,
    ADD COLUMN IF NOT EXISTS description_ta TEXT,
    ADD COLUMN IF NOT EXISTS short_description_si VARCHAR(500),
    ADD COLUMN IF NOT EXISTS short_description_ta VARCHAR(500);

ALTER TABLE categories
    ADD COLUMN IF NOT EXISTS name_si VARCHAR(100),
    ADD COLUMN IF NOT EXISTS name_ta VARCHAR(100),
    ADD COLUMN IF NOT EXISTS description_si TEXT,
    ADD COLUMN IF NOT EXISTS description_ta TEXT;

COMMENT ON COLUMN products.name_si IS
    'Sinhala product name. NULL falls back to products.name (English).';
COMMENT ON COLUMN products.name_ta IS
    'Tamil product name. NULL falls back to products.name (English).';
COMMENT ON COLUMN categories.name_si IS
    'Sinhala category name. NULL falls back to categories.name (English).';
COMMENT ON COLUMN categories.name_ta IS
    'Tamil category name. NULL falls back to categories.name (English).';

-- ---------------------------------------------------------------------------
-- Demo seed: skipped on a database marked
--     ALTER DATABASE <db> SET sribees.demo_seed = 'off';
-- which production is. Deploys replay every migration, so without this the
-- seed re-creates demo customers, orders, admins and catalog rows every time
-- they are cleared. Unmarked databases (dev, QA, a fresh local one) still seed.
-- ---------------------------------------------------------------------------
SELECT coalesce(nullif(current_setting('sribees.demo_seed', true), ''), 'on') <> 'off' AS demo_seed \gset
\if :demo_seed
\else
\echo 'Skipped: sribees.demo_seed is off for this database.'
\quit
\endif

-- ---------------------------------------------------------------------------
-- 2. Seed the existing catalog
--
-- Matched on `slug` (unique, and stable across environments) rather than the
-- seed UUIDs, so this also applies to catalogs re-seeded with fresh ids.
-- Every statement is an idempotent UPDATE — re-running only rewrites the same
-- values.
-- ---------------------------------------------------------------------------

-- Categories -----------------------------------------------------------------

UPDATE categories SET
    name_si = 'පාන වර්ග',
    name_ta = 'பானங்கள்',
    description_si = 'නැවුම් බීම, පළතුරු යුෂ, සිසිල් බීම සහ සෞඛ්‍ය පාන.',
    description_ta = 'புத்துணர்ச்சி பானங்கள், சாறுகள், குளிர்பானங்கள் மற்றும் ஆரோக்கிய பானங்கள்.'
WHERE slug = 'beverages';

UPDATE categories SET
    name_si = 'කොකා-කෝලා',
    name_ta = 'கோகோ-கோலா',
    description_si = 'ලීටර් 1, මි.ලී. 500',
    description_ta = '1 லி, 500 மிலி'
WHERE slug = 'cocacola';

UPDATE categories SET
    name_si = 'කිරි නිෂ්පාදන සහ බිත්තර',
    name_ta = 'பால் பொருட்கள் & முட்டை',
    description_si = 'කිරි, දී, යෝගට්, බටර් සහ ගොවිපල බිත්තර.',
    description_ta = 'பால், தயிர், யோகர்ட், வெண்ணெய் மற்றும் பண்ணை முட்டை.'
WHERE slug = 'dairy-and-eggs';

UPDATE categories SET
    name_si = 'නැවුම් එළවළු සහ පලතුරු',
    name_ta = 'புதிய காய்கறி & பழங்கள்',
    description_si = 'දේශීය ගොවීන්ගෙන් ලබාගත් පලතුරු සහ එළවළු.',
    description_ta = 'உள்ளூர் விவசாயிகளிடமிருந்து பெறப்பட்ட பழங்கள் மற்றும் காய்கறிகள்.'
WHERE slug = 'fresh-produce';

UPDATE categories SET
    name_si = 'අත්‍යවශ්‍ය ආහාර ද්‍රව්‍ය',
    name_ta = 'அத்தியாவசிய உணவுப் பொருட்கள்',
    description_si = 'හාල්, පිටි, සීනි, කුළුබඩු සහ දෛනික ඉවුම් පිහුම් අවශ්‍යතා.',
    description_ta = 'அரிசி, மாவு, சர்க்கரை, மசாலா மற்றும் அன்றாட சமையல் தேவைகள்.'
WHERE slug = 'pantry-staples';

-- Products -------------------------------------------------------------------

UPDATE products SET
    name_si = 'කැරට් (ග්‍රෑම් 500)',
    name_ta = 'கேரட் (500 கி)',
    description_si = 'නුවරඑළිය කැරට්, මෘදු සහ මිහිරි.',
    description_ta = 'நுவரெலியா கேரட், மென்மையானதும் இனிப்பானதும்.',
    short_description_si = 'උඩරට කැරට්, ග්‍රෑම් 500',
    short_description_ta = 'மலைநாட்டு கேரட், 500 கி'
WHERE slug = 'carrot-500g';

UPDATE products SET
    name_si = 'අමු මිරිස් (ග්‍රෑම් 250)',
    name_ta = 'பச்சை மிளகாய் (250 கி)',
    description_si = 'නැවුම් අමු මිරිස්, පිරිසිදු තියුණු සැරක් සහිතයි.',
    description_ta = 'புதிய பச்சை மிளகாய், சுத்தமான காரத்துடன்.',
    short_description_si = 'අමු මිරිස්, ග්‍රෑම් 250',
    short_description_ta = 'பச்சை மிளகாய், 250 கி'
WHERE slug = 'green-chilli-250g';

UPDATE products SET
    name_si = 'තැඹිලි (එකක්)',
    name_ta = 'இளநீர் (ஒன்று)',
    description_si = 'උපරිම මිහිරි බව සඳහා ළපටි විට කඩාගත් තැඹිලි.',
    description_ta = 'அதிக இனிப்புக்காக இளமையில் பறிக்கப்பட்ட தம்பிலி.',
    short_description_si = 'තැඹිලි, එකක්',
    short_description_ta = 'தம்பிலி, ஒன்று'
WHERE slug = 'king-coconut-each';

UPDATE products SET
    name_si = 'නැවුම් කිරි (ලීටර් 1)',
    name_ta = 'புதிய பால் (1 லி)',
    description_si = 'පැස්චරීකරණය කළ සම්පූර්ණ ක්‍රීම් එළකිරි, ලීටර් 1 බෝතලයක.',
    description_ta = 'பாஸ்டரைஸ் செய்யப்பட்ட முழு கிரீம் பசும்பால், 1 லிட்டர் பாட்டிலில்.',
    short_description_si = 'සම්පූර්ණ ක්‍රීම් නැවුම් කිරි, ලීටර් 1',
    short_description_ta = 'முழு கிரீம் புதிய பால், 1 லி'
WHERE slug = 'fresh-milk-1l';

UPDATE products SET
    name_si = 'යෝගට් (කුසලාන 4)',
    name_ta = 'யோகர்ட் (4 கப்)',
    description_si = 'සුළු වශයෙන් රසකළ යෝගට්, කුසලාන හතරක පැකට්ටුවක්.',
    description_ta = 'லேசாக இனிப்பூட்டப்பட்ட யோகர்ட், நான்கு கப் தொகுப்பு.',
    short_description_si = 'යෝගට්, කුසලාන 4',
    short_description_ta = 'யோகர்ட், 4 கப்'
WHERE slug = 'set-yoghurt-4-cups';

UPDATE products SET
    name_si = 'බටර් (ග්‍රෑම් 200)',
    name_ta = 'வெண்ணெய் (200 கி)',
    description_si = 'ලුණු සහිත කිරි බටර්, තීරු ආවරණයක ග්‍රෑම් 200 කැටයක්.',
    description_ta = 'உப்பிட்ட பால் வெண்ணெய், படலத்தில் சுற்றப்பட்ட 200 கி கட்டி.',
    short_description_si = 'ලුණු සහිත බටර්, ග්‍රෑම් 200',
    short_description_ta = 'உப்பிட்ட வெண்ணெய், 200 கி'
WHERE slug = 'butter-200g';

UPDATE products SET
    name_si = 'සුදු සීනි (කි.ග්‍රෑ. 1)',
    name_ta = 'வெள்ளை சர்க்கரை (1 கிகி)',
    description_si = 'සිහින් කැට සහිත පිරිපහදු කළ සුදු සීනි.',
    description_ta = 'நுண்ணிய பருமன் கொண்ட சுத்திகரிக்கப்பட்ட வெள்ளை சர்க்கரை.',
    short_description_si = 'සුදු සීනි, කි.ග්‍රෑ. 1',
    short_description_ta = 'வெள்ளை சர்க்கரை, 1 கிகி'
WHERE slug = 'white-sugar-1kg';

UPDATE products SET
    name_si = 'රතු පරිප්පු (කි.ග්‍රෑ. 1)',
    name_ta = 'சிவப்பு பருப்பு (1 கிகி)',
    description_si = 'පැලූ රතු පරිප්පු (මයිසූර් දාල්), පිරිසිදු කර වර්ග කළ.',
    description_ta = 'பிளந்த சிவப்பு பருப்பு (மைசூர் பருப்பு), சுத்தம் செய்து தரப்படுத்தப்பட்டது.',
    short_description_si = 'රතු පරිප්පු, කි.ග්‍රෑ. 1',
    short_description_ta = 'சிவப்பு பருப்பு, 1 கிகி'
WHERE slug = 'red-lentils-1kg';

UPDATE products SET
    name_si = 'පොල් තෙල් (මි.ලී. 750)',
    name_ta = 'தேங்காய் எண்ணெய் (750 மிலி)',
    description_si = 'සීතලෙන් මිරිකූ වර්ජින් පොල් තෙල්, මි.ලී. 750 බෝතලයක.',
    description_ta = 'குளிர் பிழிவு வர்ஜின் தேங்காய் எண்ணெய், 750 மிலி பாட்டிலில்.',
    short_description_si = 'වර්ජින් පොල් තෙල්, මි.ලී. 750',
    short_description_ta = 'வர்ஜின் தேங்காய் எண்ணெய், 750 மிலி'
WHERE slug = 'coconut-oil-750ml';

UPDATE products SET
    name_si = 'තුනපහ (ග්‍රෑම් 250)',
    name_ta = 'கறித் தூள் (250 கி)',
    description_si = 'බැදගත් ශ්‍රී ලාංකික තුනපහ, අලුතින් අඹරන ලද.',
    description_ta = 'வறுத்த இலங்கை கறித் தூள், புதிதாக அரைக்கப்பட்டது.',
    short_description_si = 'බැදපු තුනපහ, ග්‍රෑම් 250',
    short_description_ta = 'வறுத்த கறித் தூள், 250 கி'
WHERE slug = 'curry-powder-250g';

UPDATE products SET
    name_si = 'තක්කාලි (කි.ග්‍රෑ. 1)',
    name_ta = 'தக்காளி (1 கிகி)',
    description_si = 'තද සලාද තක්කාලි, වර්ග කර සෝදන ලද.',
    description_ta = 'இறுகிய சாலட் தக்காளி, தரப்படுத்தி கழுவப்பட்டது.',
    short_description_si = 'නැවුම් තක්කාලි, කි.ග්‍රෑ. 1',
    short_description_ta = 'புதிய தக்காளி, 1 கிகி'
WHERE slug = 'tomato-1kg';

UPDATE products SET
    name_si = 'ඇඹුල් කෙසෙල් (කි.ග්‍රෑ. 1)',
    name_ta = 'அம்பல் வாழைப்பழம் (1 கிகி)',
    description_si = 'පැණි-ඇඹුල් රසැති ඇඹුල් කෙසෙල්, ඉදුණු විට කඩා එදිනම ඇසුරුම් කළ.',
    description_ta = 'இனிப்பு-புளிப்பு அம்பல் வாழைப்பழம், பழுத்ததும் பறித்து அன்றே பொதியிடப்பட்டது.',
    short_description_si = 'දේශීය ඇඹුල් කෙසෙල්, කි.ග්‍රෑ. 1',
    short_description_ta = 'உள்ளூர் அம்பல் வாழை, 1 கிகி'
WHERE slug = 'ambul-banana-1kg';

UPDATE products SET
    name_si = 'මී දී (ග්‍රෑම් 400)',
    name_ta = 'எருமைத் தயிர் (400 கி)',
    description_si = 'සම්ප්‍රදායික මැටි වළන්ගෙඩියක තැම්පත් කළ මී දී.',
    description_ta = 'பாரம்பரிய மண் சட்டியில் இறுக்கிய எருமைத் தயிர்.',
    short_description_si = 'මැටි වළන් මී දී, ග්‍රෑම් 400',
    short_description_ta = 'மண் சட்டி எருமைத் தயிர், 400 கி'
WHERE slug = 'buffalo-curd-400g';

UPDATE products SET
    name_si = 'ගොවිපල බිත්තර (10 ක පැකට්)',
    name_ta = 'பண்ணை முட்டை (10 பொதி)',
    description_si = 'නිදහසේ හැසිරෙන කිකිළියන්ගෙන් ලද දුඹුරු බිත්තර, දහයක පෙට්ටියක්.',
    description_ta = 'சுதந்திரமாக மேயும் கோழிகளின் பழுப்பு முட்டை, பத்து கொண்ட பெட்டி.',
    short_description_si = 'නිදහස් ගොවිපල දුඹුරු බිත්තර, 10 ක්',
    short_description_ta = 'பண்ணை பழுப்பு முட்டை, 10'
WHERE slug = 'farm-eggs-10-pack';

UPDATE products SET
    name_si = 'රතු හාල් (කි.ග්‍රෑ. 5)',
    name_ta = 'சிவப்பு பச்சரிசி (5 கிகி)',
    description_si = 'නොපොලිෂ් කළ රතු හාල්, පිරිසිදු කර දෙවරක් පෙරූ.',
    description_ta = 'பாலிஷ் செய்யாத சிவப்பு பச்சரிசி, சுத்தம் செய்து இருமுறை சலிக்கப்பட்டது.',
    short_description_si = 'රතු හාල්, කි.ග්‍රෑ. 5',
    short_description_ta = 'சிவப்பு பச்சரிசி, 5 கிகி'
WHERE slug = 'red-raw-rice-5kg';
