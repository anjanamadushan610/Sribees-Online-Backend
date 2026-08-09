"""
Stock Transfer Service

Branch-to-branch inventory movement. Stock is committed twice: decremented
from the source branch on approval (so it can't also be sold or transferred
elsewhere while in transit), and added to the destination branch on
completion (arrival). Rejecting a still-pending request touches no stock.
"""
from datetime import datetime
from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.admin import Admin
from app.models.branch import Branch
from app.models.product import BranchInventory, Product
from app.models.stock_transfer import StockTransfer, TransferStatus
from app.services.product_service import ProductService
from app.utils.logger import logger

FromBranch = aliased(Branch, name="from_branch")
ToBranch = aliased(Branch, name="to_branch")
Requester = aliased(Admin, name="requester")
Approver = aliased(Admin, name="approver")

TransferRow = Tuple[StockTransfer, Branch, Branch, Product, Admin, Optional[Admin]]


class StockTransferError(Exception):
    """Raised for business-rule violations (insufficient stock, bad state)."""


class StockTransferService:
    """Business logic for the stock_transfers table."""

    @staticmethod
    def _base_query():
        return (
            select(StockTransfer, FromBranch, ToBranch, Product, Requester, Approver)
            .join(FromBranch, StockTransfer.from_branch_id == FromBranch.branch_id)
            .join(ToBranch, StockTransfer.to_branch_id == ToBranch.branch_id)
            .join(Product, StockTransfer.product_id == Product.product_id)
            .join(Requester, StockTransfer.requested_by_id == Requester.admin_id)
            .outerjoin(Approver, StockTransfer.approved_by_id == Approver.admin_id)
        )

    @staticmethod
    async def create(
        db: AsyncSession,
        *,
        from_branch_id: UUID,
        to_branch_id: UUID,
        product_id: UUID,
        quantity: int,
        notes: Optional[str],
        requested_by_id: UUID,
    ) -> StockTransfer:
        # The product must actually exist and be stocked at the source branch
        # in at least the requested quantity — otherwise this is a request
        # nobody could ever fulfil.
        source = (
            await db.execute(
                select(BranchInventory).where(
                    and_(
                        BranchInventory.branch_id == from_branch_id,
                        BranchInventory.product_id == product_id,
                    )
                )
            )
        ).scalar_one_or_none()
        if source is None:
            raise StockTransferError("The source branch does not stock this product.")
        if source.stock_quantity < quantity:
            raise StockTransferError(
                f"Source branch only has {source.stock_quantity} units in stock."
            )

        transfer = StockTransfer(
            from_branch_id=from_branch_id,
            to_branch_id=to_branch_id,
            product_id=product_id,
            quantity=quantity,
            notes=notes,
            requested_by_id=requested_by_id,
            status=TransferStatus.PENDING.value,
        )
        db.add(transfer)
        await db.commit()
        await db.refresh(transfer)
        logger.info(
            f"[transfers] {transfer.transfer_id}: {quantity}x {product_id} "
            f"{from_branch_id} -> {to_branch_id} requested by {requested_by_id}"
        )
        return transfer

    @staticmethod
    async def list(
        db: AsyncSession,
        *,
        branch_id: Optional[UUID],
        status: Optional[str] = None,
        page: int = 1,
        limit: int = 20,
    ) -> Tuple[List[TransferRow], int]:
        """
        ``branch_id=None`` means unscoped (Super Admin sees everything).
        Otherwise rows where the branch is either party — a scoped admin
        needs to see transfers they're sending as well as receiving.
        """
        query = StockTransferService._base_query()
        if branch_id is not None:
            query = query.where(
                or_(
                    StockTransfer.from_branch_id == branch_id,
                    StockTransfer.to_branch_id == branch_id,
                )
            )
        if status:
            query = query.where(StockTransfer.status == status)

        count_result = await db.execute(query.with_only_columns(StockTransfer.transfer_id))
        total = len(count_result.all())

        query = query.order_by(StockTransfer.requested_at.desc()).offset((page - 1) * limit).limit(limit)
        rows = (await db.execute(query)).all()
        return [tuple(r) for r in rows], total

    @staticmethod
    async def get(db: AsyncSession, transfer_id: UUID) -> Optional[TransferRow]:
        query = StockTransferService._base_query().where(StockTransfer.transfer_id == transfer_id)
        row = (await db.execute(query)).first()
        return tuple(row) if row else None

    @staticmethod
    async def approve(db: AsyncSession, transfer: StockTransfer, approved_by_id: UUID) -> StockTransfer:
        if transfer.status != TransferStatus.PENDING.value:
            raise StockTransferError(f"Only a pending transfer can be approved (this one is {transfer.status}).")

        source = (
            await db.execute(
                select(BranchInventory).where(
                    and_(
                        BranchInventory.branch_id == transfer.from_branch_id,
                        BranchInventory.product_id == transfer.product_id,
                    )
                )
            )
        ).scalar_one_or_none()
        if source is None or source.stock_quantity < transfer.quantity:
            available = source.stock_quantity if source else 0
            raise StockTransferError(
                f"Source branch only has {available} units available — cannot commit {transfer.quantity}."
            )

        source.stock_quantity -= transfer.quantity
        transfer.status = TransferStatus.APPROVED.value
        transfer.approved_by_id = approved_by_id
        transfer.approved_at = datetime.utcnow()
        await db.commit()
        await db.refresh(transfer)
        logger.info(f"[transfers] {transfer.transfer_id} approved by {approved_by_id}")
        return transfer

    @staticmethod
    async def reject(db: AsyncSession, transfer: StockTransfer, reason: Optional[str]) -> StockTransfer:
        if transfer.status != TransferStatus.PENDING.value:
            raise StockTransferError(f"Only a pending transfer can be rejected (this one is {transfer.status}).")

        transfer.status = TransferStatus.CANCELLED.value
        if reason:
            transfer.notes = f"{transfer.notes}\n\nRejected: {reason}" if transfer.notes else f"Rejected: {reason}"
        await db.commit()
        await db.refresh(transfer)
        logger.info(f"[transfers] {transfer.transfer_id} rejected")
        return transfer

    @staticmethod
    async def mark_in_transit(db: AsyncSession, transfer: StockTransfer) -> StockTransfer:
        if transfer.status != TransferStatus.APPROVED.value:
            raise StockTransferError(f"Only an approved transfer can ship (this one is {transfer.status}).")

        transfer.status = TransferStatus.IN_TRANSIT.value
        transfer.shipped_at = datetime.utcnow()
        await db.commit()
        await db.refresh(transfer)
        logger.info(f"[transfers] {transfer.transfer_id} marked in transit")
        return transfer

    @staticmethod
    async def complete(db: AsyncSession, transfer: StockTransfer) -> StockTransfer:
        if transfer.status != TransferStatus.IN_TRANSIT.value:
            raise StockTransferError(f"Only an in-transit transfer can be completed (this one is {transfer.status}).")

        dest = (
            await db.execute(
                select(BranchInventory).where(
                    and_(
                        BranchInventory.branch_id == transfer.to_branch_id,
                        BranchInventory.product_id == transfer.product_id,
                    )
                )
            )
        ).scalar_one_or_none()
        if dest is None:
            # First time this branch has carried the product — stock it,
            # inheriting the global price/threshold like any other pickup.
            await ProductService.upsert_branch_override(
                db,
                product_id=transfer.product_id,
                branch_id=transfer.to_branch_id,
                stock_quantity=transfer.quantity,
            )
        else:
            dest.stock_quantity += transfer.quantity

        transfer.status = TransferStatus.COMPLETED.value
        transfer.completed_at = datetime.utcnow()
        await db.commit()
        await db.refresh(transfer)
        logger.info(f"[transfers] {transfer.transfer_id} completed")
        return transfer
