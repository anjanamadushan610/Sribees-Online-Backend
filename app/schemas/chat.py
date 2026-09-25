"""
SRIBEESonline - AI Shopping Chat Schemas
"""

import base64
import binascii
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.assistant_commerce import (
    CartOperation,
    PreferenceChanges,
    RecipeIdea,
    ReminderInput,
)

ChatTask = Literal[
    "track_order",
    "recent_orders",
    "app_help",
    "navigate",
    "memory",
    "reminders",
    "reminder_suggestions",
    "pantry_recipe",
    "compare_products",
    "alternatives",
    "cart_edit",
]
ChatTarget = Literal[
    "orders",
    "cart",
    "checkout",
    "addresses",
    "wallet",
    "offers",
    "settings",
    "support",
    "profile",
]


class ChatPlan(BaseModel):
    """Untrusted model output, validated before any tool runs."""

    # First, so the model settles the language before it writes `reply`.
    # Only consulted when the server could not decide it from the message
    # itself -- see app/services/chat_language.py.
    reply_language: Optional[Literal["en", "si", "ta"]] = None
    reply: str = Field(min_length=1, max_length=1600)
    intent: Literal[
        "buy_product",
        "recipe_ingredients",
        "chitchat",
        "track_order",
        "recent_orders",
        "app_help",
        "navigate",
        "cart_edit",
        "compare_products",
        "alternatives",
        "memory",
        "reminders",
        "pantry_recipe",
    ]
    search_terms: list[str] = Field(default_factory=list, max_length=6)
    brand: Optional[str] = Field(None, max_length=100)
    variant: Optional[str] = Field(None, max_length=100)
    max_price: Optional[float] = Field(None, ge=0, allow_inf_nan=False)
    min_price: Optional[float] = Field(None, ge=0, allow_inf_nan=False)
    is_followup: bool = False
    target: Optional[ChatTarget] = None
    order_reference: Optional[str] = Field(None, max_length=64)
    operations: list[CartOperation] = Field(default_factory=list, max_length=8)
    product_ids: list[str] = Field(default_factory=list, max_length=4)
    memory_operation: Literal["view", "save", "forget"] = "view"
    memory_changes: Optional[PreferenceChanges] = None
    memory_forget_fields: list[
        Literal["favorite_brands", "avoid_ingredients", "household_size", "budget_lkr"]
    ] = Field(default_factory=list, max_length=4)
    reminder: Optional[ReminderInput] = None
    pantry_items: list[str] = Field(default_factory=list, max_length=20)
    recipes: list[RecipeIdea] = Field(default_factory=list, max_length=3)


class ChatMessageRequest(BaseModel):
    """One turn sent to the AI shopping assistant."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field("", max_length=1000)
    image_base64: Optional[str] = Field(
        None,
        max_length=5_600_000,
        description="Optional photo, base64-encoded (no data: prefix).",
    )
    image_mime_type: Optional[Literal["image/jpeg", "image/png", "image/webp"]] = Field(
        None, description="e.g. 'image/jpeg' — required if image_base64 is set."
    )
    task: Optional[ChatTask] = None
    target: Optional[ChatTarget] = None
    order_reference: Optional[str] = Field(None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_turn(self):
        self.message = self.message.strip()
        if not self.message and not self.image_base64 and not self.task:
            raise ValueError("A message, photo, or assistant task is required")
        if bool(self.image_base64) != bool(self.image_mime_type):
            raise ValueError("Photo data and MIME type must be supplied together")
        if self.image_base64:
            try:
                data = base64.b64decode(self.image_base64, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError("Invalid photo encoding") from exc
            signatures = {
                "image/jpeg": data.startswith(b"\xff\xd8\xff"),
                "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
                "image/webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
            }
            if len(data) > 4_000_000 or not signatures[self.image_mime_type]:
                raise ValueError("Photo must match its MIME type and be at most 4 MB")
        return self
