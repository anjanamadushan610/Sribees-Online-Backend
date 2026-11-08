"""Deterministic customer support menu. No customer/account lookups."""

from datetime import datetime, timezone


def valid_signature(body: bytes, signature: str, secret: str) -> bool:
    import hashlib
    import hmac

    if not secret or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature.encode(), expected.encode())


def incoming_messages(payload: dict, phone_number_id: str, now: datetime):
    """Ignore delivery statuses, other numbers and events outside the reply window."""
    if payload.get("object") != "whatsapp_business_account":
        return
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            if change.get("field") != "messages":
                continue
            if value.get("metadata", {}).get("phone_number_id") != phone_number_id:
                continue
            for message in value.get("messages", []):
                sender = message.get("from", "")
                message_id = message.get("id", "")
                if (
                    not isinstance(sender, str)
                    or not sender.isascii()
                    or not sender.isdigit()
                    or not 7 <= len(sender) <= 15
                ):
                    continue
                if not isinstance(message_id, str) or not 1 <= len(message_id) <= 255:
                    continue
                try:
                    received_at = datetime.fromtimestamp(
                        int(message["timestamp"]), timezone.utc
                    )
                except (KeyError, TypeError, ValueError, OverflowError, OSError):
                    continue
                age = (now - received_at).total_seconds()
                # Leave five minutes for queue/network time before Meta's 24h boundary.
                if age < -300 or age >= 86100:
                    continue
                text = ""
                kind = message.get("type")
                if kind == "text":
                    text = message.get("text", {}).get("body", "")
                elif kind == "interactive":
                    interactive = message.get("interactive", {})
                    text = interactive.get(
                        "button_reply", interactive.get("list_reply", {})
                    ).get("id", "")
                if not isinstance(text, str):
                    text = ""
                yield message_id, sender, received_at, text.strip()[:4096], kind


def menu(language: str) -> dict:
    sinhala = language == "si"
    return {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {
                "text": (
                    "Sribees Online වෙත සාදරයෙන් පිළිගනිමු! 🛒\nඅවශ්‍ය සේවාව තෝරන්න.\nEnglish සඳහා EN යවන්න."
                    if sinhala
                    else "Welcome to Sribees Online! 🛒\nChoose an option below.\nසිංහල සඳහා SI යවන්න."
                )
            },
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {
                            "id": "delivery",
                            "title": "බෙදාහැරීම" if sinhala else "Delivery & fees",
                        },
                    },
                    {
                        "type": "reply",
                        "reply": {
                            "id": "app",
                            "title": "App එක" if sinhala else "Get the app",
                        },
                    },
                    {
                        "type": "reply",
                        "reply": {
                            "id": "support",
                            "title": "සහාය ලබාගන්න" if sinhala else "Customer support",
                        },
                    },
                ]
            },
        },
    }


def text_reply(text: str) -> dict:
    return {"type": "text", "text": {"body": text, "preview_url": False}}


def answer(text: str, language: str) -> dict:
    sinhala = language == "si"
    if text == "delivery":
        return text_reply(
            "App එකේ Deliver location තෝරා සේවාව පවතිනවාද බලන්න. Delivery charge එක ලිපිනය සහ ඇණවුමේ බර අනුව checkout එකේ පෙන්වයි.\nනැවත menu එකට MENU යවන්න."
            if sinhala
            else "Select your delivery location in the app to check availability. Delivery fees depend on the address and order weight and are shown at checkout.\nSend MENU to return."
        )
    return text_reply(
        "Sribees Online app එක සහ තොරතුරු සඳහා https://www.sribeesonline.com වෙත පිවිසෙන්න.\nAndroid: https://play.google.com/store/apps/details?id=com.sribeesonline.sribees_mobile\nනැවත menu එකට MENU යවන්න."
        if sinhala
        else "Visit https://www.sribeesonline.com for Sribees Online and app information.\nAndroid: https://play.google.com/store/apps/details?id=com.sribeesonline.sribees_mobile\nSend MENU to return."
    )
