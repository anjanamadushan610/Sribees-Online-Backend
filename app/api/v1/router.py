"""
SRIBEESonline FastAPI Backend - API v1 Router

Main router that aggregates all API v1 endpoints.
"""
from fastapi import APIRouter

from app.api.v1 import (
    admin,
    admin_analytics,
    admin_assistant_briefings,
    admin_auth,
    admin_banners,
    admin_branch_categories,
    admin_branches,
    admin_campaigns,
    admin_cancellations,
    admin_catalog,
    admin_coupons,
    admin_courier,
    admin_customer_promos,
    admin_dashboard,
    admin_inventory,
    admin_location_rollout,
    admin_locations,
    admin_loyalty,
    admin_orders,
    admin_partners,
    admin_roles,
    admin_settings,
    admin_staff,
    admin_support,
    admin_transfers,
    admin_users,
    app_public,
    app_usage,
    auth,
    banners,
    branch,
    cart,
    categories,
    chat,
    coupons,
    courier_webhooks,
    delivery,
    locations,
    marketing,
    notifications,
    orders,
    partner,
    payments,
    products,
    referrals,
    reviews,
    session,
    support,
    user_addresses,
    wallet,
    wishlist,
)

# Create main v1 router
router = APIRouter(prefix="/v1")

# Include module routers
router.include_router(
    auth.router,
    prefix="/auth",
    tags=["Authentication"],
)

router.include_router(
    categories.router,
    prefix="/categories",
    tags=["Categories"],
)

router.include_router(
    products.router,
    prefix="/products",
    tags=["Products"],
)

# Reviews share the /products prefix (distinct sub-paths: /{id}/reviews).
router.include_router(
    reviews.router,
    prefix="/products",
    tags=["Reviews"],
)

router.include_router(
    cart.router,
    prefix="/cart",
    tags=["Cart"],
)

router.include_router(
    wishlist.router,
    prefix="/wishlist",
    tags=["Wishlist"],
)

router.include_router(
    coupons.router,
    prefix="/coupons",
    tags=["Coupons"],
)

# AI Shopping Chat (/chat) — Gemini-backed assistant over the real,
# branch-visible catalog. Works for guests too, same X-Device-Id convention
# as /products/search.
router.include_router(
    chat.router,
    prefix="/chat",
    tags=["AI Chat"],
)

# Admin AI Briefings (/admin/assistant-briefings) — what a branch tells the
# chat assistant for a while (a disruption, an event). briefings:* permission;
# a Branch Manager is pinned to their own branch.
router.include_router(
    admin_assistant_briefings.router,
    prefix="/admin/assistant-briefings",
    tags=["Admin AI Briefings"],
)

router.include_router(
    orders.router,
    prefix="/orders",
    tags=["Orders"],
)

router.include_router(
    wallet.router,
    prefix="/wallet",
    tags=["Wallet"],
)

router.include_router(
    payments.methods_router,
    prefix="/payment-methods",
    tags=["Payment Methods"],
)

router.include_router(
    payments.router,
    prefix="/payments",
    tags=["Payments"],
)

# Admin courier ops (/admin/courier) -- pickup-location registration, the
# reconciliation sweep an external cron drives (there is no scheduler in this
# backend), and one-time webhook-endpoint registration.
router.include_router(
    admin_courier.router,
    prefix="/admin/courier",
    tags=["Admin - Courier"],
)

# Courier webhooks (/courier/webhooks) -- inbound-only, SribeesExpress calls
# us. Authenticated by HMAC over the raw body (X-Sribees-Signature) using
# courier_webhook_secret, NOT by a shared api key -- see
# docs/SRIBEESEXPRESS_ECOMMERCE_API.md section 10.3.
router.include_router(
    courier_webhooks.router,
    prefix="/courier/webhooks",
    tags=["Courier Webhooks"],
)

router.include_router(
    notifications.router,
    prefix="/notifications",
    tags=["Notifications"],
)

router.include_router(
    branch.router,
    prefix="/branch",
    tags=["Branch Routing"],
)

# Delivery slots (/delivery/slots) — the dispatch rounds a customer can order
# into, from branches.delivery_runs. This path has been listed in main.py's
# route index since long before anything served it.
router.include_router(
    delivery.router,
    prefix="/delivery",
    tags=["Delivery"],
)

router.include_router(
    locations.router,
    prefix="/locations",
    tags=["Location Discovery"],
)

router.include_router(
    user_addresses.router,
    prefix="/user/addresses",
    tags=["User Addresses"],
)

# Hyper-local session location (set/get the active delivery branch).
router.include_router(
    session.router,
    prefix="/session",
    tags=["Session"],
)

router.include_router(
    admin.router,
    prefix="/admin",
    tags=["Admin"],
)

router.include_router(
    admin_auth.router,
    prefix="/admin/auth",
    tags=["Admin Authentication"],
)

# Admin Global Catalog management (/admin/categories, /admin/products).
# Restricted to super_admin + inventory_manager inside the router.
router.include_router(
    admin_catalog.router,
    prefix="/admin",
    tags=["Admin Catalog"],
)

# Admin Branch Inventory management (/admin/inventory). Branch-scoped via
# inject_branch_filter; restricted to super_admin/branch_manager/inventory_manager.
router.include_router(
    admin_inventory.router,
    prefix="/admin/inventory",
    tags=["Admin Inventory"],
)

# Admin Stock Transfers (/admin/transfers). A scoped admin sees/acts on
# transfers where their branch is either party; restricted to
# super_admin/branch_manager/inventory_manager (approve/reject/ship/complete
# further restricted to super_admin/inventory_manager inside the router).
router.include_router(
    admin_transfers.router,
    prefix="/admin/transfers",
    tags=["Admin Stock Transfers"],
)

# Admin Branch management (/admin/branches) — Super Admin only, except
# listing (scoped) and status-toggle which Branch Managers may also reach.
router.include_router(
    admin_branches.router,
    prefix="/admin/branches",
    tags=["Admin Branches"],
)

# Admin Branch Category visibility (/admin/branch-categories). Branch-scoped
# via inject_branch_filter; restricted to super_admin/branch_manager.
router.include_router(
    admin_branch_categories.router,
    prefix="/admin/branch-categories",
    tags=["Admin Branch Categories"],
)

# Admin User management (/admin/users) — Super Admin only.
router.include_router(
    admin_users.router,
    prefix="/admin/users",
    tags=["Admin Users"],
)

# Admin Role management (/admin/roles) — any authenticated admin can read the
# catalog; defining a role is restricted to base-role admins (require_base_role_admin).
router.include_router(
    admin_roles.router,
    prefix="/admin/roles",
    tags=["Admin Roles"],
)

# Admin Staff management (/admin/staff) — any base-role admin manages their
# own delegated staff. Separate from /admin/users (Super-Admin-only, 5 base roles).
router.include_router(
    admin_staff.router,
    prefix="/admin/staff",
    tags=["Admin Staff"],
)

# Admin Dashboard (/admin/dashboard) — the generic staff-role landing page's
# stats. Gated purely on dashboard:read, with each metric further gated on
# the specific permission it represents (see admin_dashboard.py) — separate
# from /admin/analytics, which stays super_admin/branch_manager only.
router.include_router(
    admin_dashboard.router,
    prefix="/admin/dashboard",
    tags=["Admin Dashboard"],
)

# Admin Partner management (/admin/partners) — Super Admin only. CRUD for the
# professional referral team plus commission-ledger visibility and
# out-of-band settlement tracking. Replaces the raw DB inserts the seed data
# used to require.
router.include_router(
    admin_partners.router,
    prefix="/admin/partners",
    tags=["Admin Partners"],
)

# Admin Order management (/admin/orders). Branch-scoped via inject_branch_filter;
# restricted to super_admin/branch_manager/customer_support.
router.include_router(
    admin_cancellations.router,
    prefix="/admin",
    tags=["Admin Cancellations"],
)

router.include_router(
    admin_orders.router,
    prefix="/admin/orders",
    tags=["Admin Orders"],
)

# Admin Analytics (/admin/analytics). Branch-scoped via inject_branch_filter;
# restricted to super_admin/branch_manager.
router.include_router(
    admin_analytics.router,
    prefix="/admin/analytics",
    tags=["Admin Analytics"],
)

# Admin 1-to-1 retention promos (/admin/customers/{id}/promo). Mounted on the
# customer path rather than under /admin/coupons because it is a customer
# action, not a campaign one: it issues a coupon nobody else can ever redeem.
# Gated on marketing:create + the same branch isolation as the directory.
router.include_router(
    admin_customer_promos.router,
    prefix="/admin/customers",
    tags=["Admin Customer Promos"],
)

# Admin Coupons (/admin/coupons) — Promotions management.
# Restricted to super_admin/marketing_manager.
router.include_router(
    admin_coupons.router,
    prefix="/admin/coupons",
    tags=["Admin Coupons"],
)

router.include_router(
    admin_locations.router,
    prefix="/admin/locations",
    tags=["Admin Location Management"],
)

router.include_router(
    admin_location_rollout.router,
    prefix="/admin/service-areas",
    tags=["Admin Service Areas"],
)

router.include_router(
    admin_settings.router,
    prefix="/admin/settings",
    tags=["Admin Settings"],
)

# Admin Loyalty (/admin/loyalty) — levels and a customer's points. Levels are
# read by marketing (members-only coupons), changed only by a Super Admin.
router.include_router(
    admin_loyalty.router,
    prefix="/admin/loyalty",
    tags=["Admin Loyalty"],
)

router.include_router(
    marketing.router,
    prefix="/admin/marketing",
    tags=["Marketing Management"],
)

# Admin Push Campaigns (/admin/notification-campaigns) — marketing pushes to
# customers in their language. marketing:read to view, marketing:create to
# send; a branch's campaign reaches only the customers that branch served.
router.include_router(
    admin_campaigns.router,
    prefix="/admin/notification-campaigns",
    tags=["Admin Push Campaigns"],
)

# Admin Banner management (/admin/banners). Branch-scoped via inject_branch_filter;
# restricted to super_admin/marketing_manager.
router.include_router(
    admin_banners.router,
    prefix="/admin/banners",
    tags=["Admin Banners"],
)

# Public home-screen banner carousel (/banners) — branch-scoped, guest-friendly.
router.include_router(
    banners.router,
    prefix="/banners",
    tags=["Banners"],
)

# Referrals (/referrals) — invite codes, commission ledger, wallet payout.
# Every route is scoped to the authenticated user; no beneficiary id is ever
# accepted from the client.
router.include_router(
    referrals.router,
    prefix="/referrals",
    tags=["Referrals"],
)

# Partner portal (/partner) — the professional referral team.
# A SEPARATE auth realm: every route requires the `is_partner` token claim,
# which /admin/* refuses and which admin/customer tokens do not carry. Served
# by its own frontend so the two never share an origin or a session.
router.include_router(
    partner.router,
    prefix="/partner",
    tags=["Partner Portal"],
)

router.include_router(
    app_public.router,
    prefix="/app",
    tags=["App Configuration"],
)

# App presence: the app's heartbeat (/app/usage/heartbeat, guests too) and the
# Super Admin "App Users" page (/admin/app-usage).
router.include_router(
    app_usage.router,
    prefix="/app",
    tags=["App Usage"],
)
router.include_router(
    app_usage.admin_router,
    prefix="/admin/app-usage",
    tags=["Admin App Usage"],
)

# Customer support tickets (/support)
router.include_router(
    support.router,
    tags=["Customer Support"],
)

# Admin support ticket management (/admin/support)
router.include_router(
    admin_support.router,
    prefix="/admin/support",
    tags=["Admin Support Tickets"],
)
