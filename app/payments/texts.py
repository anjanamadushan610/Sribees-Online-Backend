"""
What the customer reads about an order's payment, in en / si / ta.

The app shows these as given. Order status labels live here too, so a status
the app has never heard of (``awaiting_payment``, and whatever comes next)
still reads properly on a phone that has not been updated.
"""
from __future__ import annotations

from app.utils.i18n import DEFAULT_LANGUAGE

STATUS_LABELS: dict[str, dict[str, str]] = {
    "awaiting_payment": {"en": "Payment processing", "si": "ගෙවීම සකසමින්", "ta": "கட்டணம் செயலாக்கத்தில்"},
    "pending": {"en": "Pending", "si": "සකසමින් පවතී", "ta": "நிலுவையில் உள்ளது"},
    "confirmed": {"en": "Confirmed", "si": "තහවුරු කරන ලදී", "ta": "உறுதிப்படுத்தப்பட்டது"},
    "processing": {"en": "Preparing", "si": "ඇසුරුම් කරමින්", "ta": "தயாராகிறது"},
    "packing": {"en": "Preparing", "si": "ඇසුරුම් කරමින්", "ta": "தயாராகிறது"},
    "packed": {"en": "Packed", "si": "ඇසුරුම් කරන ලදී", "ta": "பொதி செய்யப்பட்டது"},
    "ready_for_pickup": {"en": "Waiting for Courier", "si": "කුරියර් වෙත ලබාදීමට සූදානම්", "ta": "கூரியருக்காக காத்திருக்கிறது"},
    "handed_to_courier": {"en": "Handed to Courier", "si": "කුරියර් වෙත භාර දෙන ලදී", "ta": "கூரியரிடம் ஒப்படைக்கப்பட்டது"},
    "shipped": {"en": "Shipped", "si": "බෙදාහැරීමට යවා ඇත", "ta": "அனுப்பப்பட்டது"},
    "out_for_delivery": {"en": "Out for Delivery", "si": "ලඟදීම ඔබට ලැබෙනු ඇත", "ta": "டெலிவரிக்கு வருகிறது"},
    "delivered": {"en": "Delivered", "si": "භාර දෙන ලදී", "ta": "வழங்கப்பட்டது"},
    "cancelled": {"en": "Cancelled", "si": "අවලංගු කරන ලදී", "ta": "ரத்து செய்யப்பட்டது"},
    "return_requested": {"en": "Return Requested", "si": "ආපසු ලබාදීමට ඉල්ලා ඇත", "ta": "திரும்பக் கோரப்பட்டது"},
    "return_approved": {"en": "Return Approved", "si": "ආපසු ගැනීම අනුමත විය", "ta": "திரும்பப் பெற அனுமதி"},
    "refunded": {"en": "Refunded", "si": "මුදල් ආපසු ගෙවන ලදී", "ta": "பணம் திருப்பியளிக்கப்பட்டது"},
    "delivery_failed": {"en": "Delivery Failed", "si": "බෙදාහැරීම අසාර්ථක විය", "ta": "டெலிவரி தோல்வியடைந்தது"},
    "rto_initiated": {"en": "Returning to Store", "si": "නැවත ගබඩාව වෙත", "ta": "திரும்பி வருகிறது"},
    # Not an order status: how an awaiting order reads once a try was declined.
    "payment_failed": {"en": "Payment failed", "si": "ගෙවීම අසාර්ථකයි", "ta": "கட்டணம் தோல்வியடைந்தது"},
}

TEXTS: dict[str, dict[str, str]] = {
    "processing.message": {
        "en": "We'll place your order as soon as the payment is confirmed.",
        "si": "ගෙවීම තහවුරු වූ විගස ඔබේ ඇණවුම යොමු කෙරේ.",
        "ta": "கட்டணம் உறுதிசெய்யப்பட்டதும் உங்கள் ஆர்டர் பதிவு செய்யப்படும்.",
    },
    "processing.payBy": {
        "en": "Didn't finish paying? Pay by {time} to keep your items.",
        "si": "ගෙවීම අවසන් කළේ නැද්ද? ඔබේ භාණ්ඩ රඳවා ගැනීමට {time} ට පෙර ගෙවන්න.",
        "ta": "செலுத்தி முடிக்கவில்லையா? உங்கள் பொருட்களை வைத்திருக்க {time} க்குள் செலுத்துங்கள்.",
    },
    "failed.message": {
        "en": "Your payment didn't go through, so the order isn't placed yet. You can try again until {time}.",
        "si": "ඔබේ ගෙවීම සාර්ථක නොවූ නිසා ඇණවුම තවම යොමු කර නැත. {time} දක්වා නැවත උත්සාහ කළ හැක.",
        "ta": "உங்கள் கட்டணம் செல்லவில்லை, எனவே ஆர்டர் இன்னும் பதிவு செய்யப்படவில்லை. {time} வரை மீண்டும் முயற்சிக்கலாம்.",
    },
    "notCompleted.title": {
        "en": "Payment not completed",
        "si": "ගෙවීම සම්පූර්ණ නොවීය",
        "ta": "கட்டணம் முடிக்கப்படவில்லை",
    },
    "notCompleted.message": {
        "en": "This order was cancelled because the payment was not completed.",
        "si": "ගෙවීම සම්පූර්ණ නොවූ නිසා මෙම ඇණවුම අවලංගු විය.",
        "ta": "கட்டணம் முடிக்கப்படாததால் இந்த ஆர்டர் ரத்து செய்யப்பட்டது.",
    },
    "paid.title": {"en": "Paid", "si": "ගෙවා ඇත", "ta": "செலுத்தப்பட்டது"},
    "refunded.title": {"en": "Refunded", "si": "ආපසු ගෙවා ඇත", "ta": "திருப்பி அளிக்கப்பட்டது"},
    "onDelivery.title": {
        "en": "Pay on delivery",
        "si": "ලැබුණු විට ගෙවන්න",
        "ta": "டெலிவரியின் போது செலுத்துங்கள்",
    },
    "payNow": {"en": "Pay now", "si": "දැන් ගෙවන්න", "ta": "இப்போது செலுத்து"},
    "tryAgain": {"en": "Try again", "si": "නැවත උත්සාහ කරන්න", "ta": "மீண்டும் முயற்சி"},
    "result.paid": {
        "en": "Payment successful. Your order is placed.",
        "si": "ගෙවීම සාර්ථකයි. ඔබේ ඇණවුම යොමු කළා.",
        "ta": "கட்டணம் வெற்றி. உங்கள் ஆர்டர் பதிவு செய்யப்பட்டது.",
    },
    "result.failed": {
        "en": "The payment was declined. You can try again from your orders.",
        "si": "ගෙවීම ප්‍රතික්ෂේප විය. ඔබේ ඇණවුම් වෙතින් නැවත උත්සාහ කළ හැක.",
        "ta": "கட்டணம் நிராகரிக்கப்பட்டது. உங்கள் ஆர்டர்களிலிருந்து மீண்டும் முயற்சிக்கலாம்.",
    },
    "result.pending": {
        "en": "We're still waiting for the bank to confirm. Your order will update by itself.",
        "si": "බැංකුවේ තහවුරු කිරීම තවම බලාපොරොත්තුවෙන්. ඔබේ ඇණවුම ඉබේම යාවත්කාලීන වේ.",
        "ta": "வங்கியின் உறுதிப்படுத்தலுக்காக இன்னும் காத்திருக்கிறோம். உங்கள் ஆர்டர் தானாகவே புதுப்பிக்கப்படும்.",
    },
    "error.notPayable": {
        "en": "This order can no longer be paid.",
        "si": "මෙම ඇණවුම තවදුරටත් ගෙවිය නොහැක.",
        "ta": "இந்த ஆர்டருக்கு இனி செலுத்த முடியாது.",
    },
    "error.tooManyTries": {
        "en": "Too many payment tries for this order. Please place a new order.",
        "si": "මෙම ඇණවුමට ගෙවීම් උත්සාහ වැඩියි. කරුණාකර නව ඇණවුමක් කරන්න.",
        "ta": "இந்த ஆர்டருக்கு அதிக முயற்சிகள். புதிய ஆர்டரை செய்யவும்.",
    },
    "error.methodUnavailable": {
        "en": "This payment method is not available right now.",
        "si": "මෙම ගෙවීම් ක්‍රමය දැනට ලබාගත නොහැක.",
        "ta": "இந்த கட்டண முறை தற்போது கிடைக்கவில்லை.",
    },
    "error.gateway": {
        "en": "We couldn't reach the payment service. Please try again in a moment.",
        "si": "ගෙවීම් සේවාව සම්බන්ධ කරගත නොහැකි විය. මොහොතකින් නැවත උත්සාහ කරන්න.",
        "ta": "கட்டண சேவையை அணுக முடியவில்லை. சிறிது நேரத்தில் மீண்டும் முயற்சிக்கவும்.",
    },
    "error.returnMismatch": {
        "en": "That payment does not belong to this order.",
        "si": "එම ගෙවීම මෙම ඇණවුමට අයත් නොවේ.",
        "ta": "அந்தக் கட்டணம் இந்த ஆர்டருக்கு உரியது அல்ல.",
    },
}


def text(key: str, lang: str, **params) -> str:
    table = TEXTS[key]
    value = table.get(lang) or table[DEFAULT_LANGUAGE]
    for name, replacement in params.items():
        value = value.replace("{" + name + "}", str(replacement))
    return value


def status_label(status: str, lang: str) -> str:
    table = STATUS_LABELS.get(status)
    if table is None:
        return status.replace("_", " ").title()
    return table.get(lang) or table[DEFAULT_LANGUAGE]
