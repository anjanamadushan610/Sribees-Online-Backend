# Sribees WhatsApp support — පළමු අදියර

අංකය **+94 76 700 0836**. Meta dashboard එකේ Registered බව 2026-11-08 දින පරීක්ෂා කර, හිමිකරුද මෙය තහවුරු කළා.

Source commit `4a6e90b7220bd8a924d05fc96b75d8a844c5a693` මත වෙනම copy එකක මෙම implementation එක සකස් කර ඇත.

## Customer flow

1. Customer පළමු message එක එවූ විට welcome menu එක ලැබේ.
2. `SI` / `EN` මගින් භාෂාව තෝරාගත හැකියි. Sinhala message එකක් හඳුනාගත් විටද Sinhala menu එක ලැබේ.
3. Delivery & fees මගින් app එකේ location සහ checkout පරීක්ෂා කිරීමට උපදෙස් ලැබේ. නිශ්චිත නොදන්නා ගාස්තු හෝ වේලාවක් පොරොන්දු නොවේ.
4. Get the app මගින් official website සහ Android app link එක ලැබේ.
5. Customer support තෝරා විස්තර text ලෙස යැවූ විට දැනට ඇති Admin Support Tickets කොටසේ ticket එක සෑදේ. Customerට reference එකක් ලැබේ.
6. එම conversation එකේ පසුව ලැබෙන විස්තර එම pending ticket එකට එකතු වේ. `MENU` යැවීමෙන් නැවත bot menu එක ලැබේ.

මෙම අදියරේ staff ක්‍රියාව Admin Support Tickets හි ticket එක කියවා එහි phone number එකෙන් customer සම්බන්ධ කිරීමයි. Admin dashboard එකෙන් WhatsApp reply ලියන live chat editor එකක් මෙම අදියරේ අඩංගු නොවේ. WhatsApp media files download නොවේ. පාරිභෝගිකයාගෙන් text විස්තර ඉල්ලයි.

## Server configuration

පහත environment variable names server secret settings තුළ සකස් කළ යුතුයි. සැබෑ secret values මෙම document එකේ හෝ Git තුළ ඇතුළත් නොකරන්න.

- `WHATSAPP_ENABLED` මුලින් `false`; configuration/test අවසන් කර `true` කරන්න.
- `WHATSAPP_VERIFY_TOKEN` operator විසින් නිර්මාණය කරන random secret එක. Meta verify field එකට සහ server එකට එකම අගය භාවිත කරන්න.
- `WHATSAPP_APP_SECRET` Meta app secret එක. Incoming POST signature පරීක්ෂාවට භාවිත වේ.
- `WHATSAPP_ACCESS_TOKEN` නිවැරදි WhatsApp account/number එකට send permission ඇති server-side token එක.
- `WHATSAPP_PHONE_NUMBER_ID` පරීක්ෂා කළ dashboard අගය `1251524121387354`.
- `WHATSAPP_GRAPH_VERSION` Meta app dashboard එකේ පෙන්වන supported Graph API version එක, `vNN.N` ආකාරයෙන්. Source එක version අනුමාන කරන්නේ නැහැ.

Meta app ID `2079492732708531`. WhatsApp Business account ID `1997832370874792`.

යෝජිත Callback URL `https://api.sribeesonline.com/api/v1/whatsapp/webhook`.
මෙය deployment අවසන් වන තුරු live endpoint එකක් ලෙස තහවුරු කර නැහැ.

## Deployment සහ Meta සැකසුම්

1. Backend වෙනස්කම් review කර වෙනම branch/PR හරහා අනුමත කරන්න.
2. Migration `105_whatsapp_support.sql` development database එකේ යොදා schema සහ concurrent duplicate webhook tests ධාවනය කරන්න. Migration අංකය වෙනත් team change එකක් සමඟ ගැටේද deploy කිරීමට පෙර පරීක්ෂා කරන්න.
3. Deployment environment එකේ ඉහත settings සකස් කර backend restart කරන්න. Production Compose එක `.env` server file එකෙන් environment ලබාගනී.
4. Public HTTPS endpoint එක සහ verify-token challenge response එක පරීක්ෂා කරන්න.
5. Meta Configure Webhooks තුළ Callback URL සහ verify token දමා Verify and save කරන්න. `messages` webhook field සහ නිවැරදි WABA subscription එක සක්‍රිය කරන්න.
6. Dashboard එකේ ප්‍රකාශිත සීමාව අනුව unpublished Meta app එකට production messages නොලැබේ. Publish requirements සහ business verification අවසන් කරන්න.
7. හිමිකරුගේ වෙනත් WhatsApp number එකෙන් SI, EN, Delivery, Get the app, Support, follow-up සහ MENU පරීක්ෂා කරන්න.
8. Super Admin Support Tickets හි ticket reference සහ phone නිවැරදි බව බලන්න. WhatsApp tickets branch නොතෝරාගත් නිසා branch-specific staffට නොපෙනිය හැකියි.

## Delivery failure හැසිරීම

- Meta signature වැරදි POST requests ප්‍රතික්ෂේප වේ.
- Inbound message ID unique constraint එක නිසා duplicate message එකෙන් tickets/replies දෙවරක් සෑදෙන්නේ නැහැ.
- Tickets සහ reply queue එක එක database transaction එකක save කර පසුව webhook acknowledgment ලබාදේ.
- Queue එක database තුළ පවතී. Server restart එකකින් pending replies මැකෙන්නේ නැහැ.
- සේවකයන් කිහිපයක් ක්‍රියාත්මක වුවත් row locks මගින් එක reply එක එකවර ගන්නේ එක් worker එකක් පමණයි.
- Meta 200 response එක `accepted` ලෙස සටහන් වේ. එය customerට delivered/read වූ බව තහවුරු කිරීමක් නොවේ.
- HTTP 429 සඳහා සීමිත retries ඇත. Timeout/5xx/අතරමග crash වූ send එක `uncertain` ලෙස තබයි. ඒවා ස්වයංක්‍රීයව නැවත යවා duplicate replies ඇති නොකරයි.
- පැය 24 customer-service window එක අවසන් වීමට පෙර safety margin එකකින් pending replies expire වේ. මෙම අදියරේ business-initiated templates යවන්නේ නැහැ.
- `failed` සහ `uncertain` rows පිළිබඳ server log එකේ internal reply ID පමණක් සටහන් වේ. Operator විසින් Meta delivery status පරීක්ෂා කර reconcile කළ යුතුයි.

## Data සහ validation සීමා

Support ticket එකට customer යවන text සහ phone number එක ගබඩා වේ. WhatsApp conversation සහ reply queue එකට phone number, timestamps, message IDs සහ generated reply ගබඩා වේ. වෙනත් customer profile/order විස්තර හෙළි නොවේ. Live කිරීමට පෙර පවතින privacy policy/retention ක්‍රියාවලියට නව WhatsApp tables ඇතුළත් කළ යුතුයි.

Local tests සැබෑ Meta number එකට messages යවන්නේ නැහැ. Signed webhook HTTP tests, menu/state transitions සහ mocked send failure tests භාවිත වේ. PostgreSQL concurrency සහ live delivery වෙනම development/end-to-end පරීක්ෂාවක් අවශ්‍යයි.
