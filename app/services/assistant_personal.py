"""Customer-controlled memory, replenishment suggestions and pantry cooking."""

from datetime import datetime, timedelta, timezone
from statistics import median
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.models.assistant import AssistantProfile, AssistantReceipt, AssistantReminder
from app.models.order import Order, OrderItem
from app.models.user import User
from app.schemas.assistant_commerce import AssistantPreferences
from app.services.assistant_shopping import (
    catalog_product,
    panel,
    proposal,
    search,
    section,
)
from app.services.assistant_tools import action, tr


def next_morning(days, now=None):
    now = now or datetime.now(timezone.utc)
    return (
        (now.astimezone(ZoneInfo("Asia/Colombo")) + timedelta(days=days))
        .replace(hour=9, minute=0, second=0, microsecond=0)
        .astimezone(timezone.utc)
    )


async def preferences(db, user_id):
    if not user_id:
        return AssistantPreferences().model_dump()
    stored = await db.get(AssistantProfile, user_id)
    return AssistantPreferences.model_validate(
        stored.preferences if stored else {}
    ).model_dump()


def preference_rows(data, lang):
    labels = {
        "favorite_brands": tr(
            lang, "Favourite brands", "කැමති වෙළඳ නාම", "விருப்பமான பிராண்டுகள்"
        ),
        "avoid_ingredients": tr(
            lang, "Ingredients to avoid", "මඟහරින අමුද්‍රව්‍ය", "தவிர்க்கும் பொருட்கள்"
        ),
        "household_size": tr(
            lang, "Household size", "ගෙදර සාමාජික ගණන", "குடும்ப உறுப்பினர்கள்"
        ),
        "budget_lkr": tr(lang, "Budget (LKR)", "අයවැය (රු.)", "வரவுசெலவு (LKR)"),
    }
    return [
        (labels[key], ", ".join(value) if isinstance(value, list) else value)
        for key, value in data.items()
        if value is not None and value != []
    ]


def login_result(lang):
    return {
        "reply": tr(
            lang,
            "Sign in to manage your personal preferences and reminders.",
            "ඔබේ මතක තොරතුරු සහ මතක් කිරීම් පාලනය කරන්න ගිණුමට පිවිසෙන්න.",
            "விருப்பங்களையும் நினைவூட்டல்களையும் நிர்வகிக்க உள்நுழையவும்.",
        ),
        "actions": [action("login", lang)],
    }


async def memory_result(db, redis, user_id, plan, lang):
    if not user_id:
        return login_result(lang)
    current = await preferences(db, user_id)
    operation = plan.get("memory_operation", "view")
    changes = plan.get("memory_changes") or {}
    if operation == "save" and not changes:
        return {
            "reply": tr(
                lang,
                "Tell me the brands, budget, household size or ingredients you want me to remember.",
                "මතක තබාගන්න ඕන වෙළඳ නාම, අයවැය, ගෙදර සාමාජික ගණන හෝ මඟහරින අමුද්‍රව්‍ය කියන්න.",
                "நினைவில் வைக்க வேண்டிய பிராண்டுகள், வரவுசெலவு, குடும்ப அளவு அல்லது தவிர்க்கும் பொருட்களைச் சொல்லுங்கள்.",
            )
        }
    if operation == "save":
        # Only explicitly supplied non-null fields are updated; no inferred facts.
        changes = {key: value for key, value in changes.items() if value is not None}
        desired = AssistantPreferences.model_validate(current | changes).model_dump()
        command = await proposal(
            redis,
            user_id,
            "memory",
            {"base": current, "preferences": desired},
            tr(
                lang,
                "Save these preferences",
                "මේ තොරතුරු මතක තබාගන්න",
                "இந்த விருப்பங்களைச் சேமிக்கவும்",
            ),
        )
        reply = tr(
            lang,
            "Review what I will remember. Save only if it is correct.",
            "මතක තබාගන්න තොරතුරු පරීක්ෂා කරන්න. නිවැරදි නම් සුරකින්න.",
            "நினைவில் வைக்கும் தகவலைச் சரிபார்த்து சரியானால் சேமிக்கவும்.",
        )
    elif operation == "forget" and plan.get("memory_forget_fields"):
        desired = current | {
            key: AssistantPreferences().model_dump()[key]
            for key in plan["memory_forget_fields"]
        }
        command = await proposal(
            redis,
            user_id,
            "memory",
            {"base": current, "preferences": desired},
            tr(
                lang,
                "Confirm removing these preferences",
                "මේ මතක තොරතුරු ඉවත් කිරීම තහවුරු කරන්න",
                "இந்த விருப்பங்களை நீக்க உறுதிசெய்யவும்",
            ),
        )
        reply = tr(
            lang,
            "Review the preferences that will remain after this change.",
            "මේ වෙනසෙන් පසු ඉතිරි වන මතක තොරතුරු පරීක්ෂා කරන්න.",
            "இந்த மாற்றத்திற்குப் பின் இருக்கும் விருப்பங்களைச் சரிபார்க்கவும்.",
        )
    else:
        desired = current
        command = await proposal(
            redis,
            user_id,
            "memory",
            {
                "base": current,
                "preferences": AssistantPreferences().model_dump(),
                "forget": True,
            },
            tr(
                lang,
                "Forget all saved preferences",
                "සුරැකි මතක තොරතුරු සියල්ල මකන්න",
                "சேமித்த விருப்பங்கள் அனைத்தையும் மறக்கவும்",
            ),
        )
        reply = tr(
            lang,
            "These are your saved preferences. You can change them by telling me what to remember, or erase them below.",
            "මෙන්න ඔබ සුරැකි මතක තොරතුරු. මතක තබාගන්න ඕන වෙනස්කම කියන්න, නැත්නම් පහළින් සියල්ල මකන්න.",
            "சேமித்த விருப்பங்கள் இதோ. மாற்றத்தைச் சொல்லலாம் அல்லது கீழே அனைத்தையும் அழிக்கலாம்.",
        )
    return {
        "reply": reply,
        "panels": [
            panel(
                tr(lang, "My assistant memory", "සහායකයාගේ මතකය", "உதவியாளர் நினைவு"),
                [section("", preference_rows(desired, lang))],
                [command],
                tr(
                    lang,
                    "Saved preferences help personalize replies. Chat history and reminders have separate delete controls. Ingredient preferences are not verified allergen information.",
                    "සුරැකි තොරතුරු ඔබට ගැළපෙන පිළිතුරු ලබාදීමට භාවිත වෙනවා. කතාබහ සහ මතක් කිරීම් වෙන වෙනම මකන්න පුළුවන්. අමුද්‍රව්‍ය කැමැත්ත තහවුරු කළ අසාත්මිකතා තොරතුරු නොවේ.",
                    "சேமித்த விருப்பங்கள் பதில்களைத் தனிப்பயனாக்க உதவும். அரட்டை மற்றும் நினைவூட்டல்களுக்கு தனித்தனி நீக்கக் கட்டுப்பாடுகள் உள்ளன. பொருள் விருப்பங்கள் சரிபார்க்கப்பட்ட ஒவ்வாமைத் தகவல் அல்ல.",
                ),
            )
        ],
    }


async def reminder_command(redis, user_id, product, days, lang):
    return await proposal(
        redis,
        user_id,
        "reminder_enable",
        {
            "product_id": product["product_id"],
            "name": product["name"],
            "days": days,
            "language": lang,
        },
        tr(
            lang,
            f"Remind every {days} days at 9 AM",
            f"දින {days}කට වරක් උදේ 9ට මතක් කරන්න",
            f"ஒவ்வொரு {days} நாட்களுக்கும் காலை 9 மணிக்கு நினைவூட்டவும்",
        ),
    )


async def reminders_result(db, redis, user_id, branch_id, plan, lang):
    if not user_id:
        return login_result(lang)
    request = plan.get("reminder") or {"operation": "list"}
    operation = request.get("operation", "list")
    sections, commands = [], []
    if operation == "enable":
        product = await catalog_product(db, branch_id, request.get("product_id"), lang)
        choices = (
            [product]
            if product
            else await search(db, branch_id, [request.get("query", "")], lang)
        )
        if len(choices) != 1:
            return {
                "reply": tr(
                    lang,
                    "Specify the exact product and how often to remind you.",
                    "මතක් කරන්න ඕන නිශ්චිත භාණ්ඩය සහ දින ගණන කියන්න.",
                    "சரியான பொருளையும் நினைவூட்ட வேண்டிய நாட்களையும் சொல்லுங்கள்.",
                ),
                "products": choices,
            }
        product = choices[0]
        commands.append(
            await reminder_command(
                redis, user_id, product, request.get("interval_days", 7), lang
            )
        )
        sections.append(section(product["name"], []))
    elif operation == "suggest":
        rows = (
            await db.execute(
                select(OrderItem.product_id, OrderItem.product_name, Order.created_at)
                .join(Order, Order.order_id == OrderItem.order_id)
                .where(
                    Order.user_id == user_id,
                    Order.status == "delivered",
                    Order.created_at
                    >= datetime.now(timezone.utc) - timedelta(days=180),
                )
                .order_by(Order.created_at.desc())
                .limit(300)
            )
        ).all()
        grouped = {}
        for pid, name, at in rows:
            grouped.setdefault(pid, {"name": name, "dates": set()})["dates"].add(
                at.date()
            )
        for pid, values in grouped.items():
            dates = sorted(values["dates"])
            if len(dates) < 2:
                continue
            interval = max(
                3,
                min(
                    90,
                    round(
                        median(
                            (b - a).days for a, b in zip(dates, dates[1:], strict=False)
                        )
                    ),
                ),
            )
            product = await catalog_product(db, branch_id, pid, lang)
            if product:
                sections.append(
                    section(
                        product["name"],
                        [
                            (
                                tr(
                                    lang,
                                    "Suggested interval (days)",
                                    "යෝජිත පරතරය (දින)",
                                    "பரிந்துரைக்கப்பட்ட இடைவெளி (நாட்கள்)",
                                ),
                                interval,
                            )
                        ],
                    )
                )
                command = await reminder_command(
                    redis, user_id, product, interval, lang
                )
                command["label"] = product["name"] + " · " + command["label"]
                commands.append(command)
            if len(commands) == 3:
                break
    else:
        reminders = list(
            (
                await db.execute(
                    select(AssistantReminder)
                    .where(
                        AssistantReminder.user_id == user_id,
                        AssistantReminder.active.is_(True),
                    )
                    .order_by(AssistantReminder.next_due_at)
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
        for reminder in reminders:
            if request.get("reminder_id") and request["reminder_id"] != str(
                reminder.reminder_id
            ):
                continue
            sections.append(
                section(
                    reminder.product_name,
                    [
                        (
                            tr(
                                lang,
                                "Next reminder (Sri Lanka)",
                                "ඊළඟ මතක් කිරීම (ශ්‍රී ලංකා වේලාව)",
                                "அடுத்த நினைவூட்டல் (இலங்கை நேரம்)",
                            ),
                            reminder.next_due_at.astimezone(
                                ZoneInfo("Asia/Colombo")
                            ).strftime("%Y-%m-%d %H:%M"),
                        )
                    ],
                )
            )
            for kind, label in [
                ("cancel", tr(lang, "Stop", "නවත්වන්න", "நிறுத்தவும்")),
                (
                    "snooze",
                    tr(lang, "Snooze 7 days", "දින 7කට කල් දමන්න", "7 நாட்கள் ஒத்திவைக்கவும்"),
                ),
            ]:
                commands.append(
                    await proposal(
                        redis,
                        user_id,
                        "reminder_" + kind,
                        {"reminder_id": str(reminder.reminder_id)},
                        reminder.product_name + " · " + label,
                    )
                )
    return {
        "reply": tr(
            lang,
            "Manage reminders below. Nothing is enabled until you choose a reminder.",
            "මතක් කිරීම් පහළින් පාලනය කරන්න. ඔබ තෝරන තෙක් අලුත් මතක් කිරීමක් සක්‍රිය වෙන්නේ නැහැ.",
            "நினைவூட்டல்களை கீழே நிர்வகிக்கவும். நீங்கள் தேர்ந்தெடுக்கும் வரை புதிய நினைவூட்டல் செயல்படாது.",
        ),
        "panels": [
            panel(
                tr(lang, "Shopping reminders", "භාණ්ඩ මතක් කිරීම්", "ஷாப்பிங் நினைவூட்டல்கள்"),
                sections,
                commands,
                tr(
                    lang,
                    "Reminders are optional suggestions, not a claim that supplies have run out. Times use Sri Lanka time. To get suggestions, ask about items you buy regularly.",
                    "මේවා කැමැත්තෙන් ලබාගන්නා යෝජනා පමණයි; නිවසේ භාණ්ඩ ඉවර බවට ප්‍රකාශයක් නොවේ. වේලාව ශ්‍රී ලංකා වේලාවයි. යෝජනා සඳහා නිතර මිලදී ගන්නා භාණ්ඩ ගැන අහන්න.",
                    "இவை விருப்பமான பரிந்துரைகள்; பொருட்கள் தீர்ந்துவிட்டதாகக் கூறவில்லை. இலங்கை நேரம் பயன்படுத்தப்படுகிறது. பரிந்துரைகளுக்கு வழக்கமாக வாங்கும் பொருட்கள் பற்றிக் கேளுங்கள்.",
                ),
            )
        ],
    }


async def confirm_personal(db, user_id, token, kind, payload, lang):
    # Serialize per account and commit a durable receipt with each write.
    await db.execute(
        select(User.user_id).where(User.user_id == user_id).with_for_update()
    )
    receipt = await db.get(AssistantReceipt, UUID(token))
    if receipt and receipt.user_id == user_id:
        return receipt.response
    if kind == "memory":
        if await preferences(db, user_id) != payload["base"]:
            raise HTTPException(409, "PREFERENCES_CHANGED")
        statement = insert(AssistantProfile).values(
            user_id=user_id, preferences=payload["preferences"]
        )
        await db.execute(
            statement.on_conflict_do_update(
                index_elements=[AssistantProfile.user_id],
                set_={
                    "preferences": payload["preferences"],
                    "updated_at": datetime.now(timezone.utc),
                },
            )
        )
    elif kind == "reminder_enable":
        # One active reminder per product/account through this serialized entrypoint.
        existing = (
            (
                await db.execute(
                    select(AssistantReminder)
                    .where(
                        AssistantReminder.user_id == user_id,
                        AssistantReminder.product_id == UUID(payload["product_id"]),
                        AssistantReminder.active.is_(True),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .first()
        )
        reminder = existing or AssistantReminder(
            reminder_id=UUID(token),
            user_id=user_id,
            product_id=UUID(payload["product_id"]),
            product_name=payload["name"],
        )
        reminder.interval_days = payload["days"]
        reminder.language = payload["language"]
        reminder.next_due_at = next_morning(payload["days"])
        reminder.active = True
        if existing is None:
            db.add(reminder)
    elif kind in ("reminder_cancel", "reminder_snooze"):
        reminder = (
            await db.execute(
                select(AssistantReminder)
                .where(
                    AssistantReminder.reminder_id == UUID(payload["reminder_id"]),
                    AssistantReminder.user_id == user_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if not reminder:
            raise HTTPException(404, "REMINDER_NOT_FOUND")
        if kind == "reminder_cancel":
            reminder.active = False
        else:
            reminder.next_due_at = next_morning(7)
    else:
        raise HTTPException(400, "UNKNOWN_ASSISTANT_ACTION")
    reply = {
        "reply": tr(
            lang,
            "Done. Your choice has been saved.",
            "හරි, ඔබේ තේරීම සුරැකුවා.",
            "சரி, உங்கள் தேர்வு சேமிக்கப்பட்டது.",
        ),
        "products": [],
    }
    db.add(AssistantReceipt(token=UUID(token), user_id=user_id, response=reply))
    await db.commit()
    return reply


async def pantry_result(db, branch_id, plan, lang):
    recipes = plan.get("recipes") or []
    pantry = plan.get("pantry_items") or []
    if not recipes or not pantry:
        return {
            "reply": tr(
                lang,
                "Tell me what ingredients you have, how many people you are cooking for, and anything to avoid.",
                "ගෙදර තියෙන අමුද්‍රව්‍ය, කෑම හදන්නේ කීදෙනෙකුටද සහ මඟහරින්න ඕන දේවල් කියන්න.",
                "வீட்டில் உள்ள பொருட்கள், எத்தனை பேருக்குச் சமைக்கிறீர்கள், தவிர்க்க வேண்டியவற்றைச் சொல்லுங்கள்.",
            )
        }
    panels, missing = [], []
    for recipe in recipes:
        needed = [
            item
            for item in recipe["missing_ingredients"]
            if item.casefold().strip() not in {p.casefold().strip() for p in pantry}
        ]
        missing.extend(needed)
        rows = [
            (tr(lang, "Servings", "කෑම ගන්නා ගණන", "பரிமாறல்கள்"), recipe["servings"]),
            (
                tr(lang, "Ingredients", "අමුද්‍රව්‍ය", "தேவையான பொருட்கள்"),
                ", ".join(recipe["ingredients"]),
            ),
            (
                tr(lang, "Still needed", "තව අවශ්‍ය", "மேலும் தேவை"),
                ", ".join(needed) or "—",
            ),
        ]
        rows += [(str(i + 1), step) for i, step in enumerate(recipe["steps"])]
        panels.append(panel(recipe["title"], [section("", rows)]))
    products = (
        await search(db, branch_id, list(dict.fromkeys(missing))[:6], lang)
        if branch_id and missing
        else []
    )
    return {
        "reply": tr(
            lang,
            "Here are recipe ideas using the ingredients you mentioned. Check the quantities you have; only missing ingredients are searched below.",
            "ඔබ සඳහන් කළ අමුද්‍රව්‍යවලින් හදන්න පුළුවන් කෑම යෝජනා මෙන්න. ගෙදර ඇති ප්‍රමාණ පරීක්ෂා කරන්න; පහළින් සොයන්නේ අඩු අමුද්‍රව්‍ය පමණයි.",
            "நீங்கள் கூறிய பொருட்களைக் கொண்டு உணவு யோசனைகள் இதோ. உங்களிடம் உள்ள அளவுகளைச் சரிபார்க்கவும்; குறைவான பொருட்கள் மட்டுமே கீழே தேடப்படுகின்றன.",
        ),
        "panels": panels,
        "products": products,
    }
