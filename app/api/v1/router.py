"""
SRIBEESonline FastAPI Backend - API v1 Router

Main router that aggregates all API v1 endpoints.
"""
from fastapi import APIRouter

from app.api.v1 import (
    admin,
    admin_analytics,
    admin_auth,
    admin_branches,
    admin_catalog,
    admin_coupons,
    admin_inventory,
    admin_locations,
    admin_orders,
    admin_settings,
    admin_users,
    app_public,
    auth,
    branch,
    cart,
    categories,
    locations,
    marketing,
    notifications,
    orders,
    payments,
    products,
    reviews,
    search,
    session,
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
    search.router,
    prefix="/search",
    tags=["Search"],
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

# Admin Branch management (/admin/branches) — Super Admin only.
router.include_router(
    admin_branches.router,
    prefix="/admin/branches",
    tags=["Admin Branches"],
)

# Admin User management (/admin/users) — Super Admin only.
router.include_router(
    admin_users.router,
    prefix="/admin/users",
    tags=["Admin Users"],
)

# Admin Order management (/admin/orders). Branch-scoped via inject_branch_filter;
# restricted to super_admin/branch_manager/customer_support.
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
    admin_settings.router,
    prefix="/admin/settings",
    tags=["Admin Settings"],
)

router.include_router(
    marketing.router,
    prefix="/admin/marketing",
    tags=["Marketing Management"],
)

router.include_router(
    app_public.router,
    prefix="/app",
    tags=["App Configuration"],
)
