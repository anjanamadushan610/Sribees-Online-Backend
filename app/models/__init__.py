"""
SRIBEESonline FastAPI Backend - Models Module

SQLAlchemy ORM models for all database tables.
"""
from app.models.admin import Admin, AdminRole, AdminSession
from app.models.app_settings import AppSetting
from app.models.banner import Banner, BannerEvent, BannerEventType
from app.models.branch import Branch, PostOfficeBranchMapping, PostOfficeDirectory
from app.models.category import Category
from app.models.coupon import (
    Coupon,
    CouponCategory,
    CouponDiscountType,
    CouponProduct,
)
from app.models.escalation import (
    EscalationCategory,
    EscalationStatus,
    OrderEscalation,
)
from app.models.notification import Notification, NotificationType, PushToken
from app.models.order import Order, OrderItem, OrderStatus, PaymentStatus
from app.models.partner import Partner
from app.models.product import (
    BranchInventory,
    Product,
    ProductImage,
    ProductVariant,
    Review,
    VariantOption,
    VariantType,
)
from app.models.referral import ReferralCommission, ReferralCommissionStatus
from app.models.role import Permission, Role, RolePermission
from app.models.stock_transfer import StockTransfer, TransferStatus
from app.models.user import (
    Address,
    EmailVerification,
    PasswordReset,
    Session,
    User,
)
from app.models.wallet import Wallet, WalletTransaction, WalletTransactionType
from app.models.wishlist import WishlistItem

__all__ = [
    # User models
    "User",
    "EmailVerification",
    "PasswordReset",
    "Session",
    "Address",
    # Banner
    "Banner",
    "BannerEvent",
    "BannerEventType",
    # Referrals
    "Partner",
    "ReferralCommission",
    "ReferralCommissionStatus",
    # Category
    "Category",
    # Coupon
    "Coupon",
    "CouponCategory",
    "CouponProduct",
    "CouponDiscountType",
    # Order escalations (B4)
    "OrderEscalation",
    "EscalationCategory",
    "EscalationStatus",
    # Product models
    "BranchInventory",
    "Product",
    "ProductImage",
    "ProductVariant",
    "VariantType",
    "VariantOption",
    "Review",
    # Wishlist
    "WishlistItem",
    # Order models
    "Order",
    "OrderItem",
    "OrderStatus",
    "PaymentStatus",
    # Wallet models
    "Wallet",
    "WalletTransaction",
    "WalletTransactionType",
    # Notification models
    "Notification",
    "PushToken",
    "NotificationType",
    # Admin & RBAC models
    "Admin",
    "AdminSession",
    "AdminRole",
    "Role",
    "Permission",
    "RolePermission",
    # Branch models
    "Branch",
    "PostOfficeBranchMapping",
    "PostOfficeDirectory",
    # App settings
    "AppSetting",
    # Stock transfers
    "StockTransfer",
    "TransferStatus",
]
