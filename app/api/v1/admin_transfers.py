"""
Admin Stock Transfer API

Branch-to-branch inventory movement. RBAC: super_admin, branch_manager and
inventory_manager can all view and request transfers (enforced at router
level); only super_admin and inventory_manager can approve/reject/ship/
complete one (``canApproveTransfers`` on the frontend).

Branch isolation is bespoke here rather than a plain ``inject_branch_filter``
scope, because a transfer always involves two branches: a scoped admin may
see and act on any transfer where their branch is either party, not just one
fixed branch_id.

Prefix "/admin/transfers" is applied by app/api/v1/router.py.
"""
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.dependencies import BranchScope, get_current_admin, inject_branch_filter, require_roles
from app.models.branch import Branch
from app.models.stock_transfer import TransferStatus
from app.schemas.stock_transfer import StockTransferCreate, StockTransferReject
from app.services.stock_transfer_service import (
    StockTransferError,
    StockTransferService,
    TransferRow,
)

router = APIRouter(
    dependencies=[Depends(require_roles("super_admin", "branch_manager", "inventory_manager"))],
    tags=["Admin Stock Transfers"],
)

VALID_STATUSES = {s.value for s in TransferStatus}


def _format(row: TransferRow) -> dict:
    transfer, from_branch, to_branch, product, requester, approver = row
    return {
        "transfer_id": str(transfer.transfer_id),
        "from_branch_id": str(transfer.from_branch_id),
        "from_branch_name": from_branch.name,
        "to_branch_id": str(transfer.to_branch_id),
        "to_branch_name": to_branch.name,
        "product_id": str(transfer.product_id),
        "product_name": product.name,
        "quantity": transfer.quantity,
        "status": transfer.status,
        "requested_by_id": str(transfer.requested_by_id),
        "requested_by_name": requester.full_name,
        "approved_by_id": str(transfer.approved_by_id) if transfer.approved_by_id else None,
        "approved_by_name": approver.full_name if approver else None,
        "notes": transfer.notes,
        "requested_at": transfer.requested_at.isoformat() if transfer.requested_at else None,
        "approved_at": transfer.approved_at.isoformat() if transfer.approved_at else None,
        "shipped_at": transfer.shipped_at.isoformat() if transfer.shipped_at else None,
        "completed_at": transfer.completed_at.isoformat() if transfer.completed_at else None,
    }


def _require_party(scope: BranchScope, from_branch_id: UUID, to_branch_id: UUID) -> None:
    """A scoped admin may only act on transfers where their branch is a party."""
    if scope.is_super_admin:
        return
    if scope.branch_id not in (from_branch_id, to_branch_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This transfer does not involve your branch.",
        )


def _require_approver(current_admin) -> None:
    role = current_admin.role.value if hasattr(current_admin.role, "value") else current_admin.role
    if role not in ("super_admin", "inventory_manager"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only Super Admin or Inventory Manager can act on transfers.",
        )


@router.get("/branches", response_model=dict)
async def list_transfer_branches(db: AsyncSession = Depends(get_db)):
    """
    Every active branch's id/name/code — for the "From"/"To" pickers on the
    transfer-request forms. `/admin/branches` itself is Super Admin only
    (full branch management), but a Branch/Inventory Manager still needs to
    see the other branches to request stock from one; nothing here is
    sensitive that isn't already shown elsewhere in the UI to every role.
    """
    rows = (
        await db.execute(
            select(Branch.branch_id, Branch.name, Branch.code)
            .where(Branch.is_active.is_(True))
            .order_by(Branch.name)
        )
    ).all()
    return {
        "success": True,
        "data": {
            "branches": [{"branch_id": str(r.branch_id), "name": r.name, "code": r.code} for r in rows]
        },
    }


@router.get("", response_model=dict)
async def list_transfers(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    transfer_status: Optional[str] = Query(None, alias="status"),
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
):
    """List transfers involving the admin's branch (all branches for Super Admin)."""
    if transfer_status and transfer_status not in VALID_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"status must be one of {sorted(VALID_STATUSES)}",
        )

    rows, total = await StockTransferService.list(
        db,
        branch_id=None if scope.is_super_admin else scope.branch_id,
        status=transfer_status,
        page=page,
        limit=limit,
    )
    return {
        "success": True,
        "data": {
            "transfers": [_format(r) for r in rows],
            "pagination": {
                "total": total,
                "page": page,
                "limit": limit,
                "total_pages": (total + limit - 1) // limit if limit else 1,
            },
            "scope": {"is_super_admin": scope.is_super_admin, "branch_id": str(scope.branch_id) if scope.branch_id else None},
        },
    }


@router.post("", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_transfer(
    body: StockTransferCreate,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
    current_admin=Depends(get_current_admin),
):
    """
    Request a stock transfer. Scoped admins must be a party to it — whichever
    of from/to_branch_id they omit is filled in with their own branch.
    """
    from_branch_id = body.from_branch_id
    to_branch_id = body.to_branch_id

    if not scope.is_super_admin:
        if from_branch_id is None and to_branch_id is None:
            to_branch_id = scope.branch_id
        elif from_branch_id is not None and to_branch_id is not None:
            _require_party(scope, from_branch_id, to_branch_id)
        elif from_branch_id is None:
            from_branch_id = scope.branch_id
        else:
            to_branch_id = scope.branch_id
    elif from_branch_id is None or to_branch_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Super Admin must specify both from_branch_id and to_branch_id.",
        )

    if from_branch_id == to_branch_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="from_branch_id and to_branch_id must be different branches.",
        )

    try:
        transfer = await StockTransferService.create(
            db,
            from_branch_id=from_branch_id,
            to_branch_id=to_branch_id,
            product_id=body.product_id,
            quantity=body.quantity,
            notes=body.notes,
            requested_by_id=current_admin.admin_id,
        )
    except StockTransferError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    found = await StockTransferService.get(db, transfer.transfer_id)
    return {"success": True, "data": _format(found), "message": "Transfer request created"}


async def _load_or_404(db: AsyncSession, transfer_id: UUID) -> TransferRow:
    row = await StockTransferService.get(db, transfer_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transfer not found")
    return row


@router.post("/{transfer_id}/approve", response_model=dict)
async def approve_transfer(
    transfer_id: UUID,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
    current_admin=Depends(get_current_admin),
):
    _require_approver(current_admin)
    row = await _load_or_404(db, transfer_id)
    transfer = row[0]
    _require_party(scope, transfer.from_branch_id, transfer.to_branch_id)

    try:
        await StockTransferService.approve(db, transfer, approved_by_id=current_admin.admin_id)
    except StockTransferError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    found = await StockTransferService.get(db, transfer_id)
    return {"success": True, "data": _format(found), "message": "Transfer approved"}


@router.post("/{transfer_id}/reject", response_model=dict)
async def reject_transfer(
    transfer_id: UUID,
    body: StockTransferReject,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
    current_admin=Depends(get_current_admin),
):
    _require_approver(current_admin)
    row = await _load_or_404(db, transfer_id)
    transfer = row[0]
    _require_party(scope, transfer.from_branch_id, transfer.to_branch_id)

    try:
        await StockTransferService.reject(db, transfer, body.reason)
    except StockTransferError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    found = await StockTransferService.get(db, transfer_id)
    return {"success": True, "data": _format(found), "message": "Transfer rejected"}


@router.post("/{transfer_id}/ship", response_model=dict)
async def ship_transfer(
    transfer_id: UUID,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
    current_admin=Depends(get_current_admin),
):
    _require_approver(current_admin)
    row = await _load_or_404(db, transfer_id)
    transfer = row[0]
    _require_party(scope, transfer.from_branch_id, transfer.to_branch_id)

    try:
        await StockTransferService.mark_in_transit(db, transfer)
    except StockTransferError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    found = await StockTransferService.get(db, transfer_id)
    return {"success": True, "data": _format(found), "message": "Transfer marked in transit"}


@router.post("/{transfer_id}/complete", response_model=dict)
async def complete_transfer(
    transfer_id: UUID,
    db: AsyncSession = Depends(get_db),
    scope: BranchScope = Depends(inject_branch_filter),
    current_admin=Depends(get_current_admin),
):
    _require_approver(current_admin)
    row = await _load_or_404(db, transfer_id)
    transfer = row[0]
    _require_party(scope, transfer.from_branch_id, transfer.to_branch_id)

    try:
        await StockTransferService.complete(db, transfer)
    except StockTransferError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    found = await StockTransferService.get(db, transfer_id)
    return {"success": True, "data": _format(found), "message": "Transfer completed"}
