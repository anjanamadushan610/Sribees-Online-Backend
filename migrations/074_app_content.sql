-- =============================================================================
-- Migration 074: words the app shows that change without an app release.
--
-- The owner asked for an "About the developer" page in Profile (Sribees
-- Digital Solutions, the software company of Sribees Lanka (Pvt) Ltd) whose
-- every word comes from the database in English, Sinhala and Tamil, so it can
-- be changed with a query after the app is on the Play Store.
--
-- app_content: one row per page per language; `content` is the page's text as
--   JSON. The app draws whatever sections are there and skips any that are
--   missing, so a section can be removed by deleting its key. The first page
--   is 'developer'. GET /api/v1/app/content/{page}?lang=si serves it.
--
-- Changing it -- any UPDATE works; the trigger stamps updated_at, the backend
-- notices within a minute and sends the silent `sync` push, and open apps
-- re-read the page (app/services/app_content_service.py):
--
--   -- one field, one language
--   UPDATE app_content
--      SET content = jsonb_set(content, '{hero,tagline}', to_jsonb('New tagline'::text))
--    WHERE page = 'developer' AND lang = 'en';
--
--   -- the website link, all three languages
--   UPDATE app_content
--      SET content = jsonb_set(content, '{cta,url}', to_jsonb('https://www.sribeesdigital.com'::text))
--    WHERE page = 'developer';
--
--   -- drop a section (here the numbers strip) in every language
--   UPDATE app_content SET content = content - 'highlights' WHERE page = 'developer';
--
--   -- hide the page (and its Profile row) / show it again
--   UPDATE app_content SET is_active = false WHERE page = 'developer';
--
-- Service icons the app knows: mobile, web, chatbot, ai, design, cloud,
-- analytics, security, code, support (anything else shows a generic icon).
--
-- Idempotent -- the deploy applies this file again on each release. The seed
-- is ON CONFLICT DO NOTHING, so a replay never undoes the owner's edits.
-- =============================================================================

CREATE TABLE IF NOT EXISTS app_content (
    page        VARCHAR(64) NOT NULL,
    lang        VARCHAR(8)  NOT NULL,
    content     JSONB       NOT NULL,
    is_active   BOOLEAN     NOT NULL DEFAULT TRUE,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (page, lang),
    CONSTRAINT ck_app_content_lang CHECK (lang IN ('en', 'si', 'ta'))
);

-- create_all() may have made the table first; its server defaults then match,
-- but the trigger only exists here.
CREATE OR REPLACE FUNCTION app_content_touch()
RETURNS TRIGGER AS $$
BEGIN
    -- clock_timestamp, not now(): two edits in one transaction still differ.
    NEW.updated_at = clock_timestamp();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_app_content_touch ON app_content;
CREATE TRIGGER trg_app_content_touch
    BEFORE INSERT OR UPDATE ON app_content
    FOR EACH ROW EXECUTE FUNCTION app_content_touch();

INSERT INTO app_content (page, lang, content) VALUES
    ('developer', 'en', $page${
  "menu": {
    "title": "About the developer",
    "subtitle": "Built by Sribees Digital Solutions"
  },
  "title": "About the developer",
  "hero": {
    "eyebrow": "SRIBEES LANKA (PVT) LTD COMPANY",
    "name": "Sribees Digital Solutions",
    "tagline": "Software that works as hard as your business."
  },
  "intro": "Sribees Digital Solutions is the software company of Sribees Lanka (Pvt) Ltd. We design and build mobile apps, websites and AI systems — and we built the Sribees Online app you are using right now.",
  "highlights": [
    {
      "value": "3",
      "label": "Languages we build in"
    },
    {
      "value": "24/7",
      "label": "AI that never sleeps"
    },
    {
      "value": "End to end",
      "label": "Idea, design, build, support"
    }
  ],
  "services_title": "What we build",
  "services": [
    {
      "icon": "mobile",
      "title": "Mobile apps",
      "body": "Android and iOS apps, from the first sketch to the Play Store and App Store."
    },
    {
      "icon": "web",
      "title": "Websites & web systems",
      "body": "Company websites, online stores, dashboards and admin panels."
    },
    {
      "icon": "chatbot",
      "title": "AI chatbots",
      "body": "Assistants that answer your customers in Sinhala, Tamil and English, day and night."
    },
    {
      "icon": "ai",
      "title": "AI systems",
      "body": "Automation, recommendations and insights that turn your data into decisions."
    },
    {
      "icon": "design",
      "title": "UI/UX design",
      "body": "Clear, friendly screens designed around the people who use them."
    },
    {
      "icon": "cloud",
      "title": "Cloud & integrations",
      "body": "Payments, couriers, SMS and cloud hosting — connected and looked after."
    }
  ],
  "showcase": {
    "label": "MADE BY US",
    "title": "Sribees Online",
    "body": "Shopping, delivery tracking, cashback and an AI assistant that speaks your language. The app in your hand is designed, built and run by our team."
  },
  "cta": {
    "title": "Have an idea for your business?",
    "body": "Tell us what you need. We will help you turn it into an app, a website or an AI system.",
    "button": "Visit our website",
    "url": "https://www.sribeesdigital.com"
  },
  "footer": "© Sribees Digital Solutions · part of Sribees Lanka (Pvt) Ltd"
}$page$::jsonb),
    ('developer', 'si', $page${
  "menu": {
    "title": "සංවර්ධකයා ගැන",
    "subtitle": "Sribees Digital Solutions විසින් නිර්මාණය කළා"
  },
  "title": "සංවර්ධකයා ගැන",
  "hero": {
    "eyebrow": "SRIBEES LANKA (PVT) LTD සමාගමේ කොටසක්",
    "name": "Sribees Digital Solutions",
    "tagline": "ඔබේ අදහසට, අපේ මෘදුකාංග විසඳුම."
  },
  "intro": "Sribees Digital Solutions යනු Sribees Lanka (Pvt) Ltd හි මෘදුකාංග සමාගමයි. අපි ජංගම යෙදුම්, වෙබ් අඩවි සහ AI පද්ධති සැලසුම් කර නිර්මාණය කරනවා. ඔබ දැන් භාවිතා කරන Sribees Online යෙදුමත් අපේ නිර්මාණයක්.",
  "highlights": [
    {
      "value": "3",
      "label": "අපි නිර්මාණය කරන භාෂා"
    },
    {
      "value": "24/7",
      "label": "නොනවතින AI සේවය"
    },
    {
      "value": "මුල සිට අගට",
      "label": "අදහස, සැලසුම, නිර්මාණය, සහාය"
    }
  ],
  "services_title": "අපි නිර්මාණය කරන දේ",
  "services": [
    {
      "icon": "mobile",
      "title": "ජංගම යෙදුම්",
      "body": "Android සහ iOS යෙදුම්, මුල් අදහසේ සිට Play Store සහ App Store දක්වා."
    },
    {
      "icon": "web",
      "title": "වෙබ් අඩවි සහ වෙබ් පද්ධති",
      "body": "සමාගම් වෙබ් අඩවි, online වෙළඳසැල්, dashboards සහ admin පැනල."
    },
    {
      "icon": "chatbot",
      "title": "AI chatbots",
      "body": "ඔබේ පාරිභෝගිකයන්ට සිංහලෙන්, දෙමළෙන් සහ English වලින් දිවා රෑ පිළිතුරු දෙන සහායකයන්."
    },
    {
      "icon": "ai",
      "title": "AI පද්ධති",
      "body": "ඔබේ දත්ත හොඳ තීරණ බවට පත් කරන automation, recommendations සහ විශ්ලේෂණ."
    },
    {
      "icon": "design",
      "title": "UI/UX සැලසුම්",
      "body": "භාවිතා කරන අයට පහසු, පැහැදිලි තිර සැලසුම්."
    },
    {
      "icon": "cloud",
      "title": "Cloud සහ සම්බන්ධ කිරීම්",
      "body": "ගෙවීම්, courier, SMS සහ cloud hosting එකට සම්බන්ධ කර නඩත්තු කරනවා."
    }
  ],
  "showcase": {
    "label": "අපේ නිර්මාණයක්",
    "title": "Sribees Online",
    "body": "සාප්පු යෑම, delivery ලුහුබැඳීම, cashback සහ ඔබේ භාෂාවෙන් කතා කරන AI සහායකයා. ඔබේ අතේ ඇති මේ යෙදුම සැලසුම් කර, නිර්මාණය කර, පවත්වාගෙන යන්නේ අපේ කණ්ඩායමයි."
  },
  "cta": {
    "title": "ඔබේ ආයතනයට අදහසක් තිබේද?",
    "body": "ඔබට ඕනෑ දේ අපට කියන්න. එය යෙදුමක්, වෙබ් අඩවියක් හෝ AI පද්ධතියක් බවට පත් කරන්න අපි උදව් කරනවා.",
    "button": "අපේ වෙබ් අඩවියට යන්න",
    "url": "https://www.sribeesdigital.com"
  },
  "footer": "© Sribees Digital Solutions · Sribees Lanka (Pvt) Ltd හි කොටසක්"
}$page$::jsonb),
    ('developer', 'ta', $page${
  "menu": {
    "title": "டெவலப்பர் பற்றி",
    "subtitle": "Sribees Digital Solutions உருவாக்கியது"
  },
  "title": "டெவலப்பர் பற்றி",
  "hero": {
    "eyebrow": "SRIBEES LANKA (PVT) LTD நிறுவனத்தின் ஒரு பகுதி",
    "name": "Sribees Digital Solutions",
    "tagline": "உங்கள் யோசனைக்கு, எங்கள் மென்பொருள் தீர்வு."
  },
  "intro": "Sribees Digital Solutions என்பது Sribees Lanka (Pvt) Ltd இன் மென்பொருள் நிறுவனம். நாங்கள் மொபைல் செயலிகள், இணையதளங்கள் மற்றும் AI அமைப்புகளை வடிவமைத்து உருவாக்குகிறோம். நீங்கள் இப்போது பயன்படுத்தும் Sribees Online செயலியும் எங்கள் படைப்பே.",
  "highlights": [
    {
      "value": "3",
      "label": "நாங்கள் உருவாக்கும் மொழிகள்"
    },
    {
      "value": "24/7",
      "label": "ஓய்வில்லாத AI சேவை"
    },
    {
      "value": "முழுமையாக",
      "label": "யோசனை முதல் ஆதரவு வரை"
    }
  ],
  "services_title": "நாங்கள் உருவாக்குபவை",
  "services": [
    {
      "icon": "mobile",
      "title": "மொபைல் செயலிகள்",
      "body": "முதல் யோசனையிலிருந்து Play Store, App Store வரை Android மற்றும் iOS செயலிகள்."
    },
    {
      "icon": "web",
      "title": "இணையதளங்கள் & வலை அமைப்புகள்",
      "body": "நிறுவன இணையதளங்கள், ஆன்லைன் கடைகள், dashboards மற்றும் நிர்வாகப் பலகங்கள்."
    },
    {
      "icon": "chatbot",
      "title": "AI சாட்பாட்கள்",
      "body": "உங்கள் வாடிக்கையாளர்களுக்கு சிங்களம், தமிழ், ஆங்கிலத்தில் இரவும் பகலும் பதிலளிக்கும் உதவியாளர்கள்."
    },
    {
      "icon": "ai",
      "title": "AI அமைப்புகள்",
      "body": "உங்கள் தரவை முடிவுகளாக மாற்றும் தானியக்கம், பரிந்துரைகள் மற்றும் பகுப்பாய்வுகள்."
    },
    {
      "icon": "design",
      "title": "UI/UX வடிவமைப்பு",
      "body": "பயன்படுத்துபவர்களை மையமாகக் கொண்ட தெளிவான, எளிமையான திரைகள்."
    },
    {
      "icon": "cloud",
      "title": "Cloud & ஒருங்கிணைப்புகள்",
      "body": "கட்டணங்கள், கூரியர், SMS மற்றும் cloud hosting — இணைத்து பராமரிக்கிறோம்."
    }
  ],
  "showcase": {
    "label": "எங்கள் படைப்பு",
    "title": "Sribees Online",
    "body": "ஷாப்பிங், டெலிவரி கண்காணிப்பு, கேஷ்பேக் மற்றும் உங்கள் மொழியில் பேசும் AI உதவியாளர். உங்கள் கையில் உள்ள இந்த செயலியை வடிவமைத்து, உருவாக்கி, இயக்குவது எங்கள் குழுவே."
  },
  "cta": {
    "title": "உங்கள் வணிகத்திற்கு ஒரு யோசனை உள்ளதா?",
    "body": "உங்களுக்குத் தேவையானதைச் சொல்லுங்கள். அதை ஒரு செயலி, இணையதளம் அல்லது AI அமைப்பாக மாற்ற நாங்கள் உதவுகிறோம்.",
    "button": "எங்கள் இணையதளத்தைப் பார்வையிடவும்",
    "url": "https://www.sribeesdigital.com"
  },
  "footer": "© Sribees Digital Solutions · Sribees Lanka (Pvt) Ltd இன் ஒரு பகுதி"
}$page$::jsonb)
ON CONFLICT (page, lang) DO NOTHING;
