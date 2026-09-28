-- =============================================================================
-- Migration 075: Update developer page content branding
-- Fix "Digitel" -> "Digital" and "A SRIBEES LANKA (PVT) LTD COMPANY" -> "SRIBEES LANKA (PVT) LTD COMPANY"
-- =============================================================================

UPDATE app_content
SET content = jsonb_set(
    jsonb_set(
        jsonb_set(
            jsonb_set(
                jsonb_set(
                    jsonb_set(
                        content,
                        '{menu,subtitle}',
                        to_jsonb('Built by Sribees Digital Solutions'::text)
                    ),
                    '{hero,eyebrow}',
                    to_jsonb('SRIBEES LANKA (PVT) LTD COMPANY'::text)
                ),
                '{hero,name}',
                to_jsonb('Sribees Digital Solutions'::text)
            ),
            '{intro}',
            to_jsonb('Sribees Digital Solutions is the software company of Sribees Lanka (Pvt) Ltd. We design and build mobile apps, websites and AI systems — and we built the Sribees Online app you are using right now.'::text)
        ),
        '{cta,url}',
        to_jsonb('https://www.sribeesdigital.com'::text)
    ),
    '{footer}',
    to_jsonb('© Sribees Digital Solutions · part of Sribees Lanka (Pvt) Ltd'::text)
)
WHERE page = 'developer' AND lang = 'en';

UPDATE app_content
SET content = jsonb_set(
    jsonb_set(
        jsonb_set(
            jsonb_set(
                jsonb_set(
                    content,
                    '{menu,subtitle}',
                    to_jsonb('Sribees Digital Solutions විසින් නිර්මාණය කළා'::text)
                ),
                '{hero,name}',
                to_jsonb('Sribees Digital Solutions'::text)
            ),
            '{intro}',
            to_jsonb('Sribees Digital Solutions යනු Sribees Lanka (Pvt) Ltd හි මෘදුකාංග සමාගමයි. අපි ජංගම යෙදුම්, වෙබ් අඩවි සහ AI පද්ධති සැලසුම් කර නිර්මාණය කරනවා. ඔබ දැන් භාවිතා කරන Sribees Online යෙදුමත් අපේ නිර්මාණයක්.'::text)
        ),
        '{cta,url}',
        to_jsonb('https://www.sribeesdigital.com'::text)
    ),
    '{footer}',
    to_jsonb('© Sribees Digital Solutions · Sribees Lanka (Pvt) Ltd හි කොටසක්'::text)
)
WHERE page = 'developer' AND lang = 'si';

UPDATE app_content
SET content = jsonb_set(
    jsonb_set(
        jsonb_set(
            jsonb_set(
                jsonb_set(
                    content,
                    '{menu,subtitle}',
                    to_jsonb('Sribees Digital Solutions உருவாக்கியது'::text)
                ),
                '{hero,name}',
                to_jsonb('Sribees Digital Solutions'::text)
            ),
            '{intro}',
            to_jsonb('Sribees Digital Solutions என்பது Sribees Lanka (Pvt) Ltd இன் மென்பொருள் நிறுவனம். நாங்கள் மொபைல் செயலிகள், இணையதளங்கள் மற்றும் AI அமைப்புகளை வடிவமைத்து உருவாக்குகிறோம். நீங்கள் இப்போது பயன்படுத்தும் Sribees Online செயலியும் எங்கள் படைப்பே.'::text)
        ),
        '{cta,url}',
        to_jsonb('https://www.sribeesdigital.com'::text)
    ),
    '{footer}',
    to_jsonb('© Sribees Digital Solutions · Sribees Lanka (Pvt) Ltd இன் ஒரு பகுதி'::text)
)
WHERE page = 'developer' AND lang = 'ta';
