"""Customer-facing WhatsApp copy and bounded interactive menus."""

LANGUAGES = ("en", "si", "ta")
COPY = {
    "welcome": (
        "Sribees Online 🛒\n\nWelcome to Sribees Online Customer Support!\n\nWe're happy to assist you. Please select your preferred language to continue.",
        "Sribees Online 🛒\n\nSribees Online පාරිභෝගික සහාය සේවාව වෙත සාදරයෙන් පිළිගනිමු!\n\nඔබට සහාය වීමට අපි සූදානම්. ඉදිරියට යාම සඳහා ඔබ කැමති භාෂාව තෝරන්න.",
        "Sribees Online 🛒\n\nSribees Online வாடிக்கையாளர் ஆதரவு சேவைக்கு உங்களை அன்புடன் வரவேற்கிறோம்!\n\nஉங்களுக்கு உதவுவதில் மகிழ்ச்சி. தொடர்வதற்கு உங்களுக்கு விருப்பமான மொழியைத் தேர்ந்தெடுக்கவும்.",
    ),
    "main": (
        "Sribees Online Customer Support 🛒\n\nHello! How can we help you today? 😊\n\nPlease select a service below.",
        "Sribees Online පාරිභෝගික සහාය 🛒\n\nආයුබෝවන්! අද අපට ඔබට සහාය විය හැක්කේ කෙසේද? 😊\n\nඅවශ්‍ය සේවාව තෝරන්න.",
        "Sribees Online வாடிக்கையாளர் ஆதரவு 🛒\n\nவணக்கம்! இன்று நாங்கள் உங்களுக்கு எவ்வாறு உதவ முடியும்? 😊\n\nதேவையான சேவையைத் தேர்ந்தெடுக்கவும்.",
    ),
    "choose": (
        "Choose an option below.",
        "අවශ්‍ය විකල්පය තෝරන්න.",
        "ஒரு விருப்பத்தைத் தேர்ந்தெடுக்கவும்.",
    ),
    "options": ("Options", "විකල්ප", "விருப்பங்கள்"),
    "nav": (
        "Send MENU for the main menu, BACK to go back, LANGUAGE to change language, or CANCEL to cancel this request.",
        "ප්‍රධාන මෙනුවට MENU, ආපසු යාමට BACK, භාෂාව වෙනස් කිරීමට LANGUAGE හෝ මෙම විමසීම නවත්වන්න CANCEL යවන්න.",
        "முதன்மை பட்டியலுக்கு MENU, பின்னால் செல்ல BACK, மொழியை மாற்ற LANGUAGE அல்லது விசாரணையை நிறுத்த CANCEL அனுப்பவும்.",
    ),
    "order_prompt": (
        "Please enter your Order Number to check the latest status of your order.",
        "ඔබගේ ඇණවුමේ නවතම තත්ත්වය පරීක්ෂා කිරීම සඳහා Order Number එක ඇතුළත් කරන්න.",
        "உங்கள் ஆர்டரின் தற்போதைய நிலையைச் சரிபார்க்க உங்கள் ஆர்டர் எண்ணை உள்ளிடவும்.",
    ),
    "order_missing": (
        "We couldn't verify an order with these details and your WhatsApp number. Check your Order Number or choose Customer Support from MENU.",
        "ඔබගේ WhatsApp අංකයට සහ මෙම තොරතුරුවලට අදාළ ඇණවුමක් තහවුරු කළ නොහැකි විය. Order Number එක පරීක්ෂා කරන්න හෝ MENU වෙතින් පාරිභෝගික සහාය තෝරන්න.",
        "இந்த விவரங்களும் உங்கள் WhatsApp எண்ணும் பொருந்தும் ஆர்டரைச் சரிபார்க்க முடியவில்லை. ஆர்டர் எண்ணைச் சரிபார்க்கவும் அல்லது MENU மூலம் ஆதரவைக் கோரவும்.",
    ),
    "details_prompt": (
        "Please describe the issue briefly. Do not send passwords, OTPs, card numbers, CVV, PINs or banking credentials.",
        "ගැටලුව කෙටියෙන් විස්තර කරන්න. Password, OTP, card number, CVV, PIN හෝ බැංකු පිවිසුම් තොරතුරු එවන්න එපා.",
        "சிக்கலைச் சுருக்கமாக விளக்கவும். கடவுச்சொல், OTP, அட்டை எண், CVV, PIN அல்லது வங்கி உள்நுழைவு விவரங்களை அனுப்ப வேண்டாம்.",
    ),
    "agent_request": (
        "Customer requested a support agent.",
        "පාරිභෝගිකයා සේවකයෙකුගේ සහාය ඉල්ලා ඇත.",
        "வாடிக்கையாளர் ஆதரவுப் பணியாளரைக் கோரியுள்ளார்.",
    ),
    "product_prompt": (
        "Enter the affected product name.",
        "අදාළ භාණ්ඩයේ නම ඇතුළත් කරන්න.",
        "பாதிக்கப்பட்ட பொருளின் பெயரை உள்ளிடவும்.",
    ),
    "quantity_prompt": (
        "Enter the affected quantity (1–999).",
        "අදාළ ප්‍රමාණය ඇතුළත් කරන්න (1–999).",
        "பாதிக்கப்பட்ட எண்ணிக்கையை உள்ளிடவும் (1–999).",
    ),
    "optional_order_prompt": (
        "Enter your Order Number. If no order was created or the number is unavailable, send SKIP.",
        "ඔබගේ Order Number එක ඇතුළත් කරන්න. ඇණවුමක් සෑදී නැති නම් හෝ අංකය නොමැති නම් SKIP යවන්න.",
        "உங்கள் ஆர்டர் எண்ணை உள்ளிடவும். ஆர்டர் உருவாகவில்லை அல்லது எண் கிடைக்கவில்லை என்றால் SKIP அனுப்பவும்.",
    ),
    "method_prompt": (
        "Enter the payment method you used, or SKIP if unknown.",
        "භාවිත කළ ගෙවීම් ක්‍රමය ඇතුළත් කරන්න. නොදන්නේ නම් SKIP යවන්න.",
        "பயன்படுத்திய கட்டண முறையை உள்ளிடவும். தெரியாவிட்டால் SKIP அனுப்பவும்.",
    ),
    "reference_prompt": (
        "Enter the transaction reference, or SKIP if unavailable. Do not send card or banking credentials.",
        "Transaction reference එක ඇතුළත් කරන්න. නොමැති නම් SKIP යවන්න. Card හෝ බැංකු පිවිසුම් තොරතුරු එවන්න එපා.",
        "பரிவர்த்தனை குறிப்பை உள்ளிடவும். இல்லையெனில் SKIP அனுப்பவும். அட்டை அல்லது வங்கி உள்நுழைவு விவரங்களை அனுப்ப வேண்டாம்.",
    ),
    "photo_prompt": (
        "Send a product photo or a payment receipt with sensitive details covered. Send SKIP to submit without a photo.",
        "භාණ්ඩයේ ඡායාරූපයක් හෝ රහස්‍ය තොරතුරු ආවරණය කළ ගෙවීම් රිසිට්පතක් එවන්න. ඡායාරූපයක් නැතිව යැවීමට SKIP යවන්න.",
        "பொருளின் படம் அல்லது முக்கிய விவரங்கள் மறைக்கப்பட்ட கட்டண ரசீதை அனுப்பவும். படம் இல்லாமல் சமர்ப்பிக்க SKIP அனுப்பவும்.",
    ),
    "invalid": (
        "Please use the available options or enter the requested details.",
        "පෙන්වා ඇති විකල්පයක් තෝරන්න හෝ ඉල්ලූ තොරතුරු ඇතුළත් කරන්න.",
        "கிடைக்கும் விருப்பத்தைத் தேர்ந்தெடுக்கவும் அல்லது கோரிய விவரங்களை உள்ளிடவும்.",
    ),
    "error": (
        "We're currently unable to process your request. Please try again shortly or contact support@sribeesonline.com.",
        "දැනට ඔබගේ ඉල්ලීම ක්‍රියාත්මක කළ නොහැකියි. මඳ වේලාවකින් නැවත උත්සාහ කරන්න හෝ support@sribeesonline.com අමතන්න.",
        "தற்போது உங்கள் கோரிக்கையைச் செயல்படுத்த முடியவில்லை. சிறிது நேரம் கழித்து முயற்சிக்கவும் அல்லது support@sribeesonline.com ஐத் தொடர்புகொள்ளவும்.",
    ),
    "ticket_created": (
        "Your support request has been successfully submitted! ✅\n\nTicket Number: {number}\n\nOur Customer Support team will review your request and assist you as soon as possible.",
        "ඔබගේ සහාය ඉල්ලීම සාර්ථකව අප වෙත ලැබී ඇත! ✅\n\nයොමු අංකය: {number}\n\nඅපගේ පාරිභෝගික සහාය කණ්ඩායම ඔබගේ ඉල්ලීම පරීක්ෂා කර හැකි ඉක්මනින් සහාය ලබාදෙනු ඇත.",
        "உங்கள் ஆதரவு கோரிக்கை வெற்றிகரமாகச் சமர்ப்பிக்கப்பட்டுள்ளது! ✅\n\nகோரிக்கை எண்: {number}\n\nஎங்கள் வாடிக்கையாளர் ஆதரவுக் குழு உங்கள் கோரிக்கையைப் பரிசீலித்து விரைவில் உதவும்.",
    ),
    "ticket_prompt": (
        "Enter your Support Ticket Number.",
        "ඔබගේ සහාය යොමු අංකය ඇතුළත් කරන්න.",
        "உங்கள் ஆதரவு கோரிக்கை எண்ணை உள்ளிடவும்.",
    ),
    "ticket_missing": (
        "We couldn't verify this ticket with your WhatsApp number. Check the reference or contact Customer Support.",
        "ඔබගේ WhatsApp අංකයෙන් මෙම සහාය ඉල්ලීම තහවුරු කළ නොහැකියි. යොමු අංකය පරීක්ෂා කරන්න හෝ පාරිභෝගික සහාය අමතන්න.",
        "உங்கள் WhatsApp எண்ணுடன் இந்தக் கோரிக்கையைச் சரிபார்க்க முடியவில்லை. எண்ணைச் சரிபார்க்கவும் அல்லது ஆதரவுக் குழுவை அணுகவும்.",
    ),
    "download": (
        "Download Sribees Online for Android:\nhttps://play.google.com/store/apps/details?id=com.sribeesonline.sribees_mobile\nWebsite: https://www.sribeesonline.com",
        "Android සඳහා Sribees Online ලබාගන්න:\nhttps://play.google.com/store/apps/details?id=com.sribeesonline.sribees_mobile\nවෙබ් අඩවිය: https://www.sribeesonline.com",
        "Android இல் Sribees Online பதிவிறக்கம்:\nhttps://play.google.com/store/apps/details?id=com.sribeesonline.sribees_mobile\nஇணையதளம்: https://www.sribeesonline.com",
    ),
    "registration": (
        "Open the app and choose phone sign-in, Google or Apple. For phone sign-in, enter your number and verify the code in the app. Complete the requested profile and delivery address.",
        "App එක විවෘත කර දුරකථන අංකය, Google හෝ Apple මගින් පිවිසෙන්න. දුරකථන අංකයට ලැබෙන code එක app එක තුළ ඇතුළත් කරන්න. ඉල්ලූ ගිණුම් සහ ලිපින තොරතුරු සම්පූර්ණ කරන්න.",
        "செயலியைத் திறந்து தொலைபேசி, Google அல்லது Apple வழியாக உள்நுழையவும். தொலைபேசிக்குக் கிடைக்கும் குறியீட்டைச் செயலியில் உள்ளிடவும். கோரப்படும் சுயவிவரம் மற்றும் விநியோக முகவரியை நிரப்பவும்.",
    ),
    "otp": (
        "Check your phone number and +94 country code, mobile signal and blocked SMS. Wait before trying again, then use Resend in the app. Never share your OTP. If the issue continues, choose Customer Support from MENU.",
        "දුරකථන අංකය, +94 රටේ කේතය, signal සහ අවහිර කළ SMS පරීක්ෂා කරන්න. ටික වේලාවක් රැඳී app එකේ නැවත code යැවීම භාවිත කරන්න. OTP කිසිවෙකුට ලබා නොදෙන්න. ගැටලුව පවතී නම් MENU වෙතින් පාරිභෝගික සහාය තෝරන්න.",
        "தொலைபேசி எண், +94 நாட்டுக் குறியீடு, சிக்னல் மற்றும் தடுக்கப்பட்ட SMS ஐச் சரிபார்க்கவும். சிறிது நேரம் காத்திருந்து செயலியில் மீண்டும் அனுப்பும் வசதியைப் பயன்படுத்தவும். OTP ஐப் பகிர வேண்டாம். சிக்கல் தொடர்ந்தால் MENU மூலம் ஆதரவைக் கோரவும்.",
    ),
    "password": (
        "For phone sign-in use the verification code in the app. For Google or Apple, recover access through that provider's official account recovery. Never share your password or code with support.",
        "දුරකථන අංකයෙන් පිවිසීමට app එකේ verification code භාවිත කරන්න. Google හෝ Apple ගිණුමකට පිවිසිය නොහැකි නම් එහි නිල account recovery භාවිත කරන්න. Password හෝ code සහාය සේවයට එවන්න එපා.",
        "தொலைபேசி உள்நுழைவிற்கு செயலியில் சரிபார்ப்பு குறியீட்டைப் பயன்படுத்தவும். Google அல்லது Apple அணுகலை அந்த சேவையின் அதிகாரப்பூர்வ மீட்பு வழியில் பெறவும். கடவுச்சொல் அல்லது குறியீட்டை ஆதரவிற்கு அனுப்ப வேண்டாம்.",
    ),
    "profile": (
        "Open Profile / My Account in the app to update your details. Phone changes require the app's verification process.",
        "තොරතුරු වෙනස් කිරීමට app එකේ Profile / My Account වෙත යන්න. දුරකථන අංකය වෙනස් කිරීමට app එකේ තහවුරු කිරීම අවශ්‍යයි.",
        "விவரங்களை மாற்ற செயலியில் Profile / My Account செல்லவும். தொலைபேசி மாற்றத்திற்கு செயலியின் சரிபார்ப்பு தேவை.",
    ),
    "deletion": (
        "In the app open Profile > Settings > Delete Account and complete SMS verification. Request information: https://api.sribeesonline.com/account-deletion",
        "App එකේ Profile > Settings > Delete Account වෙත ගොස් SMS තහවුරු කිරීම සම්පූර්ණ කරන්න. වැඩි විස්තර: https://api.sribeesonline.com/account-deletion",
        "செயலியில் Profile > Settings > Delete Account சென்று SMS சரிபார்ப்பை முடிக்கவும். விவரங்கள்: https://api.sribeesonline.com/account-deletion",
    ),
    "cancel_blocked": (
        "This order cannot be cancelled in its current state. Choose an order inquiry for help with delivered items.",
        "දැනට පවතින තත්ත්වයේ මෙම ඇණවුම අවලංගු කළ නොහැකියි. ලැබුණු භාණ්ඩ පිළිබඳ සහායට ඇණවුම් විමසීමක් තෝරන්න.",
        "தற்போதைய நிலையில் இந்த ஆர்டரை ரத்து செய்ய முடியாது. விநியோகிக்கப்பட்ட பொருட்களுக்கான உதவிக்கு ஆர்டர் விசாரணையைத் தேர்ந்தெடுக்கவும்.",
    ),
    "cancel_confirm": (
        "Cancel order {order}? Any refundable amount is credited to Sribees Wallet under the existing cancellation rules. Confirm below.",
        "{order} ඇණවුම අවලංගු කරන්නද? පවතින කොන්දේසි අනුව ආපසු ලැබිය යුතු මුදල් Sribees Wallet එකට බැර වේ. පහතින් තහවුරු කරන්න.",
        "{order} ஆர்டரை ரத்து செய்யவா? விதிமுறைகளின்படி திரும்பப் பெறத்தக்க தொகை Sribees Wallet இல் வரவு வைக்கப்படும். கீழே உறுதிப்படுத்தவும்.",
    ),
    "cancel_done": (
        "Order {order} has been cancelled successfully.",
        "{order} ඇණවුම සාර්ථකව අවලංගු කළා.",
        "{order} ஆர்டர் வெற்றிகரமாக ரத்து செய்யப்பட்டது.",
    ),
    "cancel_request": (
        "Cancellation may involve delivery charges and a wallet refund. We will submit a request for staff to check these terms with you. Describe your reason.",
        "අවලංගු කිරීමේදී බෙදාහැරීමේ ගාස්තු සහ wallet මුදල් ආපසු බැර කිරීම අදාළ විය හැකියි. සේවකයෙකු ඔබ සමඟ කොන්දේසි තහවුරු කිරීමට ඉල්ලීම යොමු කරමු. හේතුව සඳහන් කරන්න.",
        "ரத்து செய்வதில் விநியோக கட்டணம் மற்றும் பணப்பை மீள்செலுத்தல் பொருந்தலாம். விதிமுறைகளை உங்களுடன் உறுதிப்படுத்த ஊழியருக்கு கோரிக்கை அனுப்பப்படும். காரணத்தை விளக்கவும்.",
    ),
    "wallet": (
        "Sribees Wallet balance: LKR {balance}\nCashback depends on the product and current conditions shown in the app. Wallet funds are used for shopping.",
        "Sribees Wallet ශේෂය: LKR {balance}\nCashback භාණ්ඩය සහ app එකේ දැක්වෙන වත්මන් කොන්දේසි අනුව වෙනස් වේ. Wallet මුදල් භාණ්ඩ මිලදී ගැනීමට භාවිත කළ හැකියි.",
        "Sribees Wallet இருப்பு: LKR {balance}\nCashback பொருள் மற்றும் செயலியில் காட்டப்படும் தற்போதைய நிபந்தனைகளைப் பொறுத்தது. பணப்பை தொகையை வாங்குவதற்குப் பயன்படுத்தலாம்.",
    ),
    "identity": (
        "We couldn't securely match your WhatsApp number to a verified active customer account. Choose Customer Support from MENU.",
        "ඔබගේ WhatsApp අංකය තහවුරු කළ සක්‍රිය පාරිභෝගික ගිණුමකට සම්බන්ධ කළ නොහැකියි. MENU වෙතින් පාරිභෝගික සහාය තෝරන්න.",
        "உங்கள் WhatsApp எண்ணைச் சரிபார்க்கப்பட்ட செயலில் உள்ள வாடிக்கையாளர் கணக்குடன் இணைக்க முடியவில்லை. MENU மூலம் ஆதரவைக் கோரவும்.",
    ),
    "offers": (
        "Current offers are shown in the app's Offers and Quick Sale sections for your selected delivery area. Conditions and availability are checked at checkout.",
        "ඔබ තෝරාගත් බෙදාහැරීමේ ප්‍රදේශයට අදාළ වත්මන් දීමනා app එකේ Offers සහ Quick Sale කොටස්වල බලන්න. කොන්දේසි සහ සුදුසුකම checkout එකේ පරීක්ෂා වේ.",
        "தேர்ந்தெடுத்த விநியோகப் பகுதிக்கான தற்போதைய சலுகைகளை செயலியின் Offers மற்றும் Quick Sale பகுதியில் பார்க்கவும். நிபந்தனைகளும் தகுதியும் checkout இல் சரிபார்க்கப்படும்.",
    ),
    "no_grant": (
        "There is no active personal free-delivery grant on your account. Branch and referral offers may apply subject to location, order value and checkout conditions.",
        "ඔබගේ ගිණුමට සක්‍රිය පුද්ගලික නොමිලේ බෙදාහැරීමේ දීමනාවක් නැහැ. ශාඛා සහ referral දීමනා ලිපිනය, ඇණවුම් වටිනාකම සහ checkout කොන්දේසි අනුව අදාළ විය හැකියි.",
        "உங்கள் கணக்கில் செயலில் உள்ள தனிப்பட்ட இலவச விநியோக ஒதுக்கீடு இல்லை. கிளை மற்றும் பரிந்துரை சலுகைகள் முகவரி, ஆர்டர் மதிப்பு மற்றும் checkout நிபந்தனைகளுக்கு உட்பட்டிருக்கலாம்.",
    ),
    "grant": (
        "Personal free delivery until {date}. Minimum order LKR {minimum}. Branch restrictions apply where configured. Checkout confirms eligibility.",
        "පුද්ගලික නොමිලේ බෙදාහැරීම {date} දක්වා. අවම ඇණවුම LKR {minimum}. අදාළ ශාඛා සීමා තිබිය හැකියි. සුදුසුකම checkout එකේ තහවුරු වේ.",
        "தனிப்பட்ட இலவச விநியோகம் {date} வரை. குறைந்தபட்ச ஆர்டர் LKR {minimum}. பொருந்தும் கிளை வரம்புகள் உண்டு. தகுதி checkout இல் உறுதிப்படுத்தப்படும்.",
    ),
    "thank_you": (
        "Thank you for contacting Sribees Online! ❤️",
        "Sribees Online සමඟ සම්බන්ධ වීම පිළිබඳව ස්තූතියි! ❤️",
        "Sribees Online-ஐ தொடர்புகொண்டதற்கு நன்றி! ❤️",
    ),
    "media_failed": (
        "We couldn't open that attachment. Please resend a JPEG/PNG image or describe the issue to your support agent.",
        "එම ගොනුව විවෘත කළ නොහැකි විය. JPEG/PNG ඡායාරූපයක් නැවත එවන්න හෝ ගැටලුව සේවකයාට විස්තර කරන්න.",
        "அந்த இணைப்பைத் திறக்க முடியவில்லை. JPEG/PNG படத்தை மீண்டும் அனுப்பவும் அல்லது சிக்கலை ஆதரவுப் பணியாளரிடம் விளக்கவும்.",
    ),
}

# Row descriptions retain full wording when translated titles exceed Meta's 24-character limit.
LABELS = {
    "orders": ("Orders & Delivery", "ඇණවුම් සහ බෙදාහැරීම්", "ஆர்டர்கள், விநியோகம்"),
    "account": ("App & Account Help", "App සහ ගිණුම් සහාය", "செயலி, கணக்கு உதவி"),
    "payments": (
        "Payments & Refunds",
        "ගෙවීම් සහ මුදල් ආපසු",
        "கட்டணம், மீள்செலுத்தல்",
    ),
    "offers": ("Offers & Promotions", "දීමනා සහ ප්‍රවර්ධන", "சலுகைகள்"),
    "support": ("Customer Support", "පාරිභෝගික සහාය", "வாடிக்கையாளர் ஆதரவு"),
    "language": ("Change Language", "භාෂාව වෙනස් කරන්න", "மொழியை மாற்றவும்"),
    "main": ("Main Menu", "ප්‍රධාන මෙනුව", "முதன்மை பட்டியல்"),
    "track": ("Track My Order", "ඇණවුමේ තත්ත්වය", "ஆர்டரின் நிலை"),
    "delivery": ("Delivery Issues", "බෙදාහැරීමේ ගැටලු", "விநியோக சிக்கல்கள்"),
    "cancel_order": ("Cancel My Order", "ඇණවුම අවලංගු කිරීම", "ஆர்டரை ரத்து செய்"),
    "items": ("Item Issues", "භාණ්ඩ පිළිබඳ ගැටලු", "பொருள் சிக்கல்கள்"),
    "order_other": (
        "Other Order Inquiries",
        "වෙනත් ඇණවුම් විමසීම්",
        "பிற ஆர்டர் விசாரணை",
    ),
    "delayed": ("Order delayed", "ඇණවුම ප්‍රමාදයි", "ஆர்டர் தாமதம்"),
    "not_received": ("Order not received", "ඇණවුම ලැබී නැහැ", "ஆர்டர் கிடைக்கவில்லை"),
    "contact": (
        "Rider couldn't contact",
        "සේවකයාට ඇමතිය නොහැක",
        "தொடர்புகொள்ள இயலவில்லை",
    ),
    "address": ("Address problem", "ලිපිනයේ ගැටලුවක්", "முகவரி சிக்கல்"),
    "delivery_other": (
        "Other delivery issue",
        "වෙනත් බෙදාහැරීම් ගැටලු",
        "பிற விநியோக சிக்கல்",
    ),
    "missing": ("Missing item", "භාණ්ඩයක් ලැබී නැහැ", "பொருள் கிடைக்கவில்லை"),
    "wrong": ("Wrong item", "වැරදි භාණ්ඩයක්", "தவறான பொருள்"),
    "damaged": ("Damaged item", "හානි වූ භාණ්ඩයක්", "சேதமடைந்த பொருள்"),
    "quality": ("Product quality", "ගුණාත්මකභාවයේ ගැටලු", "தர சிக்கல்"),
    "expired": ("Expired product", "කල් ඉකුත් වූ භාණ්ඩයක්", "காலாவதியான பொருள்"),
    "download": ("Download / Install App", "App එක ලබාගන්න", "செயலியைப் பதிவிறக்கு"),
    "registration": ("Registration Help", "ලියාපදිංචි වීම", "பதிவு உதவி"),
    "login": ("Login Problems", "පිවිසීමේ ගැටලු", "உள்நுழைவு சிக்கல்"),
    "otp": ("OTP Not Received", "OTP නොලැබීම", "OTP கிடைக்கவில்லை"),
    "password": ("Forgot Password", "Password අමතකයි", "கடவுச்சொல் மீட்பு"),
    "profile": (
        "Update Account Details",
        "ගිණුම් තොරතුරු වෙනස් කිරීම",
        "கணக்கு விவர மாற்றம்",
    ),
    "deletion": ("Account Deletion", "ගිණුම මකා දැමීම", "கணக்கை நீக்குதல்"),
    "payment_failed": (
        "Online Payment Failed",
        "Online ගෙවීම අසාර්ථකයි",
        "கட்டணம் தோல்வி",
    ),
    "deducted": (
        "Debited, no order",
        "මුදල් අඩුවී ඇණවුම නැහැ",
        "பணம் கழிந்து ஆர்டர் இல்லை",
    ),
    "bank": ("Bank Transfer / QR", "Bank Transfer / QR", "வங்கி பரிமாற்றம் / QR"),
    "refund": ("Refund Inquiry", "මුදල් ආපසු විමසීම්", "பணமீட்பு விசாரணை"),
    "confirmation": (
        "Payment Confirmation",
        "ගෙවීම තහවුරු කිරීම",
        "கட்டண உறுதிப்படுத்தல்",
    ),
    "payment_other": (
        "Other Payment Issues",
        "වෙනත් ගෙවීම් ගැටලු",
        "பிற கட்டண சிக்கல்கள்",
    ),
    "current_offers": ("Current Offers", "වත්මන් දීමනා", "தற்போதைய சலுகைகள்"),
    "free_delivery": ("Free Delivery Benefits", "නොමිලේ බෙදාහැරීම", "இலவச விநியோகம்"),
    "wallet": ("Cashback & Wallet", "Cashback සහ Wallet", "Cashback, பணப்பை"),
    "campaigns": (
        "Promotional Campaigns",
        "ප්‍රවර්ධන වැඩසටහන්",
        "விளம்பரத் திட்டங்கள்",
    ),
    "offer_other": (
        "Other Offers Inquiries",
        "වෙනත් දීමනා විමසීම්",
        "பிற சலுகை விசாரணை",
    ),
    "agent": ("Talk to a Support Agent", "සේවකයෙකු සමඟ කතා කිරීම", "ஆதரவு பணியாளர்"),
    "new_inquiry": ("Submit a New Inquiry", "නව විමසීමක්", "புதிய விசாரணை"),
    "ticket": ("Check My Support Ticket", "සහාය ඉල්ලීමේ තත්ත්වය", "கோரிக்கையின் நிலை"),
}
MENUS = {
    "main": ["orders", "account", "payments", "offers", "support", "language"],
    "orders": ["track", "delivery", "cancel_order", "items", "order_other", "main"],
    "delivery": [
        "delayed",
        "not_received",
        "contact",
        "address",
        "delivery_other",
        "main",
    ],
    "items": ["missing", "wrong", "damaged", "quality", "expired", "main"],
    "account": [
        "download",
        "registration",
        "login",
        "otp",
        "password",
        "profile",
        "deletion",
        "main",
    ],
    "payments": [
        "payment_failed",
        "deducted",
        "bank",
        "refund",
        "confirmation",
        "payment_other",
        "main",
    ],
    "offers": [
        "current_offers",
        "free_delivery",
        "wallet",
        "campaigns",
        "offer_other",
        "main",
    ],
    "support": ["agent", "new_inquiry", "ticket", "main"],
}


def translated(values, language):
    return values[LANGUAGES.index(language if language in LANGUAGES else "en")]


def copy(key, language, **values):
    return translated(COPY[key], language).format(**values)


def text_message(key, language, **values):
    from app.services.whatsapp_menu import text_reply

    return text_reply(copy(key, language, **values) + "\n\n" + copy("nav", language))


def language_menu(language="en"):
    return {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": copy("welcome", language)},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "lang_" + code, "title": title}}
                    for code, title in (
                        ("en", "English"),
                        ("si", "සිංහල"),
                        ("ta", "தமிழ்"),
                    )
                ]
            },
        },
    }


def support_menu(name, language):
    rows = []
    for key in MENUS[name]:
        title = translated(LABELS[key], language)
        row = {"id": key, "title": title[:24]}
        if len(title) > 24:
            row["description"] = title[:72]
        rows.append(row)
    body = (
        copy("main", language)
        if name == "main"
        else translated(LABELS[name], language) + "\n\n" + copy("choose", language)
    )
    return {
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body},
            "footer": {"text": "MENU · BACK · LANGUAGE · CANCEL"},
            "action": {
                "button": copy("options", language),
                "sections": [{"rows": rows}],
            },
        },
    }
