"""The gateways the catalog may route a payment method to, by name."""
from app.payments.gateways.base import PaymentGateway
from app.payments.gateways.marx import MarxGateway

GATEWAYS: dict[str, PaymentGateway] = {
    MarxGateway.name: MarxGateway(),
}


def get_gateway(name: str) -> PaymentGateway:
    try:
        return GATEWAYS[name]
    except KeyError:
        raise LookupError(f"No payment gateway called {name!r}") from None
