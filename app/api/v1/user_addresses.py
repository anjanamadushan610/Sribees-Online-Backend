"""
User Address CRUD Endpoints

Serves the mobile app's saved-address flows:

    GET    /api/v1/user/addresses               - List my addresses
    POST   /api/v1/user/addresses               - Create address
    PUT    /api/v1/user/addresses/{address_id}  - Update address
    DELETE /api/v1/user/addresses/{address_id}  - Delete address
    PUT    /api/v1/user/addresses/{address_id}/set-default - Set default

Prefix "/user/addresses" is applied by app/api/v1/router.py.
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.core.dependencies import get_current_user
from app.models.user import Address
from app.schemas.address import (
    AddressCreateRequest,
    AddressDetailResponse,
    AddressListResponse,
    AddressResponse,
    AddressUpdateRequest,
)
from app.services import branch_service

router = APIRouter(tags=["User Addresses"])


async def _build_response(db: AsyncSession, address: Address) -> AddressResponse:
    """
    Map an Address ORM row to the response schema, resolving the delivery branch
    from its post office (hyper-local: the address dictates the serving branch).
    """
    mapping = None
    try:
        mapping = await branch_service.resolve_branch_by_post_office(
            db, address.post_office
        )
    except Exception as exc:  # pragma: no cover - resolution is best-effort
        logger.warning(f"Branch resolution failed for address {address.address_id}: {exc}")

    return AddressResponse(
        address_id=str(address.address_id),
        title=address.title,
        recipient_name=address.recipient_name,
        phone=address.phone,
        address_line1=address.address_line1,
        address_line2=address.address_line2,
        post_office=address.post_office,
        district=address.district,
        province=address.province,
        postal_code=address.postal_code,
        is_default=address.is_default,
        branch_id=str(mapping.branch_id) if mapping else None,
        branch_name=mapping.branch_name if mapping else None,
        is_serviceable=mapping is not None,
    )


async def _get_owned_address(
    db: AsyncSession, address_id: str, user_id: UUID
) -> Address:
    """Load an address, ensuring it belongs to the current user."""
    try:
        address_uuid = UUID(address_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Address not found",
        )

    result = await db.execute(
        select(Address).where(
            and_(Address.address_id == address_uuid, Address.user_id == user_id)
        )
    )
    address = result.scalar_one_or_none()
    if not address:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Address not found",
        )
    return address


async def _clear_default(db: AsyncSession, user_id: UUID) -> None:
    """Unset is_default on all of the user's addresses."""
    await db.execute(
        update(Address)
        .where(Address.user_id == user_id)
        .values(is_default=False)
    )


@router.get("", response_model=AddressListResponse)
async def list_addresses(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
) -> AddressListResponse:
    """List the authenticated user's saved delivery addresses."""
    result = await db.execute(
        select(Address)
        .where(Address.user_id == current_user.user_id)
        .order_by(Address.is_default.desc(), Address.created_at.desc())
    )
    addresses = result.scalars().all()
    return AddressListResponse(data=[await _build_response(db, a) for a in addresses])


@router.post(
    "",
    response_model=AddressDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_address(
    data: AddressCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
) -> AddressDetailResponse:
    """Create a new delivery address for the authenticated user."""
    try:
        if data.is_default:
            await _clear_default(db, current_user.user_id)

        address = Address(
            user_id=current_user.user_id,
            title=data.title,
            recipient_name=data.recipient_name,
            phone=data.phone,
            address_line1=data.address_line1,
            address_line2=data.address_line2,
            post_office=data.post_office,
            district=data.district,
            province=data.province,
            postal_code=data.postal_code or "",
            is_default=data.is_default,
        )
        db.add(address)
        await db.commit()
        await db.refresh(address)

        return AddressDetailResponse(
            data=await _build_response(db, address),
            message="Address created",
        )
    except Exception as e:
        logger.error(f"Error creating address: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create address",
        )


@router.put("/{address_id}", response_model=AddressDetailResponse)
async def update_address(
    address_id: str,
    data: AddressUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
) -> AddressDetailResponse:
    """Update one of the authenticated user's addresses (partial)."""
    address = await _get_owned_address(db, address_id, current_user.user_id)

    try:
        if data.is_default:
            await _clear_default(db, current_user.user_id)

        if data.title is not None:
            address.title = data.title
        if data.recipient_name is not None:
            address.recipient_name = data.recipient_name
        if data.phone is not None:
            address.phone = data.phone
        if data.address_line1 is not None:
            address.address_line1 = data.address_line1
        if data.address_line2 is not None:
            address.address_line2 = data.address_line2
        if data.post_office is not None:
            address.post_office = data.post_office
        if data.district is not None:
            address.district = data.district
        if data.province is not None:
            address.province = data.province
        if data.postal_code is not None:
            address.postal_code = data.postal_code
        if data.is_default is not None:
            address.is_default = data.is_default

        await db.commit()
        await db.refresh(address)

        return AddressDetailResponse(
            data=await _build_response(db, address),
            message="Address updated",
        )
    except Exception as e:
        logger.error(f"Error updating address: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update address",
        )


@router.delete("/{address_id}", response_model=dict)
async def delete_address(
    address_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
) -> dict:
    """Delete one of the authenticated user's addresses."""
    address = await _get_owned_address(db, address_id, current_user.user_id)

    try:
        await db.delete(address)
        await db.commit()
        return {"success": True, "message": "Address deleted"}
    except Exception as e:
        logger.error(f"Error deleting address: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete address",
        )


@router.put("/{address_id}/set-default", response_model=AddressDetailResponse)
async def set_default_address(
    address_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
) -> AddressDetailResponse:
    """Mark an address as the user's default delivery address."""
    address = await _get_owned_address(db, address_id, current_user.user_id)

    try:
        await _clear_default(db, current_user.user_id)
        address.is_default = True
        await db.commit()
        await db.refresh(address)

        return AddressDetailResponse(
            data=await _build_response(db, address),
            message="Default address updated",
        )
    except Exception as e:
        logger.error(f"Error setting default address: {e}")
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to set default address",
        )
