"""
Courier Webhooks — inbound endpoints the COURIER calls INTO us.

Authenticated with ``settings.courier_inbound_api_key`` — a SEPARATE secret
from ``courier_api_key`` (the one we send when calling OUT to the courier's
own ``/v1/*`` endpoints). See docs/COURIER_API.md section 15 and
``OrderService._book_courier_shipment``.
"""
from fastapi import APIRouter, Depends, Header, HTTPException, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.database import get_db
from app.config.settings import settings
from app.schemas.courier import DeliveryCodeConfirmRequest, ShipmentStatusWebhook
from app.schemas.order import OrderStatusEnum
from app.services.delivery_verification_service import DeliveryVerificationService
from app.services.order_service import OrderService

# Prefix "/courier/webhooks" is applied by app/api/v1/router.py.
router = APIRouter(tags=["Courier Webhooks"])


async def require_courier_key(x_api_key: str = Header(...)) -> None:
    if not settings.courier_inbound_api_key or x_api_key != settings.courier_inbound_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


@router.post(
    "/orders/{order_number}/delivery-code/confirm",
    dependencies=[Depends(require_courier_key)],
)
async def confirm_delivery_code(
    order_number: str,
    data: DeliveryCodeConfirmRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Confirm the delivery handover code (docs/COURIER_API.md section 15). The
    rider asks the customer for the code at the door and submits it here
    before releasing the parcel. ``waybill_number`` lets the caller cross-check
    but is not itself validated against a specific shipment here —
    ``order_number`` already uniquely identifies the order.
    """
    order = await OrderService.get_by_order_number(db, order_number)
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    matched, message = await DeliveryVerificationService.confirm(db, order, data.code)
    return {"success": matched, "message": message}


@router.post("/shipment-status", dependencies=[Depends(require_courier_key)])
async def shipment_status_webhook(
    data: ShipmentStatusWebhook,
    db: AsyncSession = Depends(get_db),
):
    """
    Courier push notification on shipment status change (docs/COURIER_API.md
    section 10's fixed status vocabulary). Looked up by
    ``courier_shipment_id`` since the courier identifies shipments by its own
    id, not our order number.
    """
    order = await OrderService.get_by_courier_shipment_id(db, data.shipment_id)
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Shipment not found")

    order.courier_tracking_status = data.status
    order.courier_tracking_updated_at = data.at
    await db.commit()

    if data.status == "delivered" and order.status != OrderStatusEnum.DELIVERED.value:
        await OrderService.update_status(db, order, OrderStatusEnum.DELIVERED)

    logger.info(f"Courier webhook: shipment {data.shipment_id} -> {data.status}")
    return {"success": True}
