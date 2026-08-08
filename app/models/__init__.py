"""
SRIBEESonline FastAPI Backend - Models Module

SQLAlchemy ORM models for all database tables.
"""
from app.models.admin import Admin, AdminRole, AdminSession
from app.models.app_settings import AppSetting
from app.models.banner import Banner
from app.models.branch import Branch, PostOfficeBranchMapping, PostOfficeDirectory
from app.models.category import Category
from app.models.coupon import Coupon, CouponDiscountType
from app.models.notification import Notification, NotificationType, PushToken
from app.models.order import Order, OrderItem, OrderStatus, PaymentStatus
from app.models.referral import ReferralCommission, ReferralCommissionStatus
from app.models.product import (
    BranchInventory,
    Product,
    ProductImage,
    ProductVariant,
    Review,
    VariantOption,
    VariantType,
)
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
    # Referrals
    "ReferralCommission",
    "ReferralCommissionStatus",
    # Category
    "Category",
    # Coupon
    "Coupon",
    "CouponDiscountType",
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
    # Branch models
    "Branch",
    "PostOfficeBranchMapping",
    "PostOfficeDirectory",
    # App settings
    "AppSetting",
]
