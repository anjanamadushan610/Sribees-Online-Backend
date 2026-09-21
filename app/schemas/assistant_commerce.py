"""Bounded inputs for shopping tools; model output never authorizes a write."""

from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

ShortText = Annotated[str, Field(min_length=1, max_length=100)]
StepText = Annotated[str, Field(min_length=1, max_length=400)]


class CartOperation(BaseModel):
    operation: Literal["add", "set", "remove"]
    query: str = Field("", max_length=100)
    product_id: Optional[str] = Field(None, max_length=36)
    variant_id: Optional[str] = Field(None, max_length=36)
    quantity: int = Field(1, ge=1, le=99)


class AssistantPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    favorite_brands: list[ShortText] = Field(default_factory=list, max_length=12)
    avoid_ingredients: list[ShortText] = Field(default_factory=list, max_length=20)
    household_size: Optional[int] = Field(None, ge=1, le=20)
    budget_lkr: Optional[float] = Field(None, ge=0, le=1_000_000, allow_inf_nan=False)


class PreferenceChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")
    favorite_brands: Optional[list[ShortText]] = Field(None, max_length=12)
    avoid_ingredients: Optional[list[ShortText]] = Field(None, max_length=20)
    household_size: Optional[int] = Field(None, ge=1, le=20)
    budget_lkr: Optional[float] = Field(None, ge=0, le=1_000_000, allow_inf_nan=False)


class RecipeIdea(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    servings: int = Field(2, ge=1, le=20)
    ingredients: list[ShortText] = Field(min_length=1, max_length=15)
    missing_ingredients: list[ShortText] = Field(default_factory=list, max_length=6)
    steps: list[StepText] = Field(min_length=1, max_length=8)


class ReminderInput(BaseModel):
    operation: Literal["list", "suggest", "enable", "cancel", "snooze"] = "list"
    product_id: Optional[str] = Field(None, max_length=36)
    query: str = Field("", max_length=100)
    interval_days: int = Field(7, ge=1, le=90)
    reminder_id: Optional[str] = Field(None, max_length=36)


class ConfirmAssistantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(pattern=r"^[a-f0-9]{32}$")
