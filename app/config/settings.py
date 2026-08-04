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
        default=0.10,
        ge=0.0,
        le=1.0,
        description="Flat cashback rate earned on the order subtotal (0.10 = 10%)",
    )
    wallet_currency: str = Field(default="LKR", description="Wallet currency code")

    # =========================================================================
    # Order Pricing
    # =========================================================================
    flat_delivery_fee: float = Field(
        default=350.0,
        ge=0.0,
        description="Flat delivery fee applied to non-empty carts (LKR)",
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
    host: str = Field(default="0.0.0.0", description="Server host")
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
    jwt_access_token_expire_minutes: int = Field(default=15, description="Access token expiry (minutes)")
    jwt_refresh_token_expire_days: int = Field(default=7, description="Refresh token expiry (days)")

    # Backward compatibility with Express backend
    jwt_access_expiry: str = Field(default="15m", description="Access token expiry string")
    jwt_refresh_expiry: str = Field(default="7d", description="Refresh token expiry string")

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
    # Firebase Cloud Messaging (FCM) Settings
    # =========================================================================
    firebase_credentials_path: Optional[str] = Field(
        default=None,
        description="Path to Firebase service account JSON file"
    )
    fcm_server_key: Optional[str] = Field(
        default=None,
        description="FCM server key (deprecated, use service account)"
    )

    # Legacy Expo Push (deprecated - use FCM)
    expo_access_token: str = Field(default="", description="Expo push notification token (deprecated)")

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
    # Gemini AI / Semantic Search Settings
    # =========================================================================
    gemini_api_key: Optional[str] = Field(
        default=None,
        description="Google Gemini API key for embeddings"
    )
    gemini_embedding_model: str = Field(
        default="text-embedding-004",
        description="Gemini embedding model name"
    )
    gemini_embedding_dimension: int = Field(
        default=768,
        description="Embedding vector dimension"
    )

    # Semantic Search Configuration
    semantic_search_similarity_threshold: float = Field(
        default=0.35,
        description="Default minimum similarity threshold (0.0-1.0)"
    )
    semantic_search_max_results: int = Field(
        default=100,
        description="Maximum search results per query"
    )
    semantic_search_cache_ttl: int = Field(
        default=3600,
        description="Search results cache TTL in seconds (1 hour)"
    )
    embedding_cache_ttl: int = Field(
        default=86400,
        description="Embedding cache TTL in seconds (24 hours)"
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
