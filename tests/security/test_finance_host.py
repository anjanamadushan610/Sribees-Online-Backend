"""Sribees Finance production reaches the internal finance routes by container name."""
from app.core.security_headers import TrustedHostMiddleware


def test_finance_worker_host_is_trusted():
    assert "sribees-online-prod-app" in TrustedHostMiddleware.ALLOWED_HOSTS
    # and nothing broad slipped in
    assert "*" not in TrustedHostMiddleware.ALLOWED_HOSTS
