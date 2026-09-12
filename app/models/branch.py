"""
SRIBEESonline - Branch & Post Office Mapping Models

SQLAlchemy models for branch management and address-to-branch routing.
"""
from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config.database import Base


class Branch(Base):
    """
    Branch model representing a store branch location.

    Each branch serves a geographic area and has its own inventory,
    staff, and marketing configuration.
    """

    __tablename__ = "branches"

    branch_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    post_office: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    district: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    province: Mapped[str] = mapped_column(String(100), nullable=False)
    phone: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    manager_id: Mapped[Optional[UUID]] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("admins.admin_id", ondelete="SET NULL"),
        nullable=True,
    )
    # New branches start inactive: a Super Admin creates the branch, but only
    # its Branch Manager can bring it live (PATCH /admin/branches/{id}/status).
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    # SribeesExpress pickup location registered for this branch (contract
    # section 12). We are a multi-branch merchant, so the rider must collect
    # from the fulfilling branch, not from one account-wide default address.
    # NULL = not registered yet; booking then falls back to that default.
    courier_pickup_location_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    courier_pickup_synced_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # This branch's own SribeesExpress merchant keys, for a branch that signs
    # with them in its own name. Both environments are held at once so going
    # live is a switch rather than a re-entry. Fernet-encrypted at rest
    # (app/core/crypto.py); never returned by the API, only masked, and only a
    # Super Admin can set them. NULL = book against the account-wide key.
    courier_api_key_test: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    courier_api_key_live: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Pins this branch to 'test' or 'live' regardless of the account default.
    # NULL = follow the account. Exists for a staged go-live: one branch books
    # real parcels while the rest stay in the sandbox.
    courier_environment: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    courier_api_key_set_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    courier_api_key_set_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # The branch's daily dispatch rounds, ordered by dispatch_time. Each entry is
    # {"label": str, "dispatch_time": "HH:MM" (24h, Asia/Colombo), "cutoff_minutes": int}.
    # Ordering closes `cutoff_minutes` before the van leaves. Kept per branch
    # rather than globally because a city branch and a regional one genuinely
    # run different timetables. Empty list = not configured yet, which the
    # dashboard surfaces as "no runs configured" rather than inventing one.
    delivery_runs: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default="[]",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    manager = relationship("Admin", foreign_keys=[manager_id], backref="managed_branch")
    post_office_mappings = relationship(
        "PostOfficeBranchMapping",
        back_populates="branch",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Branch {self.code}: {self.name}>"

    def to_dict(self) -> dict:
        """Convert branch to dictionary."""
        return {
            "branchId": str(self.branch_id),
            "name": self.name,
            "code": self.code,
            "address": self.address,
            "postOffice": self.post_office,
            "district": self.district,
            "province": self.province,
            "phone": self.phone,
            "managerId": str(self.manager_id) if self.manager_id else None,
            "isActive": self.is_active,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None,
        }


class PostOfficeBranchMapping(Base):
    """
    Maps Post Office names to serving branches.

    Used for address-based branch routing: when a customer selects
    a delivery address, the post_office field is looked up in this table
    to resolve which branch_id serves that area.
    """

    __tablename__ = "post_office_branch_mapping"

    mapping_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    post_office: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        nullable=False,
        index=True,
        comment="Post office name — must match addresses.post_office values",
    )
    branch_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("branches.branch_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    branch_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Denormalized branch name for fast reads",
    )
    district: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    province: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    branch = relationship("Branch", back_populates="post_office_mappings")

    # B-tree indexes for fast hierarchical filtering (province → district → post_office)
    __table_args__ = (
        Index("idx_po_branch_mapping_province", "province"),
        Index("idx_po_branch_mapping_district", "district"),
        Index("idx_po_branch_mapping_active", "is_active"),
        Index(
            "idx_po_branch_mapping_location_triplet",
            "province", "district", "post_office",
        ),
    )

    def __repr__(self) -> str:
        return f"<PostOfficeBranchMapping {self.post_office} → {self.branch_name}>"

    def to_dict(self) -> dict:
        """Convert mapping to dictionary."""
        return {
            "mappingId": str(self.mapping_id),
            "postOffice": self.post_office,
            "branchId": str(self.branch_id),
            "branchName": self.branch_name,
            "district": self.district,
            "province": self.province,
            "isActive": self.is_active,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None,
        }


class PostOfficeDirectory(Base):
    """
    Master directory of Sri Lankan Post Offices (the "Delivery Zones" catalog).

    This is the source-of-truth list, independent of branch coverage: every
    Post Office is tagged with its District and Province. The Admin Dashboard
    manages it (Settings → Delivery Zones), and the Branch form reads from it
    to offer coverage-area (Post Office) options for the selected district.

    Assigning coverage on a Branch writes ``PostOfficeBranchMapping`` rows; this
    directory just says "which post offices exist and where", not "who serves
    them".
    """

    __tablename__ = "post_office_directory"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    post_office: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        nullable=False,
        index=True,
        comment="Post office name — unique across Sri Lanka",
    )
    district: Mapped[str] = mapped_column(String(100), nullable=False)
    province: Mapped[str] = mapped_column(String(100), nullable=False)
    # is_active is now maintained by courier_coverage_service.sync(): FALSE
    # means SribeesExpress reported on this district and did not list this post
    # office, i.e. they cannot deliver there yet. Checkout must refuse it
    # rather than quote a parcel that will be rejected at booking.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # The city_id this post office resolved to in the last coverage sync.
    # Rate quotes and bookings are keyed on city_id, not on the post office
    # name, so caching it here saves a POST /locations/resolve per address.
    courier_city_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    courier_synced_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("idx_po_directory_province", "province"),
        Index("idx_po_directory_district", "district"),
        Index("idx_po_directory_province_district", "province", "district"),
    )

    def __repr__(self) -> str:
        return f"<PostOfficeDirectory {self.post_office} ({self.district}, {self.province})>"

    def to_dict(self) -> dict:
        """Convert directory entry to dictionary (snake_case wire)."""
        return {
            "id": str(self.id),
            "post_office": self.post_office,
            "district": self.district,
            "province": self.province,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
