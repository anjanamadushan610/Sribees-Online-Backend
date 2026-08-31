"""
Courier-facing request schemas — inbound webhooks the COURIER calls INTO us.
See docs/COURIER_API.md section 15 and app/api/v1/courier_webhooks.py.
"""
from datetime import datetime

from pydantic import BaseModel


class DeliveryCodeConfirmRequest(BaseModel):
    """Body of POST /courier/webhooks/orders/{order_number}/delivery-code/confirm."""
    waybill_number: str
    code: str


class ShipmentStatusWebhook(BaseModel):
    """Body of POST /courier/webhooks/shipment-status (docs/COURIER_API.md section 10)."""
    shipment_id: str
    status: str
    at: datetime
