"""
SRIBEESonline FastAPI Backend - Models Module

SQLAlchemy ORM models for all database tables.
"""

from app.models.admin import Admin, AdminRole, AdminSession
from app.models.app_content import AppContent
from app.models.app_settings import AppSetting
from app.models.app_usage import AppInstall, AppUsageDay
from app.models.assistant import AssistantProfile, AssistantReceipt, AssistantReminder
from app.models.banner import Banner, BannerEvent, BannerEventType
from app.models.branch import Branch, PostalCityBranchMapping, PostalCityDirectory
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
from app.models.finance_outbox import FinanceOutbox, FinanceStockLevel
from app.models.location import LocationDistrict, LocationProvince
from app.models.loyalty import (
    LoyaltyAccount,
    LoyaltyTier,
    LoyaltyTierReward,
    OrderReward,
    OrderRewardStatus,
)
from app.models.notification import (
    Notification,
    NotificationCampaign,
    NotificationType,
    PushToken,
)
from app.models.order import Order, OrderItem, OrderStatus, PaymentStatus
from app.models.partner import Partner
from app.models.partner_wallet import (
    PartnerApplication,
    PartnerWallet,
    PartnerWalletEntry,
    PartnerWithdrawal,
)
from app.models.payment import PaymentAttempt, PaymentAttemptStatus
from app.models.product import (
    BranchInventory,
    BranchVariantPrice,
    Product,
    ProductExtraCategory,
    ProductImage,
    ProductVariant,
    Review,
    VariantOption,
    VariantType,
)
from app.models.referral import (
    ReferralCommission,
    ReferralCommissionLine,
    ReferralCommissionStatus,
    ReferralRateChange,
)
from app.models.referral_promotion import (
    ReferralBenefit,
    ReferralDeliveryRedemption,
    ReferralPromotion,
)
from app.models.role import Permission, Role, RolePermission
from app.models.search_miss import SearchMiss, SearchMissStatus
from app.models.stock_transfer import StockTransfer, TransferStatus
from app.models.support_ticket import SupportTicket, TicketStatus
from app.models.user import (
    Address,
    EmailVerification,
    PasswordReset,
    Session,
    User,
    UserPhoneChangeHistory,
)
from app.models.wallet import Wallet, WalletTransaction, WalletTransactionType
from app.models.wishlist import WishlistItem

__all__ = [
    "ReferralBenefit", "ReferralDeliveryRedemption", "ReferralPromotion",
    "AssistantProfile",
    "AssistantReminder",
    "AssistantReceipt",
    # User models
    "User",
    "UserPhoneChangeHistory",
    "EmailVerification",
    "PasswordReset",
    "Session",
    "Address",
    # Banner
    "Banner",
    "BannerEvent",
    "BannerEventType",
    # Sribees Finance event feed
    "FinanceOutbox",
    "FinanceStockLevel",
    # Online payment tries
    "PaymentAttempt",
    "PaymentAttemptStatus",
    # Referrals
    "Partner",
    "PartnerApplication",
    "PartnerWallet",
    "PartnerWalletEntry",
    "PartnerWithdrawal",
    "ReferralCommission",
    "ReferralCommissionLine",
    # Loyalty
    "LoyaltyAccount",
    "LoyaltyTier",
    "LoyaltyTierReward",
    "OrderReward",
    "OrderRewardStatus",
    "SearchMiss",
    "SearchMissStatus",
    "ReferralCommissionStatus",
    "ReferralRateChange",
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
    "BranchVariantPrice",
    "Product",
    "ProductExtraCategory",
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
    "NotificationCampaign",
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
    "PostalCityBranchMapping",
    "PostalCityDirectory",
    "LocationProvince",
    "LocationDistrict",
    # App settings
    "AppSetting",
    "AppContent",
    "AppInstall",
    "AppUsageDay",
    # Stock transfers
    "StockTransfer",
    "TransferStatus",
    # Support tickets
    "SupportTicket",
    "TicketStatus",
]
