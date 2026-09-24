"""
SRIBEESonline - Location rollout models

SRIBEESonline opens district by district, so where a customer may pick an
address from is three switches, all owned by the Super Admin:

    province unlocked  ->  district unlocked  ->  postal area active

These two tables are the first two. The third is the existing
``PostalCityBranchMapping.is_active`` (a postal area is served when a branch is
mapped to it), so routing an order to a branch did not change shape. See
``app/services/location_rollout_service.py`` for the one rule that combines
them, and migration 066 for the seed (all 9 provinces and 25 districts, only
Western / Kalutara unlocked at first).

Names are the primary keys because every other table in this area joins on
names (mappings, addresses and the directory all store ``district`` and
``province`` as text), and a surrogate id here would be a second identity to
keep in step.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.config.database import Base


class LocationProvince(Base):
    __tablename__ = "location_provinces"

    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    name_si: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    name_ta: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Locked = listed to customers as "Coming soon", never selectable.
    is_unlocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    unlocked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class LocationDistrict(Base):
    __tablename__ = "location_districts"

    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    province: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("location_provinces.name", onupdate="CASCADE"),
        nullable=False,
        index=True,
    )
    name_si: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    name_ta: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Unlocked = its postal areas may be activated, and the active ones are
    # offered to customers (while the province is unlocked too).
    is_unlocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    unlocked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
