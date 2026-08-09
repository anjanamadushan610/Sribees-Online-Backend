"""
Admin Stock Transfer Schemas

Request bodies for creating and progressing a branch-to-branch transfer.
List/detail responses are hand-built dicts (see admin_transfers.py), matching
the sibling admin_inventory.py module.
"""
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class StockTransferCreate(BaseModel):
    """
    Request stock be moved from one branch to another.

    Scoped admins (Branch Manager, Inventory Manager) must be a party to the
    transfer — one of ``from_branch_id``/``to_branch_id`` has to be their own
    branch, the server fills in whichever side is omitted. Super Admins must
    supply both explicitly.
    """

    from_branch_id: Optional[UUID] = Field(
        None, description="Source branch. Omit if you are a scoped admin requesting stock in."
    )
    to_branch_id: Optional[UUID] = Field(
        None, description="Destination branch. Omit if you are a scoped admin sending stock out."
    )
    product_id: UUID
    quantity: int = Field(..., gt=0)
    notes: Optional[str] = Field(None, max_length=1000)

    @model_validator(mode="after")
    def _branches_differ(self) -> "StockTransferCreate":
        if (
            self.from_branch_id is not None
            and self.to_branch_id is not None
            and self.from_branch_id == self.to_branch_id
        ):
            raise ValueError("from_branch_id and to_branch_id must be different branches.")
        return self


class StockTransferReject(BaseModel):
    """Optional reason recorded when rejecting a pending transfer."""
    reason: Optional[str] = Field(None, max_length=1000)
