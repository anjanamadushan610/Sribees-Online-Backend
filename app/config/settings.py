"""
SRIBEESonline FastAPI Backend - Application Settings

Pydantic-based settings management with environment variable support.
Supports multiple environments: development, staging, production.
"""
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Get the project root directory (where .env is located)
_PROJECT_ROOT = Path(__file__).parent.parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # =========================================================================
    # Application Settings
    # =========================================================================
    app_name: str = Field(default="SRIBEESonline API", description="Application name")
    app_env: str = Field(default="development", description="Environment (development/staging/production)")
    debug: bool = Field(default=False, description="Debug mode")
    api_version: str = Field(default="v1", description="API version prefix")

    # =========================================================================
    # Wallet & Cashback
    # =========================================================================
    cashback_rate: float = Field(
        default=0.01,
        ge=0.0,
        le=1.0,
        description=(
            "Platform default cashback rate (0.01 = 1%). Last resort only: a "
            "product's cashback_percentage overrides this, and a branch's "
            "override beats both."
        ),
    )
    wallet_currency: str = Field(default="LKR", description="Wallet currency code")
    loyalty_spend_per_point: float = Field(
        default=1000.0,
        gt=0,
        description=(
            "Rupees of order value (excluding delivery) per loyalty point. "
            "Fallback only: the Super Admin's platform setting overrides it."
        ),
    )

    # =========================================================================
    # Referrals
    # =========================================================================
    referral_commission_percent: float = Field(
        default=2.0,
        ge=0.0,
        le=100.0,
        description=(
            "Percentage of a referred user's order subtotal paid to their "
            "referrer (2.0 = 2%). Snapshotted onto each commission row, so "
            "changing this never rewrites what people already earned."
        ),
    )
    referral_level2_percent: float = Field(
        default=1.0,
        ge=0.0,
        le=100.0,
        description=(
            "Percentage paid to a partner's upline on orders from customers "
            "their recruit signed up (level 2). Lower than level 1 by design: "
            "the recruit did the work. Two levels only — a deeper chain is a "
            "pyramid, not a referral programme. Agent 2%, lead 1%."
        ),
    )
    referral_code_length: int = Field(
        default=8,
        ge=6,
        le=12,
        description="Length of generated invite codes. Shorter is easier to "
                    "share but easier to enumerate; 8 over a 31-char alphabet "
                    "is ~10^12 combinations.",
    )

    # =========================================================================
    # Order Pricing
    # =========================================================================
    # No delivery fee here: the delivery charge is SribeesExpress's quote for
    # each parcel (app/services/pricing_service.py). A FLAT_DELIVERY_FEE left in
    # an environment file is ignored, not an error.
    cart_delivery_quote_cache_seconds: int = Field(
        default=600,
        ge=0,
        le=1500,
        description=(
            "How long the cart screen reuses a SribeesExpress delivery quote for the same "
            "account, postal city, weight and payment method. Kept under the courier's "
            "30-minute quote lifetime; checkout always asks for a fresh one."
        ),
    )
    order_tax_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Tax rate applied to the discounted subtotal (0.0 = none)",
    )

    # =========================================================================
    # Sentry Error Tracking
    # =========================================================================
    sentry_dsn: Optional[str] = Field(default=None, description="Sentry DSN for error tracking")
    sentry_traces_sample_rate: float = Field(default=0.1, description="Sentry traces sample rate (0.0-1.0)")
    sentry_profiles_sample_rate: float = Field(default=0.1, description="Sentry profiles sample rate (0.0-1.0)")

    # =========================================================================
    # Server Settings
    # =========================================================================
    # Inside the container; the published port is loopback-only (see
    # docker/ec2-docker-compose.yml), so this is not an exposure.
    host: str = Field(default="0.0.0.0", description="Server host")  # nosec B104
    port: int = Field(default=8000, description="Server port")
    workers: int = Field(default=4, description="Number of worker processes")

    # =========================================================================
    # PostgreSQL Database Settings
    # =========================================================================
    database_url: str = Field(
        default="postgresql+asyncpg://postgres:password@localhost:5432/sribeesonline",
        description="PostgreSQL async connection URL"
    )
    database_pool_size: int = Field(default=20, description="Database connection pool size")
    database_max_overflow: int = Field(default=10, description="Max overflow connections")
    database_pool_timeout: int = Field(default=30, description="Pool timeout in seconds")
    database_echo: bool = Field(default=False, description="Echo SQL queries (debug)")

    # Backward compatibility with legacy env vars
    database_host: str = Field(default="localhost", description="Database host")
    database_port: int = Field(default=5432, description="Database port")
    database_name: str = Field(default="sribeesonline", description="Database name")
    database_user: str = Field(default="postgres", description="Database user")
    database_password: str = Field(default="", description="Database password")

    @property
    def async_database_url(self) -> str:
        """Build async database URL from components if DATABASE_URL not set."""
        if self.database_url and "asyncpg" in self.database_url:
            return self.database_url
        return (
            f"postgresql+asyncpg://{self.database_user}:{self.database_password}"
            f"@{self.database_host}:{self.database_port}/{self.database_name}"
        )

    # =========================================================================
    # Redis Settings
    # =========================================================================
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL"
    )
    redis_host: str = Field(default="localhost", description="Redis host")
    redis_port: int = Field(default=6379, description="Redis port")
    redis_password: Optional[str] = Field(default=None, description="Redis password")
    redis_db: int = Field(default=0, description="Redis database number")

    @property
    def redis_connection_url(self) -> str:
        """Build Redis URL from components if REDIS_URL not set."""
        if self.redis_url:
            return self.redis_url
        if self.redis_password:
            return f"redis://:{self.redis_password}@{self.redis_host}:{self.redis_port}/{self.redis_db}"
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"

    # =========================================================================
    # JWT Settings
    # =========================================================================
    jwt_secret_key: str = Field(
        default="your-secret-key-change-in-production",
        description="JWT secret key"
    )
    jwt_algorithm: str = Field(default="HS256", description="JWT algorithm")
    credential_encryption_key: str = Field(
        default="",
        description=(
            "Fernet key (urlsafe-base64, 32 bytes) that encrypts third-party credentials "
            "stored in the database -- see app/core/crypto.py. Generate once with "
            "Fernet.generate_key(). Left empty it is derived from JWT_SECRET_KEY, which "
            "works but ties the two together: rotating the JWT secret would then make "
            "stored credentials unreadable."
        ),
    )
    jwt_access_token_expire_minutes: int = Field(default=15, description="Access token expiry (minutes)")
    jwt_refresh_token_expire_days: int = Field(default=7, description="Refresh token expiry (days)")

    # Backward compatibility with Express backend
    jwt_access_expiry: str = Field(default="15m", description="Access token expiry string")
    jwt_refresh_expiry: str = Field(default="7d", description="Refresh token expiry string")

    # Phone numbers that may sign in with the fixed test OTP instead of a real
    # one: testers and the store reviewers' demo accounts. Comma-separated, in
    # any shape normalize_mobile accepts. EMPTY BY DEFAULT, which means no
    # number can -- the code used to work for every phone in production, which
    # let anyone sign in as any phone-registered customer (SEC-001).
    otp_test_phones: str = Field(
        default="",
        description="Comma-separated mobile numbers allowed to use the test OTP",
    )

    # =========================================================================
    # SMS (one-time codes)
    # =========================================================================
    # Which company carries the texts (app/services/sms_gateway.py). "console"
    # only logs them, so no environment spends credit until it is switched on.
    sms_provider: str = Field(default="console", description="console | notifylk")
    notifylk_user_id: str = Field(default="", description="Notify.lk user ID")
    notifylk_api_key: str = Field(default="", description="Notify.lk API key")
    notifylk_sender_id: str = Field(default="NotifyDEMO", description="Approved Notify.lk sender name")
    notifylk_base_url: str = Field(default="https://app.notify.lk/api/v1")
    # The sending limits are NOT environment settings: the per-number and
    # per-address rules are code (OTPService), identical on every server, and
    # the daily total is the Super Admin's (SMS page, app_settings).


    # =========================================================================
    # Email Settings
    # =========================================================================
    mail_from: str = Field(default="noreply@sribeesonline.lk", description="From email address")
    mail_from_name: str = Field(default="SRIBEESonline", description="From name")
    mail_server: str = Field(default="smtp.gmail.com", description="SMTP server")
    mail_port: int = Field(default=587, description="SMTP port")
    mail_username: str = Field(default="", description="SMTP username")
    mail_password: str = Field(default="", description="SMTP password")
    mail_starttls: bool = Field(default=True, description="Use STARTTLS")
    mail_ssl_tls: bool = Field(default=False, description="Use SSL/TLS")

    # Backward compatibility
    email_from: Optional[str] = Field(default=None)
    email_host: Optional[str] = Field(default=None)
    email_port: Optional[int] = Field(default=None)
    email_user: Optional[str] = Field(default=None)
    email_password: Optional[str] = Field(default=None)

    # =========================================================================
    # Stripe Payment Settings
    # =========================================================================
    stripe_secret_key: str = Field(default="", description="Stripe secret key")
    stripe_publishable_key: str = Field(default="", description="Stripe publishable key")
    stripe_webhook_secret: str = Field(default="", description="Stripe webhook secret")

    # =========================================================================
    # Marx IPG Payment Gateway
    # =========================================================================
    marx_ipg_base_url: str = Field(
        default="https://payment.api.dev.marxpos.com",
        description="Marx IPG API base URL. Sandbox default; prod is https://payment.v4.api.marx.lk",
    )
    marx_ipg_api_key: str = Field(default="", description="Marx merchant-api-key (server-side secret)")
    marx_ipg_return_url: str = Field(
        default="",
        description="Backend URL Marx redirects the shopper back to after 3DS, e.g. https://api.sribees.lk/api/v1/payments/marx/return",
    )
    marx_ipg_valid_time_limit_hours: int = Field(
        default=2, description="validTimeLimit (hours) sent at order creation (doc section 5.1)"
    )
    marx_ipg_timeout_seconds: float = Field(default=15.0, description="HTTP timeout for Marx API calls")
    marx_gateway_methods_enabled: bool = Field(
        default=False,
        description=(
            "Whether MARX_OTHER/MARX_AMEX/MARX_BANK appear in GET /payment-methods. "
            "The backend can create/confirm Marx orders as soon as it's deployed, "
            "but only the UPDATED mobile app knows how to act on payment_redirect_url. "
            "Keep this off until that app release has rolled out -- a backend deploy "
            "ships instantly, a Flutter app release does not."
        ),
    )

    # =========================================================================
    # Courier Integration -- SribeesExpress
    # (see fastapi_backend/docs/SRIBEESEXPRESS_ECOMMERCE_API.md)
    # =========================================================================
    # The base URL and the merchant keys are NOT settings. They live in the
    # database and are managed from Settings -> Courier, because:
    #
    #   * the sandbox -> live switch is the change most likely to be made under
    #     time pressure, and requiring a redeploy for it is how a launch slips;
    #   * a branch can hold its own SribeesExpress merchant account, and one
    #     environment variable cannot express "per branch, per environment";
    #   * the base URL is not a secret -- anyone holding a key already knows
    #     which host it belongs to -- so there was never a reason for it to be
    #     deployment config in the first place.
    #
    # The keys are encrypted at rest (app/core/crypto.py) with
    # CREDENTIAL_ENCRYPTION_KEY above; see app/services/courier_credentials.py
    # for how a call resolves branch -> account -> environment.
    courier_webhook_secret: str = Field(
        default="",
        description=(
            "HMAC-SHA256 secret from POST /ecommerce/webhook-endpoints, shown once at "
            "registration. Used to verify X-Sribees-Signature on inbound pushes "
            "(contract section 10.3). Replaces the old shared courier_inbound_api_key."
        ),
    )
    courier_webhook_tolerance_seconds: int = Field(
        default=300,
        description="Reject a signed webhook whose t= timestamp is older than this (replay window).",
    )
    courier_request_timeout_seconds: float = Field(
        default=3.0,
        description="Per-request timeout. One retry on timeout/5xx, never on 4xx.",
    )
    courier_sync_batch_limit: int = Field(
        default=100,
        description="Page size for the GET /ecommerce/shipments reconciliation sweep (contract section 7a).",
    )
    # 4, not 3: SribeesExpress accepts 4-6 digits and rejects anything shorter
    # at booking (contract section 7). The old default of 3 only ever worked
    # because DeliveryVerificationService clamps it up to 4 — so the setting
    # said one thing and the codes said another. 6 is the safer operational
    # choice (10^6 guesses vs 10^4), but the 5-attempt lockout is what actually
    # bounds guessing, so this stays at the contract minimum rather than
    # silently changing the length of codes already in flight.
    delivery_verification_code_digits: int = Field(
        default=4, ge=4, le=6, description="Delivery handover code length (SribeesExpress accepts 4-6)"
    )
    delivery_verification_max_attempts: int = Field(default=5, description="Wrong-code attempts before lockout")

    # =========================================================================
    # Firebase Cloud Messaging (FCM) Settings
    # =========================================================================
    firebase_credentials_path: Optional[str] = Field(
        default=None,
        description="Path to Firebase service account JSON file"
    )
    firebase_credentials_json: Optional[str] = Field(
        default=None,
        description=(
            "The service account JSON itself, as a string. Preferred over "
            "firebase_credentials_path in a container: it lives with every "
            "other secret in the deploy's env file instead of needing a file "
            "written to the host and a volume mounted to the right path. A "
            "mount that silently fails leaves push notifications dead with no "
            "error anywhere."
        ),
    )
    fcm_server_key: Optional[str] = Field(
        default=None,
        description="FCM server key (deprecated, use service account)"
    )

    # Legacy Expo Push (deprecated - use FCM)
    expo_access_token: str = Field(default="", description="Expo push notification token (deprecated)")

    # =========================================================================
    # Share Links (Android App Links + Play Store fallback)
    # =========================================================================
    android_package_name: str = Field(
        default="com.sribeesonline.sribees_mobile",
        description="Android applicationId the /p/{id} share links open",
    )
    android_sha256_cert_fingerprints: str = Field(
        default="",
        description=(
            "Comma-separated SHA-256 signing-cert fingerprints served in "
            "/.well-known/assetlinks.json (Play App Signing key + upload key). "
            "Empty = App Links stay unverified; links still work via the "
            "intent:// fallback on the share page."
        ),
    )

    # =========================================================================
    # CORS Settings
    # =========================================================================
    cors_origins: List[str] = Field(
        default=[
            "http://localhost:3001",
            "http://localhost:19006",
            "http://localhost:8081",
            # Production web/admin origins (also covered by the regex in main.py).
            "https://sribees.com",
            "https://www.sribees.com",
            "https://admin.sribees.com",
        ],
        description="Allowed CORS origins"
    )
    cors_allow_credentials: bool = Field(default=True, description="Allow credentials")
    cors_allow_methods: List[str] = Field(default=["*"], description="Allowed methods")
    cors_allow_headers: List[str] = Field(default=["*"], description="Allowed headers")

    # Backward compatibility
    frontend_url: str = Field(default="http://localhost:3001", description="Frontend URL")

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, v):
        """Parse CORS origins from string or list."""
        if isinstance(v, str):
            v = v.strip("'\"").strip()
            import json
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return [origin.strip("'\"[] ") for origin in v.split(",")]
        return v

    # =========================================================================
    # Rate Limiting Settings
    # =========================================================================
    rate_limit_per_minute: int = Field(default=60, description="Requests per minute")
    rate_limit_auth_per_minute: int = Field(default=10, description="Auth requests per minute")
    rate_limit_password_reset_per_hour: int = Field(default=3, description="Password reset per hour")

    # Backward compatibility
    rate_limit_window_ms: int = Field(default=900000, description="Rate limit window (ms)")
    rate_limit_max_requests: int = Field(default=100, description="Max requests per window")

    # =========================================================================
    # File Upload Settings
    # =========================================================================
    max_file_size: int = Field(default=5242880, description="Max file size (5MB)")
    upload_dir: str = Field(default="./uploads", description="Upload directory")
    allowed_extensions: List[str] = Field(
        default=["jpg", "jpeg", "png", "gif", "webp"],
        description="Allowed file extensions"
    )

    # =========================================================================
    # Logging Settings
    # =========================================================================
    log_level: str = Field(default="INFO", description="Log level")
    log_format: str = Field(
        default="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        description="Loguru format"
    )
    log_file: Optional[str] = Field(default="logs/app.log", description="Log file path")
    log_rotation: str = Field(default="10 MB", description="Log rotation size")
    log_retention: str = Field(default="7 days", description="Log retention period")

    # =========================================================================
    # Gemini AI Settings
    # =========================================================================
    gemini_api_key: Optional[str] = Field(
        default=None,
        description="Google Gemini API key (assistant, briefings, keyword suggestions)"
    )
    gemini_generation_model: str = Field(
        default="gemini-flash-latest",
        description=(
            "Gemini text-generation model name (admin keyword suggestions). "
            "Uses Google's rolling 'latest flash' alias rather than a pinned "
            "version, since pinned free-tier model names get retired."
        )
    )
    gemini_keyword_daily_limit: int = Field(
        default=100,
        ge=0,
        description=(
            "Most AI keyword suggestions the admin panel may request per day "
            "(Asia/Colombo), across all admins. Repeats of the same product "
            "are served from a 30-day cache and do not count. 0 turns the "
            "feature off. The hard ceiling on what this feature can cost."
        ),
    )
    gemini_chat_thinking_budget: int = Field(
        default=0,
        ge=-1,
        description=(
            "Output tokens the shopping assistant may spend 'thinking' before "
            "its visible answer. Flash bills these as output, and the chat "
            "asks for a classify-and-extract plan rather than reasoning, so "
            "the default is 0 (off). Raise it if intent accuracy drops; -1 "
            "hands the budget back to the model."
        ),
    )
    gemini_chat_history_turns: int = Field(
        default=6,
        ge=1,
        le=20,
        description=(
            "Conversation turns kept and re-sent on every chat message. Every "
            "turn is prompt tokens paid for again on the next message, and "
            "product follow-ups already carry their own search_context, so "
            "this is deliberately short."
        ),
    )

    # Circuit Breaker for AI Services
    ai_circuit_failure_threshold: int = Field(
        default=5,
        description="Failures before circuit opens"
    )
    ai_circuit_recovery_timeout: int = Field(
        default=60,
        description="Circuit recovery timeout in seconds"
    )

    # =========================================================================
    # Media Storage
    #
    # The database stores ONLY a relative path (e.g. /uploads/categories/x.jpg).
    # The absolute URL is composed at serialization time as
    # MEDIA_BASE_URL + path. Swapping to S3/R2 is therefore a provider swap plus
    # an env change — no rows to rewrite, no migration.
    # =========================================================================
    storage_backend: str = Field(
        default="local",
        description="Which StorageProvider to use: 'local' (disk) or 's3'.",
    )
    media_root: str = Field(
        default="/app/media",
        description=(
            "Filesystem directory the local provider writes into. Must be a "
            "mounted volume in production, or uploads die with the container."
        ),
    )
    media_url_prefix: str = Field(
        default="/uploads",
        description="URL path the local provider's files are served under.",
    )
    media_base_url: str = Field(
        default="",
        description=(
            "Origin prepended to stored relative paths to build absolute URLs "
            "(e.g. https://api.sribees.com, or an S3/R2 public base later). "
            "Empty means serve them as same-origin relative URLs."
        ),
    )
    media_max_upload_size_mb: int = Field(
        default=5, description="Maximum image upload size in megabytes"
    )

    # =========================================================================
    # AWS S3 Storage Settings (unused while storage_backend='local')
    # =========================================================================
    aws_access_key_id: Optional[str] = Field(
        default=None, description="AWS access key ID"
    )
    aws_secret_access_key: Optional[str] = Field(
        default=None, description="AWS secret access key"
    )
    aws_region: str = Field(
        default="ap-southeast-1", description="AWS region"
    )
    s3_bucket_name: str = Field(
        default="sribeesonline-assets",
        description="S3 bucket name for media uploads",
    )
    s3_endpoint_url: Optional[str] = Field(
        default=None,
        description="Custom S3 endpoint URL (for MinIO, DigitalOcean Spaces, etc.)",
    )
    s3_public_url_prefix: Optional[str] = Field(
        default=None,
        description=(
            "Public URL prefix for S3 objects. "
            "If None, defaults to https://{bucket}.s3.{region}.amazonaws.com"
        ),
    )
    s3_max_upload_size_mb: int = Field(
        default=50, description="Maximum upload size in megabytes"
    )
    s3_emulator_url_prefix: Optional[str] = Field(
        default=None,
        description=(
            "URL prefix reachable from the Android emulator (uses 10.0.2.2). "
            "When set, the /app/splash-config endpoint rewrites URLs for "
            "clients that send the X-Client-Platform: android-emulator header."
        ),
    )

    @property
    def s3_public_base_url(self) -> str:
        """Compute the public base URL for S3 objects."""
        if self.s3_public_url_prefix:
            return self.s3_public_url_prefix.rstrip("/")
        if self.s3_endpoint_url:
            return f"{self.s3_endpoint_url.rstrip('/')}/{self.s3_bucket_name}"
        return f"https://{self.s3_bucket_name}.s3.{self.aws_region}.amazonaws.com"

    # =========================================================================
    # OAuth Settings (Optional)
    # =========================================================================
    google_client_id: str = Field(default="", description="Google OAuth client ID")
    google_client_secret: str = Field(default="", description="Google OAuth client secret")
    google_callback_url: str = Field(default="", description="Google OAuth callback URL")

    facebook_app_id: str = Field(default="", description="Facebook App ID")
    facebook_app_secret: str = Field(default="", description="Facebook App Secret")
    facebook_callback_url: str = Field(default="", description="Facebook OAuth callback URL")


@lru_cache()
def get_settings() -> Settings:
    """
    Get cached settings instance.

    Uses LRU cache to ensure settings are loaded only once.
    """
    return Settings()


# Export settings instance
settings = get_settings()
