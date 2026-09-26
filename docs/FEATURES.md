# SRIBEESonline - Complete Features Documentation

> **Document Status**: As-Built Implementation (February 2026)  
> **Last Updated**: February 2026
> **Tech Stack**: FastAPI (Backend) | Flutter (Mobile) | React (Admin)

> Detailed breakdown of all features organized by EPIC. This document reflects the actual implemented state of the SRIBEESonline platform.

---

## 📑 Table of Contents

- [EPIC 1: User Authentication & Account Management](#epic-1-user-authentication--account-management)
- [EPIC 2: Product Catalog & Search](#epic-2-product-catalog--search)
- [EPIC 3: Shopping Cart & Wishlist](#epic-3-shopping-cart--wishlist)
- [EPIC 4: Checkout & Payment](#epic-4-checkout--payment)
- [EPIC 5: Order Management](#epic-5-order-management)
- [EPIC 6: Reviews & Ratings](#epic-6-reviews--ratings)
- [EPIC 7: Admin Dashboard](#epic-7-admin-dashboard)
- [EPIC 8: Notifications & Communication](#epic-8-notifications--communication)
- [EPIC 9: Advanced Watchlist Management](#epic-9-advanced-watchlist-management)
- [EPIC 10: Location-Based Branch Routing](#epic-10-location-based-branch-routing)
- [EPIC 11: Infrastructure & DevOps](#epic-11-infrastructure--devops)

---

## EPIC 1: User Authentication & Account Management

> **Customer sign-in (as built, 2026-09-25):** customers sign in with a phone OTP,
> Google or Apple only. Email/password registration, email verification, password
> reset and customer password change were removed from the API and the app; the
> email/password stories below are the original plan and are not implemented.
> Staff (`/admin/auth/*`) and partners (`/partner/auth/login`) keep password logins.

### Feature 1.1: User Registration

**Description**: Allow new users to create an account

**Functional Requirements**:
- Email and password registration form
- Email validation (format and uniqueness)
- Password strength validation (min 8 chars, uppercase, lowercase, number, special char)
- Terms and conditions acceptance checkbox
- Email verification link sent after registration
- Account activation via email link

**UI Components**:
- Registration form with fields: Email, Password, Confirm Password, Full Name
- Password strength indicator
- Email verification success/error messages
- Resend verification email option

**API Endpoints**:
```
POST /api/v1/auth/register
POST /api/v1/auth/verify-email
POST /api/v1/auth/resend-verification
```

**Database Tables**:
- `users` (user_id, email, password_hash, full_name, is_verified, created_at)
- `email_verifications` (token, user_id, expires_at)

**Validation Rules**:
- Email: Valid format, unique, max 255 chars
- Password: Min 8 chars, 1 uppercase, 1 lowercase, 1 number, 1 special char
- Full Name: Required, 2-100 chars

---

### Feature 1.2: User Login

**Description**: Secure user authentication

**Functional Requirements**:
- Email and password login
- "Remember me" option (extended session)
- Account lockout after 5 failed attempts (15 min)
- JWT token generation (access + refresh tokens)
- Session management with Redis

**UI Components**:
- Login form (Email, Password fields)
- "Remember me" checkbox
- "Forgot password?" link
- Error messages for invalid credentials
- Account locked message

**API Endpoints**:
```
POST /api/v1/auth/login
POST /api/v1/auth/logout
POST /api/v1/auth/refresh-token
```

**Security Features**:
- Argon2 password hashing
- JWT access token (15 min expiry)
- JWT refresh token (7 days expiry, 30 days with Remember Me)
- Rate limiting (5 attempts per 15 min)
- HTTPS only cookies
- **Redis Session Management** (As-Built):
  - Session key: `sessions:{userId}:{sessionId}`
  - Token blacklist: `blacklist:token:{jti}`
  - User-wide invalidation: `blacklist:user:{userId}`
  - Circuit breaker pattern for PostgreSQL fallback

---

### Feature 1.3: Social Login

**Description**: Login with Google/Facebook accounts

**Functional Requirements**:
- Google OAuth 2.0 integration
- Facebook OAuth integration
- Auto-create account on first social login
- Link social accounts to existing accounts

**UI Components**:
- "Continue with Google" button
- "Continue with Facebook" button
- Social login success/error messages

**API Endpoints**:
```
GET /api/v1/auth/google
GET /api/v1/auth/google/callback
GET /api/v1/auth/facebook
GET /api/v1/auth/facebook/callback
```

**Database Tables**:
- `social_accounts` (provider, provider_id, user_id, access_token)

---

### Feature 1.4: Password Reset

**Description**: Allow users to reset forgotten passwords

**Functional Requirements**:
- Request password reset via email
- Generate secure reset token (1-hour expiry)
- Send reset link via email
- Validate reset token
- Set new password
- Invalidate all existing sessions after reset

**UI Components**:
- Forgot password form (Email field)
- Reset password form (New Password, Confirm Password)
- Success/error messages
- Link expiry message

**API Endpoints**:
```
POST /api/v1/auth/forgot-password
POST /api/v1/auth/reset-password
GET /api/v1/auth/validate-reset-token
```

**Database Tables**:
- `password_resets` (token, user_id, expires_at, used)

---

### Feature 1.5: Profile Management

**Description**: Update user profile information

**Functional Requirements**:
- View current profile details
- Update full name, phone number
- Upload profile picture
- Change password (requires current password)
- Delete account (with confirmation)

**UI Components**:
- Profile view page
- Profile edit form
- Profile picture upload with preview
- Change password form
- Delete account confirmation modal

**API Endpoints**:
```
GET /api/v1/users/profile
PUT /api/v1/users/profile
POST /api/v1/users/profile/picture
PUT /api/v1/users/change-password
DELETE /api/v1/users/account
```

**Database Tables**:
- `users` (phone, profile_picture_url, updated_at)

---

### Feature 1.6: Address Management

**Description**: Manage multiple delivery addresses

**Functional Requirements**:
- Add new delivery address
- Edit existing addresses
- Delete addresses
- Set default address
- Address validation
- Support for multiple address types (Home, Work, Other)

**UI Components**:
- Address list view
- Add/Edit address form
- Delete confirmation modal
- Set as default option
- Address type selector

**API Endpoints**:
```
GET /api/v1/users/addresses
POST /api/v1/users/addresses
PUT /api/v1/users/addresses/:id
DELETE /api/v1/users/addresses/:id
PUT /api/v1/users/addresses/:id/set-default
```

**Database Tables**:
- `addresses` (address_id, user_id, type, address_line1, address_line2, city, state, postal_code, country, is_default)

**Validation Rules**:
- Address Line 1: Required, max 255 chars
- City: Required, max 100 chars
- Postal Code: Required, valid format
- Phone: Valid format

---

### Feature 1.7: Two-Factor Authentication (2FA)

**Description**: Enhanced account security with 2FA

**Functional Requirements**:
- Enable/disable 2FA
- QR code generation for authenticator apps
- Backup codes generation (10 codes)
- 2FA verification during login
- Recovery options

**UI Components**:
- 2FA setup wizard
- QR code display
- Backup codes display and download
- 2FA verification input (6-digit code)
- Disable 2FA confirmation

**API Endpoints**:
```
POST /api/v1/users/2fa/enable
POST /api/v1/users/2fa/verify
POST /api/v1/users/2fa/disable
GET /api/v1/users/2fa/backup-codes
```

**Database Tables**:
- `users` (two_factor_secret, two_factor_enabled)
- `backup_codes` (code_hash, user_id, used)

---

## EPIC 2: Product Catalog & Search

### Feature 2.1: Category Management

**Description**: Hierarchical product category system

**Functional Requirements**:
- Multi-level category hierarchy (parent-child)
- Category listing with product counts
- Category images and descriptions
- Category-based navigation
- Breadcrumb navigation

**UI Components**:
- Category navigation menu (sidebar/header)
- Category cards with images
- Subcategory listings
- Breadcrumb trail
- Category filters

**API Endpoints**:
```
GET /api/v1/categories
GET /api/v1/categories/:id
GET /api/v1/categories/:id/subcategories
GET /api/v1/categories/:id/products
```

**Database Schema (MongoDB)**:
```javascript
categories: {
  _id, name, slug, parent_id, image_url,
  description, display_order, is_active
}
```

---

### Feature 2.2: Product Listing

**Description**: Display products with filtering and sorting

**Functional Requirements**:
- Grid/List view toggle
- Pagination (20 products per page)
- Filter by: price range, brand, rating, availability
- Sort by: relevance, price (low-high, high-low), popularity, rating, newest
- Quick view modal
- Add to cart from listing

**UI Components**:
- Product grid/list view
- Filter sidebar
- Sort dropdown
- Pagination controls
- Quick view modal
- Product cards (image, name, price, rating, add to cart)

**API Endpoints**:
```
GET /api/v1/products?page=1&limit=20&sort=price_asc&filter[price_min]=10&filter[price_max]=100
GET /api/v1/products/:id/quick-view
```

**Query Parameters**:
- `page`: Page number
- `limit`: Items per page
- `sort`: price_asc, price_desc, rating, popularity, newest
- `filter[price_min]`, `filter[price_max]`
- `filter[brand]`
- `filter[rating]`: 1-5
- `filter[in_stock]`: true/false

---

### Feature 2.3: Product Details Page

**Description**: Comprehensive product information

**Functional Requirements**:
- Product image gallery with zoom
- Product name, SKU, brand
- Current price and discount price
- Stock availability status
- Product description (short and detailed)
- Product specifications/attributes
- Customer reviews and ratings
- Related products
- Add to cart/wishlist buttons
- Quantity selector
- Share product (social media)

**UI Components**:
- Image gallery with thumbnails
- Zoom on hover/click
- Price display with discount badge
- Stock status indicator
- Tabs: Description, Specifications, Reviews
- Related products carousel
- Quantity selector
- Add to cart/wishlist buttons
- Share buttons

**API Endpoints**:
```
GET /api/v1/products/:id
GET /api/v1/products/:id/related
GET /api/v1/products/:id/reviews
```

**Database Schema (MongoDB)**:
```javascript
products: {
  _id, sku, name, slug, brand, category_id,
  price, discount_price, stock_quantity,
  description_short, description_long,
  images: [], attributes: {},
  rating_average, review_count,
  is_active, created_at, updated_at
}
```

---

### Feature 2.4: Product Search

**Description**: AI-powered multilingual semantic product search

**Functional Requirements**:
- **Semantic search** using Gemini text-embedding-004 (768 dimensions)
- **Multilingual support**: English, Sinhala (සිංහල), Tamil (தமிழ்), Singlish
- Full-text search across product name, description, brand
- Search autocomplete/suggestions based on popular queries
- Search history (logged-in users)
- Automatic fallback to keyword search if AI unavailable
- Search results highlighting
- Filter search results
- Search analytics tracking
- **Circuit breaker pattern** for AI service resilience

**UI Components**:
- Search bar with autocomplete dropdown
- Search results page with similarity scores
- Search filters sidebar
- "No results" message with suggestions
- Search history dropdown
- Language indicator (detected query language)

**API Endpoints** (as built, 2026-09-26):
```
GET /api/v1/products/search?q=...      # The app's search: keyword index + typo matching
```

> The Gemini/pgvector semantic search (`POST /api/v1/search` and its
> `/suggestions`, `/popular` and `/health` routes) was never called by the app
> or the admin panel, and the SQL functions it needed never reached
> production. It was removed on 2026-09-26. The design below is kept as the
> original plan only.

**Search Request Schema**:
```json
{
  "query": "රතු ඇපල් ගෙඩි",
  "filters": {
    "category_id": "uuid",
    "min_price": 0,
    "max_price": 500,
    "in_stock_only": true,
    "similarity_threshold": 0.35
  },
  "pagination": {
    "page": 1,
    "page_size": 20
  }
}
```

**Database (PostgreSQL with pgvector)**:
```sql
-- Products table with embedding
products: {
  product_id, sku, name, slug, category_id,
  price, description, short_description,
  embedding VECTOR(768),        -- Gemini embedding
  embedding_updated_at,
  search_text                   -- Auto-generated
}

-- Multilingual translations
product_translations: {
  product_id, language_code,    -- 'si', 'ta', 'si_lk'
  name, description, keywords,
  embedding VECTOR(768)
}

-- Search analytics
search_analytics: {
  query, normalized_query, language_detected,
  search_type, total_results, response_time_ms
}
```

**Redis Cache Patterns**:
```
search:embedding:{hash}   -> Cached query embedding (24h TTL)
search:results:{hash}     -> Cached search results (1h TTL)
search:popular            -> Sorted set of popular queries
search:suggestions:{prefix} -> Autocomplete cache (15m TTL)
```

---

### Feature 2.5: Product Filters

**Description**: Advanced filtering options

**Functional Requirements**:
- Price range slider
- Brand checkbox filters
- Rating filters (4+ stars, 3+ stars, etc.)
- Availability filter (In Stock, Out of Stock)
- Category filters
- Attribute filters (organic, gluten-free, etc.)
- Clear all filters option
- Active filters display

**UI Components**:
- Filter sidebar/panel
- Price range slider
- Checkbox groups for brands
- Star rating filters
- Active filters chips with remove option
- Clear all button

**API Endpoints**:
```
GET /api/v1/filters/brands?category_id=123
GET /api/v1/filters/price-range?category_id=123
GET /api/v1/filters/attributes?category_id=123
```

---

### Feature 2.6: Product Recommendations

**Description**: Personalized product suggestions

**Functional Requirements**:
- "Frequently bought together" recommendations
- "Customers also viewed" suggestions
- "Similar products" based on category/attributes
- Personalized recommendations (based on browsing/purchase history)
- Trending products
- New arrivals

**UI Components**:
- Recommendation carousels
- Product cards in recommendations
- "Add all to cart" for bundles

**API Endpoints**:
```
GET /api/v1/products/:id/frequently-bought-together
GET /api/v1/products/:id/similar
GET /api/v1/recommendations/personalized
GET /api/v1/products/trending
GET /api/v1/products/new-arrivals
```

---

### Feature 2.7: Multi-Image Gallery

**Description**: Support for multiple product images with gallery view

**Functional Requirements**:
- Products support up to 5 images
- Primary image used as thumbnail in listings
- Full gallery loads only on Product Details page (performance)
- Carousel navigation with swipe support
- Thumbnail strip for quick navigation
- Image counter display (1/5, 2/5, etc.)
- Support for various image formats

**UI Components**:
- Horizontal scrolling image carousel (FlatList)
- Pagination dots below carousel
- Clickable thumbnail strip
- Image counter badge (top-right)
- Placeholder for products without images

**Database Schema (PostgreSQL)**:
```sql
product_images: {
  image_id UUID PRIMARY KEY,
  product_id UUID REFERENCES products,
  image_url VARCHAR(500) NOT NULL,
  alt_text VARCHAR(255),
  is_primary BOOLEAN DEFAULT false,
  sort_order INTEGER DEFAULT 0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
}
-- Constraint: Only one is_primary per product
-- Index on (product_id, sort_order) for ordered retrieval
```

**Mobile Implementation**:
- `ImageGallery` component with FlatList horizontal scroll
- `pagingEnabled` for snap-to-image scrolling
- Thumbnail strip with active indicator
- Optimized rendering with `keyExtractor`

**API Response**:
```json
{
  "product": {
    "images": [
      {
        "image_id": "uuid",
        "image_url": "https://...",
        "alt_text": "Product front view",
        "is_primary": true,
        "sort_order": 0
      }
    ]
  }
}
```

---

### Feature 2.8: Product Variants

**Description**: Support for product variations (size, color, weight, etc.)

**Functional Requirements**:
- Products can have multiple variant types (e.g., Size, Color, Weight)
- Each variant type has configurable options
- Variants have own price, stock, SKU
- Dynamic price updates when variant selected
- Stock tracked per variant
- Unavailable options shown as disabled
- Color swatches for color-type variants
- Variant selection required before add-to-cart

**UI Components**:
- `VariantSelector` component
- Chip/button style for text options (S, M, L, XL)
- Color swatches for color options
- Strikethrough for out-of-stock options
- Selected state highlighting (green border)
- Price update on selection
- Stock status per variant
- SKU display for selected variant

**Database Schema (PostgreSQL)**:
```sql
-- Variant types (Size, Color, Weight, etc.)
variant_types: {
  variant_type_id UUID PRIMARY KEY,
  name VARCHAR(100) UNIQUE NOT NULL,
  display_name VARCHAR(100) NOT NULL,
  display_order INTEGER DEFAULT 0,
  is_active BOOLEAN DEFAULT true
}

-- Options for each variant type
variant_options: {
  variant_option_id UUID PRIMARY KEY,
  variant_type_id UUID REFERENCES variant_types,
  value VARCHAR(100) NOT NULL,
  display_value VARCHAR(100),
  color_hex CHAR(7), -- For color swatches (#FF0000)
  display_order INTEGER DEFAULT 0,
  is_active BOOLEAN DEFAULT true
}

-- Product-specific variants
product_variants: {
  variant_id UUID PRIMARY KEY,
  product_id UUID REFERENCES products,
  sku VARCHAR(100) UNIQUE,
  name VARCHAR(255),
  price DECIMAL(10,2) NOT NULL,
  compare_at_price DECIMAL(10,2),
  stock_quantity INTEGER DEFAULT 0,
  image_url VARCHAR(500),
  is_default BOOLEAN DEFAULT false,
  is_active BOOLEAN DEFAULT true
}

-- Mapping variants to their options
product_variant_options: {
  variant_id UUID REFERENCES product_variants,
  variant_type_id UUID REFERENCES variant_types,
  variant_option_id UUID REFERENCES variant_options,
  PRIMARY KEY (variant_id, variant_type_id)
}
```

**Variant Types (Seeded)**:
- Size: XS, S, M, L, XL, XXL
- Weight: 100g, 250g, 500g, 1kg, 2kg, 5kg
- Pack Size: Single, 2-Pack, 6-Pack, 12-Pack
- Color: Custom per product with hex codes

**API Response**:
```json
{
  "product": {
    "has_variants": true,
    "variant_types": [
      {
        "variant_type_id": "uuid",
        "name": "size",
        "display_name": "Size",
        "options": [
          { "variant_option_id": "uuid", "value": "S", "display_value": "Small" }
        ]
      }
    ],
    "variants": [
      {
        "variant_id": "uuid",
        "sku": "PROD-001-S",
        "price": 29.99,
        "stock_quantity": 50,
        "is_default": true,
        "options": [
          { "variant_type_id": "uuid", "option_value": "S" }
        ]
      }
    ]
  }
}
```

**Frontend Logic**:
1. Initialize with default variant (if exists)
2. Track selections per variant type
3. Find matching variant when all options selected
4. Update price, stock, image based on selected variant
5. Disable add-to-cart if no matching variant

---

## EPIC 3: Shopping Cart & Wishlist

### Feature 3.1: Add to Cart

**Description**: Add products to shopping cart with Redis-backed performance

**Functional Requirements**:
- Add product with quantity
- Stock validation before adding
- Update quantity if product already in cart
- Success notification
- Mini cart preview
- Guest cart (session-based)
- Logged-in cart (persistent)
- Variant-aware cart items

**UI Components**:
- Add to cart button
- Quantity selector
- Success toast notification
- Mini cart dropdown
- Out of stock message

**API Endpoints**:
```
POST /api/v1/cart/items
GET /api/v1/cart
```

**Request Body**:
```json
{
  "productId": "uuid",
  "quantity": 2,
  "name": "Product Name",
  "price": 29.99,
  "image": "https://...",
  "sku": "SKU-001",
  "stock": 100,
  "variantId": "uuid (optional)"
}
```

**Redis Storage Architecture**:
```
Key: cart:{userId}
Type: Redis Hash
TTL: 30 days (2592000 seconds)

Structure:
cart:user-123 → {
  "product-id-1": '{"productId":"...", "quantity":2, "price":29.99, "name":"...", "image":"...", "sku":"...", "addedAt":"timestamp"}',
  "product-id-2": '{"productId":"...", "quantity":1, ...}'
}
```

**Cart Data Flow**:
1. **Active Cart (Primary: Redis)**
   - All read/write operations hit Redis first
   - Low latency for cart operations
   - Atomic operations with HGET/HSET/HINCRBY

2. **PostgreSQL Sync (Background)**
   - Cart synced to PostgreSQL during checkout
   - Provides durability for order history
   - Used for analytics and reporting

3. **Frontend Data Transformation**
   - Backend returns flat item structure
   - Frontend transforms to nested Product structure
   - Supports both product_id and _id formats

**Implementation Details**:
```typescript
// Backend cart item format
interface CartItemBackend {
  productId: string;
  quantity: number;
  price: number;
  name: string;
  image: string;
  sku: string;
  addedAt: string;
  variantId?: string;
}

// Frontend cart item format  
interface CartItemFrontend {
  product: Product;
  quantity: number;
}
```

---

### Feature 3.2: View Cart

**Description**: Display cart contents

**Functional Requirements**:
- List all cart items
- Product image, name, price
- Quantity selector per item
- Remove item option
- Subtotal per item
- Cart total (subtotal + tax + shipping estimate)
- Continue shopping button
- Proceed to checkout button
- Empty cart message

**UI Components**:
- Cart page with item list
- Product thumbnails
- Quantity controls (+/- buttons)
- Remove button
- Price breakdown section
- Checkout button
- Empty cart illustration

**API Endpoints**:
```
GET /api/v1/cart
PUT /api/v1/cart/items/:product_id
DELETE /api/v1/cart/items/:product_id
DELETE /api/v1/cart
```

---

### Feature 3.3: Update Cart Quantity

**Description**: Modify item quantities in cart

**Functional Requirements**:
- Increase/decrease quantity
- Direct quantity input
- Stock validation
- Auto-update totals
- Remove item if quantity = 0
- Max quantity limit per product

**UI Components**:
- Quantity input with +/- buttons
- Stock limit warning
- Auto-updating price display

**API Endpoints**:
```
PUT /api/v1/cart/items/:product_id
```

**Request Body**:
```json
{
  "quantity": 3
}
```

---

### Feature 3.4: Apply Coupon Code

**Description**: Discount code application

**Functional Requirements**:
- Coupon code input field
- Validate coupon (existence, expiry, usage limits)
- Calculate discount (percentage or fixed amount)
- Display discount in cart total
- Remove coupon option
- One coupon per order
- Coupon types: percentage, fixed amount, free shipping

**UI Components**:
- Coupon input field with apply button
- Applied coupon display with remove option
- Discount amount in price breakdown
- Invalid coupon error message

**API Endpoints**:
```
POST /api/v1/cart/coupon
DELETE /api/v1/cart/coupon
GET /api/v1/coupons/validate/:code
```

**Database Tables**:
- `coupons` (code, type, value, min_order_amount, max_discount, valid_from, valid_until, usage_limit, usage_count)

---

### Feature 3.5: Wishlist Management

**Description**: Save products for later

**Functional Requirements**:
- Add product to wishlist
- Remove from wishlist
- View wishlist page
- Move item from wishlist to cart
- Share wishlist
- Wishlist item count badge

**UI Components**:
- Heart icon (add/remove wishlist)
- Wishlist page with product grid
- Move to cart button
- Remove from wishlist button
- Share wishlist button
- Empty wishlist message

**API Endpoints**:
```
GET /api/v1/wishlist
POST /api/v1/wishlist/items
DELETE /api/v1/wishlist/items/:product_id
POST /api/v1/wishlist/items/:product_id/move-to-cart
```

**Database Tables**:
- `wishlist_items` (user_id, product_id, added_at)

---

### Feature 3.6: Cart Persistence

**Description**: Save cart across sessions

**Functional Requirements**:
- Guest cart stored in session/localStorage
- Logged-in cart stored in Redis
- Merge guest cart with user cart on login
- Cart expiry (30 days for logged-in users)
- Sync cart across devices (logged-in users)

**Technical Implementation**:
- Guest: localStorage + session cookie
- Logged-in: Redis with TTL
- Merge logic on login

---

## EPIC 4: Checkout & Payment

### Feature 4.1: Checkout Flow

**Description**: Multi-step checkout process

**Functional Requirements**:
- Step 1: Delivery address selection
- Step 2: Delivery time slot selection
- Step 3: Payment method selection
- Step 4: Order review
- Progress indicator
- Edit previous steps
- Guest checkout option

**UI Components**:
- Stepper/progress bar
- Address selection cards
- Time slot picker
- Payment method cards
- Order summary sidebar
- Edit buttons for each step

**API Endpoints**:
```
GET /api/v1/checkout/init
POST /api/v1/checkout/address
POST /api/v1/checkout/delivery-slot
POST /api/v1/checkout/payment-method
```

---

### Feature 4.2: Delivery Address Selection

**Description**: Choose or add delivery address

**Functional Requirements**:
- Display saved addresses
- Select delivery address
- Add new address inline
- Edit address
- Set as default option
- Address validation

**UI Components**:
- Address cards with radio buttons
- Add new address button
- Inline address form
- Selected address highlight

**API Endpoints**:
```
GET /api/v1/users/addresses
POST /api/v1/users/addresses
```

---

### Feature 4.3: Delivery Time Slot

**Description**: Select delivery date and time

**Functional Requirements**:
- Display available delivery slots
- Date picker (next 7 days)
- Time slots (Morning, Afternoon, Evening)
- Slot availability status
- Delivery charges per slot
- Express delivery option

**UI Components**:
- Date selector
- Time slot cards
- Availability indicators
- Delivery charge display
- Express delivery toggle

**API Endpoints**:
```
GET /api/v1/delivery/slots?date=2026-01-20
POST /api/v1/checkout/delivery-slot
```

**Database Tables**:
- `delivery_slots` (slot_id, date, time_range, capacity, booked_count, charge)

---

### Feature 4.4: Payment Method Selection

**Description**: Choose payment option

**Functional Requirements**:
- Credit/Debit Card
- UPI
- Digital Wallets (PayPal, Google Pay, Apple Pay)
- Cash on Delivery (COD)
- Saved cards management
- CVV for saved cards
- Payment gateway integration

**UI Components**:
- Payment method cards
- Card input form
- UPI ID input
- Wallet selection
- Save card checkbox
- Secure payment badges

**API Endpoints**:
```
GET /api/v1/payment/methods
POST /api/v1/payment/cards
GET /api/v1/payment/cards
DELETE /api/v1/payment/cards/:id
```

**Database Tables**:
- `saved_cards` (card_id, user_id, last_four, card_type, expiry_month, expiry_year, token)

---

### Feature 4.5: Order Review & Placement

**Description**: Final order confirmation

**Functional Requirements**:
- Display complete order summary
- Items list with quantities and prices
- Delivery address
- Delivery slot
- Payment method
- Price breakdown (subtotal, tax, delivery, discount, total)
- Terms acceptance checkbox
- Place order button
- Order creation
- Inventory deduction
- Payment processing

**UI Components**:
- Order summary section
- Items list
- Address display
- Price breakdown table
- Terms checkbox
- Place order button
- Loading state during processing

**API Endpoints**:
```
POST /api/v1/orders
```

**Request Body**:
```json
{
  "address_id": "123",
  "delivery_slot_id": "456",
  "payment_method": "card",
  "payment_details": {},
  "coupon_code": "SAVE10"
}
```

---

### Feature 4.6: Payment Processing

**Description**: Secure payment handling

**Functional Requirements**:
- Stripe/Razorpay integration
- Payment intent creation
- 3D Secure authentication
- Payment confirmation
- Payment failure handling
- Retry payment option
- Refund processing

**API Endpoints**:
```
POST /api/v1/payments/initiate
POST /api/v1/payments/confirm
POST /api/v1/payments/verify
```

**Database Tables**:
- `payments` (payment_id, order_id, amount, method, status, transaction_id, gateway_response, created_at)

---

### Feature 4.7: Order Confirmation

**Description**: Post-order success page

**Functional Requirements**:
- Order confirmation page
- Order ID display
- Estimated delivery date
- Order summary
- Download invoice button
- Track order button
- Continue shopping button
- Email confirmation sent
- SMS confirmation sent

**UI Components**:
- Success message with checkmark
- Order details card
- Download invoice button
- Track order button
- Confirmation email sent message

**API Endpoints**:
```
GET /api/v1/orders/:id/confirmation
GET /api/v1/orders/:id/invoice
```

---

## EPIC 5: Order Management

### Feature 5.1: Order History

**Description**: View past orders

**Functional Requirements**:
- List all user orders
- Order status badges
- Order date and total
- Quick reorder button
- Filter by status (All, Pending, Delivered, Cancelled)
- Search orders by order ID or product name
- Pagination

**UI Components**:
- Order list with cards
- Status badges (color-coded)
- Filter tabs
- Search bar
- Reorder button
- View details button

**API Endpoints**:
```
GET /api/v1/orders?page=1&status=delivered
GET /api/v1/orders/search?q=order123
```

---

### Feature 5.2: Order Details

**Description**: Detailed order information

**Functional Requirements**:
- Order ID and date
- Order status with timeline
- Items list with images
- Delivery address
- Payment method
- Price breakdown
- Download invoice
- Track shipment
- Cancel order (if eligible)
- Return/refund request

**UI Components**:
- Order header with ID and status
- Status timeline
- Items table
- Address and payment info
- Action buttons (cancel, track, invoice, return)

**API Endpoints**:
```
GET /api/v1/orders/:id
GET /api/v1/orders/:id/timeline
```

**Database Tables**:
- `orders` (order_id, user_id, status, total_amount, delivery_address_id, payment_id, created_at, updated_at)
- `order_items` (order_item_id, order_id, product_id, quantity, unit_price, subtotal)
- `order_status_history` (order_id, status, timestamp, notes)

---

### Feature 5.3: Order Tracking

**Description**: Real-time order status tracking

**Functional Requirements**:
- Order status timeline
- Status updates: Placed, Confirmed, Packed, Shipped, Out for Delivery, Delivered
- Estimated delivery date
- Delivery partner details
- Tracking number
- Live location tracking (if available)
- Status change notifications

**UI Components**:
- Visual timeline/stepper
- Status descriptions
- Delivery partner info
- Tracking number
- Map view (optional)
- Refresh status button

**API Endpoints**:
```
GET /api/v1/orders/:id/tracking
GET /api/v1/orders/:id/delivery-status
```

---

### Feature 5.4: Cancel Order

**Description**: Cancel pending orders

**Functional Requirements**:
- Cancel order before shipment
- Cancellation reason selection
- Refund initiation
- Cancel confirmation
- Email notification
- Update inventory

**UI Components**:
- Cancel order button
- Cancellation reason dropdown
- Confirmation modal
- Refund information display

**API Endpoints**:
```
POST /api/v1/orders/:id/cancel
```

**Request Body**:
```json
{
  "reason": "Changed mind",
  "comments": "Optional comments"
}
```

**Business Rules**:
- Can only cancel if status is "Placed" or "Confirmed"
- Refund processed within 5-7 business days

---

### Feature 5.5: Return & Refund

**Description**: Request product returns

**Functional Requirements**:
- Return request form
- Select items to return
- Return reason selection
- Upload images (damaged products)
- Return pickup scheduling
- Return status tracking
- Refund processing
- Return policy display

**UI Components**:
- Return request form
- Item selection checkboxes
- Reason dropdown
- Image upload
- Pickup date selector
- Return policy modal

**API Endpoints**:
```
POST /api/v1/orders/:id/return
GET /api/v1/returns/:id
PUT /api/v1/returns/:id/status
```

**Database Tables**:
- `returns` (return_id, order_id, items, reason, status, images, pickup_date, refund_amount, created_at)

**Return Statuses**: Requested, Approved, Pickup Scheduled, Picked Up, Inspected, Refunded, Rejected

---

### Feature 5.6: Download Invoice

**Description**: Generate and download order invoices

**Functional Requirements**:
- PDF invoice generation
- Invoice number
- Order details
- Items with prices
- Tax breakdown
- Company details
- Customer details
- Download and email options

**UI Components**:
- Download invoice button
- Email invoice button
- PDF preview

**API Endpoints**:
```
GET /api/v1/orders/:id/invoice
POST /api/v1/orders/:id/invoice/email
```

**PDF Template Includes**:
- Company logo and details
- Invoice number and date
- Customer details
- Billing and shipping address
- Items table
- Tax and total breakdown

---

### Feature 5.7: Reorder

**Description**: Quick reorder from past orders

**Functional Requirements**:
- One-click reorder
- Add all items to cart
- Check stock availability
- Update prices to current
- Notify if items unavailable
- Redirect to cart

**UI Components**:
- Reorder button
- Confirmation toast
- Unavailable items notification

**API Endpoints**:
```
POST /api/v1/orders/:id/reorder
```

---

## EPIC 6: Reviews & Ratings

### Feature 6.1: Write Review

**Description**: Submit product reviews

**Functional Requirements**:
- Only verified purchases can review
- Star rating (1-5)
- Review title
- Review text (min 20 chars)
- Upload photos (max 5)
- One review per product per user
- Edit review within 48 hours

**UI Components**:
- Star rating selector
- Review form (title, text)
- Photo upload with preview
- Submit button
- Character counter

**API Endpoints**:
```
POST /api/v1/products/:id/reviews
PUT /api/v1/reviews/:id
```

**Request Body**:
```json
{
  "rating": 5,
  "title": "Great product!",
  "comment": "This product exceeded my expectations...",
  "images": ["url1", "url2"]
}
```

**Database Tables**:
- `reviews` (review_id, product_id, user_id, order_id, rating, title, comment, images, helpful_count, created_at, updated_at)

---

### Feature 6.2: View Reviews

**Description**: Display product reviews

**Functional Requirements**:
- List all reviews for product
- Sort by: Most Recent, Highest Rating, Lowest Rating, Most Helpful
- Filter by rating (5 stars, 4 stars, etc.)
- Pagination
- Review summary (average rating, rating distribution)
- Verified purchase badge
- Helpful votes display

**UI Components**:
- Rating summary section
- Rating distribution bars
- Sort dropdown
- Filter buttons
- Review cards
- Pagination controls

**API Endpoints**:
```
GET /api/v1/products/:id/reviews?sort=helpful&filter=5&page=1
GET /api/v1/products/:id/reviews/summary
```

---

### Feature 6.3: Helpful Votes

**Description**: Mark reviews as helpful

**Functional Requirements**:
- "Was this helpful?" button
- Yes/No voting
- Vote count display
- One vote per user per review
- Update helpful count

**UI Components**:
- Helpful vote buttons
- Vote count display
- Voted state indication

**API Endpoints**:
```
POST /api/v1/reviews/:id/helpful
```

**Database Tables**:
- `review_votes` (review_id, user_id, is_helpful)

---

### Feature 6.4: Review Moderation (Admin)

**Description**: Admin review moderation

**Functional Requirements**:
- View pending reviews
- Approve/reject reviews
- Flag inappropriate reviews
- Delete reviews
- Respond to reviews (admin)

**UI Components**:
- Moderation queue
- Approve/reject buttons
- Flag button
- Admin response form

**API Endpoints**:
```
GET /api/v1/admin/reviews/pending
PUT /api/v1/admin/reviews/:id/approve
PUT /api/v1/admin/reviews/:id/reject
POST /api/v1/admin/reviews/:id/respond
```

---

## EPIC 7: Admin Dashboard

### Feature 7.0: Multi-Branch Role-Based Access Control (RBAC)

**Description**: Hierarchical access control system supporting multiple store branches with role-based permissions

**Functional Requirements**:
- Support for multiple branch locations
- Role hierarchy: Super Admin > Branch Manager > Staff
- Branch isolation for non-admin roles
- Role-specific permissions matrix
- Branch-specific inventory management
- Cross-branch analytics (Super Admin only)

**Role Definitions**:

| Role | Description | Branch Scope | Key Permissions |
|------|-------------|--------------|-----------------|
| **Super Admin** | Complete system control | All branches | Full CRUD on all resources, user management, financial analytics, system configuration |
| **Branch Manager** | Branch-level management | Assigned branch only | Manage inventory, staff, orders; Branch analytics; Update stock for own branch |
| **Marketing Manager** | Branch-level marketing & discounts | Assigned branch only | Set/update product discounts; View branch analytics; Manage Quick Sale feed via discounts |
| **Staff** | Operational access | Assigned branch only | View/process orders; View products; Limited customer interaction |
| **Support** | Customer support | Cross-branch (read-only) | Handle support tickets; Resolve order issues |
| **Inventory** | Stock management | Cross-branch | Manage stock levels; Process transfers; Handle low-stock alerts |

**UI Components**:
- Branch selector (Super Admin only)
- Role-based navigation menu
- Branch context indicator
- User management panel
- Permission matrix viewer
- Branch assignment dropdown

**API Endpoints**:
```
# Branch Management (Super Admin)
GET    /api/v1/admin/branches              - List all branches
POST   /api/v1/admin/branches              - Create new branch
GET    /api/v1/admin/branches/:id          - Get branch details
PUT    /api/v1/admin/branches/:id          - Update branch
DELETE /api/v1/admin/branches/:id          - Deactivate branch

# Admin User Management (Super Admin)
GET    /api/v1/admin/users                 - List admin users
POST   /api/v1/admin/users                 - Create admin user with branch
PUT    /api/v1/admin/users/:id             - Update admin user
PUT    /api/v1/admin/users/:id/assign-branch - Assign user to branch

# Role Permissions
GET    /api/v1/admin/roles                 - Get available roles
GET    /api/v1/admin/roles/:role/permissions - Get role permissions
PUT    /api/v1/admin/roles/:role/permissions - Update role permissions (Super Admin)
```

**Database Tables**:
- `branches` (branch_id, name, code, address, city, state, country, manager_id, is_active)
- `admin_users` (admin_id, email, password_hash, full_name, role, branch_id, is_active)
- `role_permissions` (permission_id, role, resource, action, is_allowed)
- `branch_inventory` (inventory_id, branch_id, product_id, variant_id, stock_quantity, reserved_quantity)

**Branch Isolation Logic**:
- All database queries automatically filtered by user's `branch_id`
- Super Admin bypasses branch filter
- Cross-branch data access denied for branch-level users
- Redis caching with branch-prefixed keys

**Validation Rules**:
- Branch code: Unique, 2-10 alphanumeric characters
- Role: Must be one of: super_admin, branch_manager, marketing_manager, staff, support, inventory
- Branch assignment: Required for branch_manager, marketing_manager, and staff roles
- Manager assignment: One manager per branch

---

### Feature 7.1: Dashboard Overview

**Description**: Admin dashboard home

**Functional Requirements**:
- Total sales (today, week, month, year)
- Total orders count
- Total customers count
- Revenue charts
- Recent orders list
- Low stock alerts
- Top selling products
- Customer growth chart

**UI Components**:
- KPI cards (sales, orders, customers)
- Line/bar charts
- Recent orders table
- Alerts section
- Top products list

**API Endpoints**:
```
GET /api/v1/admin/dashboard/stats
GET /api/v1/admin/dashboard/sales-chart?period=month
GET /api/v1/admin/dashboard/recent-orders
GET /api/v1/admin/dashboard/low-stock
```

---

### Feature 7.2: Product Management

**Description**: CRUD operations for products

**Functional Requirements**:
- List all products with search and filters
- Add new product
- Edit product details
- Delete product
- Bulk actions (delete, update status)
- Import products (CSV)
- Export products (CSV)
- Product variants management

**UI Components**:
- Products table with actions
- Add/Edit product form
- Image upload
- Category selector
- Bulk action toolbar
- Import/export buttons

**API Endpoints**:
```
GET /api/v1/admin/products
POST /api/v1/admin/products
PUT /api/v1/admin/products/:id
DELETE /api/v1/admin/products/:id
POST /api/v1/admin/products/bulk-delete
POST /api/v1/admin/products/import
GET /api/v1/admin/products/export
```

---

### Feature 7.3: Order Management

**Description**: Manage customer orders

**Functional Requirements**:
- List all orders with filters
- View order details
- Update order status
- Print packing slip
- Print invoice
- Bulk status update
- Export orders

**UI Components**:
- Orders table
- Status filter tabs
- Order details modal
- Status update dropdown
- Print buttons
- Bulk actions

**API Endpoints**:
```
GET /api/v1/admin/orders
GET /api/v1/admin/orders/:id
PUT /api/v1/admin/orders/:id/status
GET /api/v1/admin/orders/:id/packing-slip
```

---

### Feature 7.4: Customer Management

**Description**: Manage customer accounts

**Functional Requirements**:
- List all customers
- View customer details
- Customer order history
- Block/unblock customers
- Customer lifetime value
- Export customer list

**UI Components**:
- Customers table
- Customer details page
- Order history tab
- Block/unblock button
- Export button

**API Endpoints**:
```
GET /api/v1/admin/customers
GET /api/v1/admin/customers/:id
GET /api/v1/admin/customers/:id/orders
PUT /api/v1/admin/customers/:id/block
```

---

### Feature 7.5: Inventory Management

**Description**: Track and manage stock

**Functional Requirements**:
- View stock levels
- Update stock quantities
- Low stock alerts
- Stock history
- Bulk stock update
- Stock reports

**UI Components**:
- Inventory table
- Stock update form
- Low stock alerts
- Stock history chart

**API Endpoints**:
```
GET /api/v1/admin/inventory
PUT /api/v1/admin/inventory/:product_id
GET /api/v1/admin/inventory/low-stock
GET /api/v1/admin/inventory/:product_id/history
```

---

### Feature 7.6: Coupon Management

**Description**: Create and manage coupons

**Functional Requirements**:
- Create coupon codes
- Set discount type (percentage/fixed)
- Set validity period
- Usage limits
- Minimum order amount
- Active/inactive status
- Coupon usage reports

**UI Components**:
- Coupons list
- Create/edit coupon form
- Usage statistics
- Activate/deactivate toggle

**API Endpoints**:
```
GET /api/v1/admin/coupons
POST /api/v1/admin/coupons
PUT /api/v1/admin/coupons/:id
DELETE /api/v1/admin/coupons/:id
GET /api/v1/admin/coupons/:id/usage
```

---

### Feature 7.7: Reports & Analytics

**Description**: Business intelligence reports

**Functional Requirements**:
- Sales reports (daily, weekly, monthly, yearly)
- Product performance reports
- Customer reports
- Revenue analytics
- Export reports (PDF, CSV, Excel)
- Date range selection
- Visual charts

**UI Components**:
- Report selector
- Date range picker
- Charts and graphs
- Export buttons
- Filter options

**API Endpoints**:
```
GET /api/v1/admin/reports/sales?from=2026-01-01&to=2026-01-31
GET /api/v1/admin/reports/products
GET /api/v1/admin/reports/customers
GET /api/v1/admin/reports/export
```

---

### Feature 7.8: Settings Management

**Description**: Configure system settings

**Functional Requirements**:
- General settings (site name, logo, contact)
- Payment gateway configuration
- Email settings (SMTP)
- SMS settings
- Tax settings
- Shipping zones and rates
- Currency settings

**UI Components**:
- Settings tabs
- Configuration forms
- Test email/SMS buttons
- Save settings button

**API Endpoints**:
```
GET /api/v1/admin/settings
PUT /api/v1/admin/settings/general
PUT /api/v1/admin/settings/payment
PUT /api/v1/admin/settings/email
PUT /api/v1/admin/settings/shipping
```

---

### Feature 7.9: Dynamic Video Splash Screen (Mobile App)

**Description**: Configurable video splash screen for the mobile app with Super Admin control

**Functional Requirements**:
- Super Admin can upload a custom splash video for the mobile app
- Video duration must not exceed **4 seconds**
- File size must be under **5MB** for fast mobile loading
- Format restricted to **MP4** only
- Video plays on app startup before navigating to Auth/Main screen
- Smooth fade-out transition at exactly 4-second mark
- Video is cached locally on mobile device after first download
- Fallback to static splash image if video unavailable

**UI Components (Admin Portal)**:
- Video upload form with drag-and-drop
- Client-side duration validation (4 seconds max)
- File size validation (5MB max)
- Video preview before upload
- Current video display with metadata
- Delete video option
- Clear instruction: *"Please upload a 4-second MP4 video for the app splash screen."*

**Mobile App Behavior (Flutter)**:
- Fetch splash video URL from `/api/v1/mobile/splash-video` on app start
- Cache video to local file system (`path_provider`)
- Play video for full 4-second duration using `video_player`
- Animated fade-out transition (500ms) to Auth/Main navigator
- Static fallback splash if video fails to load

**API Endpoints**:
```
GET  /api/v1/admin/settings/splash-video     # Get current splash video (Admin)
PUT  /api/v1/admin/settings/splash-video     # Upload new splash video (Super Admin)
DELETE /api/v1/admin/settings/splash-video   # Remove splash video (Super Admin)
GET  /api/v1/mobile/splash-video             # Public endpoint for mobile app
```

**Server-Side Validation**:
- Duration check: `duration <= 4.0 seconds`
- File size check: `size <= 5MB`
- MIME type check: `video/mp4`
- Metadata stored in `system_settings` table

**Database Table**:
```sql
CREATE TABLE system_settings (
    setting_id UUID PRIMARY KEY,
    setting_key VARCHAR(100) UNIQUE NOT NULL,  -- 'splash_video'
    setting_value TEXT,                        -- JSON metadata
    setting_type VARCHAR(50),                  -- 'json'
    created_at TIMESTAMP,
    updated_at TIMESTAMP
);
```

**Video Metadata JSON**:
```json
{
    "url": "/uploads/splash-videos/splash-video-1706540400000.mp4",
    "filename": "splash-video-1706540400000.mp4",
    "duration": 4.0,
    "fileSize": 4500000,
    "mimeType": "video/mp4",
    "uploadedAt": "2026-01-29T10:00:00.000Z",
    "uploadedBy": "admin-uuid"
}
```

---

## EPIC 8: Notifications & Communication

### Feature 8.1: Email Notifications

**Description**: Automated email communications

**Functional Requirements**:
- Welcome email (registration)
- Email verification
- Password reset email
- Order confirmation email
- Order status updates
- Delivery notifications
- Invoice email
- Promotional emails
- Newsletter

**Email Templates**:
- Welcome email
- Order confirmation
- Shipping notification
- Delivery confirmation
- Return confirmation
- Promotional campaign

**API Endpoints**:
```
POST /api/v1/notifications/email/send
GET /api/v1/notifications/email/templates
```

**Email Service**: SendGrid/AWS SES

---

### Feature 8.2: SMS Notifications

**Description**: SMS alerts for critical updates

**Functional Requirements**:
- OTP for verification
- Order confirmation SMS
- Shipping updates
- Out for delivery alert
- Delivery confirmation
- SMS preferences

**SMS Templates**:
- OTP: "Your OTP is {code}"
- Order: "Order #{id} confirmed. Track: {link}"
- Delivery: "Your order is out for delivery"

**API Endpoints**:
```
POST /api/v1/notifications/sms/send
```

**SMS Service**: Twilio

---

### Feature 8.3: Push Notifications

**Description**: Browser/app push notifications

**Functional Requirements**:
- Order status updates
- Promotional offers
- Price drop alerts
- Back in stock notifications
- Abandoned cart reminders
- Notification preferences
- Opt-in/opt-out

**UI Components**:
- Notification permission prompt
- Notification bell icon
- Notification dropdown
- Notification settings page

**API Endpoints**:
```
POST /api/v1/notifications/push/send
PUT /api/v1/notifications/push/subscribe
GET /api/v1/notifications/push/preferences
```

**Service**: Firebase Cloud Messaging (FCM)

---

### Feature 8.4: Notification Preferences

**Description**: User notification settings

**Functional Requirements**:
- Email notification toggles
- SMS notification toggles
- Push notification toggles
- Notification categories (orders, promotions, updates)
- Unsubscribe from all
- Save preferences

**UI Components**:
- Preferences form with toggles
- Category sections
- Save button
- Unsubscribe all link

**API Endpoints**:
```
GET /api/v1/users/notification-preferences
PUT /api/v1/users/notification-preferences
```

**Database Tables**:
- `notification_preferences` (user_id, email_orders, email_promotions, sms_orders, push_orders, etc.)

---

### Feature 8.5: In-App Notifications

**Description**: Notifications within the application

**Functional Requirements**:
- Notification center
- Unread count badge
- Mark as read
- Delete notifications
- Notification types (info, success, warning, error)
- Real-time updates

**UI Components**:
- Notification bell with badge
- Notification dropdown
- Notification list
- Mark all as read button

**API Endpoints**:
```
GET /api/v1/notifications
PUT /api/v1/notifications/:id/read
DELETE /api/v1/notifications/:id
PUT /api/v1/notifications/mark-all-read
```

**Database Tables**:
- `notifications` (notification_id, user_id, type, title, message, is_read, created_at)

---

## EPIC 9: Advanced Watchlist Management

### Feature 9.1: Variant-Aware Watchlist

**Description**: Save specific product variants to watchlist with price tracking

**Functional Requirements**:
- Add product variant to watchlist (not just base product)
- Store variant_id along with product_id
- Track price at time of adding (`price_at_watch`)
- Support multiple variants of same product in watchlist
- Remove specific variant from watchlist
- Wishlist syncs across devices (logged-in users)
- Heart icon reflects variant-specific watchlist state

**UI Components**:
- Heart icon on product detail page (variant-aware)
- Variant selector integrated with wishlist button
- Watchlist badge shows total item count
- Variant details displayed in watchlist (e.g., "500g", "Large")

**API Endpoints**:
```
GET    /api/v1/wishlist
POST   /api/v1/wishlist/items
DELETE /api/v1/wishlist/items/:productId/:variantId?
POST   /api/v1/wishlist/items/:productId/:variantId/move-to-cart
GET    /api/v1/wishlist/count
```

**Request Body (Add Item)**:
```json
{
  "productId": "uuid",
  "variantId": "uuid",  // Optional, null for base product
  "priceAtWatch": 29.99
}
```

**Database Schema (PostgreSQL)**:
```sql
wishlist_items: {
  wishlist_item_id UUID PRIMARY KEY,
  user_id UUID REFERENCES users,
  product_id VARCHAR(255) NOT NULL,  -- MongoDB product ID
  variant_id UUID REFERENCES product_variants,  -- NULL for base product
  price_at_watch DECIMAL(10,2) NOT NULL,
  added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(user_id, product_id, variant_id)
}
```

---

### Feature 9.2: Price Drop Tracking

**Description**: Notify users when watchlist item prices decrease

**Functional Requirements**:
- Compare current price with `price_at_watch`
- Calculate price drop percentage
- Highlight items with price drops in watchlist
- Badge indicator for price drops
- Filter watchlist by "Price Drops"
- Price history visualization (optional)

**UI Components**:
- Price comparison display (was $29.99, now $24.99)
- Green "Price Drop" badge
- Percentage discount indicator (-17%)
- Filter toggle for "Show Price Drops Only"
- Price trend icon (↓ for decrease, ↑ for increase, → for same)

**API Endpoints**:
```
GET /api/v1/wishlist/price-drops
```

**Response Format**:
```json
{
  "success": true,
  "data": [
    {
      "productId": "uuid",
      "variantId": "uuid",
      "productName": "Organic Milk",
      "variantName": "500g",
      "priceAtWatch": 29.99,
      "currentPrice": 24.99,
      "priceDrop": 5.00,
      "priceDropPercentage": 16.67,
      "addedAt": "2026-01-20T10:30:00Z"
    }
  ]
}
```

**Business Logic**:
- Price drop calculated as: `price_at_watch - current_price`
- Only show as "drop" if difference > $0.50 (configurable threshold)
- Update current prices on watchlist load (JOIN with products/variants)

---

### Feature 9.3: Redis-Backed Wishlist Cache

**Description**: High-performance wishlist state management with Redis

**Functional Requirements**:
- Cache user's watchlist in Redis for instant lookups
- O(1) complexity for "is in wishlist" checks
- Sync Redis cache with PostgreSQL on login
- Auto-refresh cache on add/remove operations
- Cache expiry: 7 days with auto-renewal on access
- Fallback to database if Redis unavailable

**Technical Implementation**:

**Redis Data Structure**:
```
Key: watchlist:{userId}
Type: Redis Set
Value: Set of "{productId}:{variantId}" strings
TTL: 604800 seconds (7 days)

Example:
watchlist:user-123 → {
  "prod-abc:variant-xyz",
  "prod-def:variant-uvw",
  "prod-ghi:null"  // Base product without variant
}
```

**Cache Operations**:
```typescript
// Add to cache
SADD watchlist:{userId} "{productId}:{variantId}"
EXPIRE watchlist:{userId} 604800

// Check if in wishlist (O(1))
SISMEMBER watchlist:{userId} "{productId}:{variantId}"

// Remove from cache
SREM watchlist:{userId} "{productId}:{variantId}"

// Get all items
SMEMBERS watchlist:{userId}

// Sync from database
DEL watchlist:{userId}
SADD wishlist:{userId} ...items from DB...
```

**Sync Strategy**:
1. On user login → Load wishlist from DB to Redis
2. On add/remove → Update both Redis and PostgreSQL
3. On app load → Check Redis first, fallback to DB if miss
4. Background job → Sync Redis with DB every 6 hours (optional)

**Performance Benefits**:
- Product page load: Check wishlist state without DB query
- Mobile app: Instant heart icon state on scroll
- Scalability: Reduced database load for read-heavy operations

**API Endpoints**:
```
POST /api/v1/wishlist/sync  // Manual sync trigger
```

---

## EPIC 10: Location-Based Branch Routing

### Feature 10.1: Address-Based Branch Selection (Pre-Home Screen)

**Description**: Enforce branch selection before accessing the Home Page by resolving the user's delivery address to a serving branch

**Functional Requirements**:
- On app launch, if no branch context exists in the session, redirect to Address Selection screen
- Display user's saved delivery addresses for selection
- Backend resolves the selected address to a `branch_id` via Post Office → Branch mapping
- Store the resolved `branch_id` in the user's Redis session context
- Redirect to the Home Page, now scoped to the resolved branch
- Allow user to change branch by re-selecting an address from profile/settings
- If address maps to no branch, show a user-friendly "We don't serve this area yet" message

**UI Components**:
- Address Selection screen (pre-home, full-screen overlay)
- Saved address cards with radio selection
- "Add New Address" option (inline form)
- Current branch indicator in the app header/toolbar
- "Change Location" button accessible from Home Page header
- "Area not served" fallback screen with support contact

**API Endpoints**:
```
POST /api/v1/branch/resolve             # Resolve address → branch, set session context
GET  /api/v1/branch/current             # Get current branch context from session
DELETE /api/v1/branch/current           # Clear branch context (forces re-selection)
GET  /api/v1/branch/post-offices        # List all served post offices (for validation)
```

**Request Body (Resolve)**:
```json
{
  "address_id": "uuid"
}
```

**Response (Resolve)**:
```json
{
  "branch_id": "uuid",
  "branch_name": "Colombo Central Branch",
  "post_office": "Nugegoda",
  "resolved_at": "2026-02-17T10:30:00Z"
}
```

**Database Tables**:
- `addresses` (existing — `post_office` field used for lookup)
- `post_office_branch_mapping` (post_office → branch_id mapping)
- Redis key: `session:{userId}:branch_context` (30-day TTL)

**Validation Rules**:
- User must be authenticated (logged in) before branch selection
- Address must belong to the authenticated user
- `post_office` field must have an active mapping in `post_office_branch_mapping`
- Branch must be active (`is_active = true`)

---

### Feature 10.2: Quick Sale Home Page Feed

**Description**: The Home Page displays only high-discount "Quick Sale" products specific to the resolved branch

**Functional Requirements**:
- Home Page exclusively shows "Quick Sale" items — products with the highest discount percentages in the user's active branch
- Discount is calculated as: `(compare_at_price - price) / compare_at_price * 100`
- Only products with a valid `compare_at_price > price` qualify for Quick Sale
- Products must be in stock at the branch (`branch_inventory.stock_quantity > 0`)
- Default limit: 20 items, sorted by highest discount percentage descending
- Pull-to-refresh and infinite scroll/pagination supported
- General product browsing is moved to Category-specific screens
- If no Quick Sale items are available, show an empty state with a link to browse categories

**UI Components**:
- Home Page header with branch name and "Change Location" button
- Quick Sale banner/section title
- Product cards showing: image, name, original price (strikethrough), sale price, discount percentage badge
- "View All Categories" button/link below Quick Sale grid
- Empty state illustration if no Quick Sale items available
- Pull-to-refresh indicator

**API Endpoints**:
```
GET /api/v1/home/quick-sale?page=1&limit=20    # Quick Sale feed for active branch
```

**Response Format**:
```json
{
  "success": true,
  "branch": {
    "branch_id": "uuid",
    "branch_name": "Colombo Central Branch"
  },
  "data": [
    {
      "product_id": "uuid",
      "name": "Organic Honey",
      "price": 799.00,
      "compare_at_price": 1299.00,
      "discount_percentage": 38.5,
      "image_url": "https://...",
      "stock_quantity": 25,
      "category_name": "Groceries"
    }
  ],
  "pagination": {
    "page": 1,
    "total_pages": 3,
    "total_items": 47
  }
}
```

**Business Rules**:
- Only active products with `compare_at_price > price` are included
- Minimum discount threshold: 5% (configurable)
- Products must have stock in the user's branch
- Sort order: highest discount percentage first
- Stale cache cleared on any price update by Marketing Manager

---

### Feature 10.3: Marketing Manager Discount Management

**Description**: Branch-restricted admin role for managing product discounts that drive the Quick Sale feed

**Functional Requirements**:
- `MARKETING_MANAGER` is a branch-restricted role assigned to a specific branch
- Can view all products available in their branch's inventory
- Can set and update the `compare_at_price` and `price` fields for products within their branch
- Discount changes are audit-logged
- The system automatically selects the highest-discount products for the Home Page Quick Sale feed — no manual featuring needed
- Can view a "Discount Performance" analytics dashboard showing: click-through rates, conversion from Quick Sale, revenue impact
- Cannot manage inventory quantities, users, orders, or system settings

**UI Components (Admin Portal)**:
- Discount Management page (table of branch products with editable price fields)
- Inline editing for `price` and `compare_at_price`
- Calculated discount percentage preview (real-time)
- "Quick Sale Preview" panel showing current top 20 Quick Sale items
- Discount Performance dashboard (charts: top discounted products, conversion metrics)
- Bulk discount update (CSV upload or percentage-based batch update)

**API Endpoints**:
```
GET    /api/v1/admin/marketing/products            # List branch products with discount info
PUT    /api/v1/admin/marketing/products/:id/discount  # Update product discount
GET    /api/v1/admin/marketing/quick-sale/preview   # Preview current Quick Sale feed
GET    /api/v1/admin/marketing/analytics            # Discount performance analytics
POST   /api/v1/admin/marketing/products/bulk-discount # Bulk discount update
```

**Request Body (Set Discount)**:
```json
{
  "price": 999.00,
  "compare_at_price": 1500.00
}
```

**Validation Rules**:
- `price` must be >= 0
- `compare_at_price` must be > `price` (otherwise it's not a discount)
- Marketing Manager can only modify products stocked in their assigned branch
- All changes are recorded in `admin_audit_logs`

**Database Tables**:
- `products` (existing — `price`, `compare_at_price` fields)
- `branch_inventory` (existing — branch-product availability)
- `admin_audit_logs` (existing — audit trail for discount changes)

---

### Feature 10.4: Post Office to Branch Mapping Administration

**Description**: Super Admin capability to manage which Post Offices are served by which branches

**Functional Requirements**:
- Super Admin can create, update, and delete Post Office → Branch mappings
- Each Post Office maps to exactly one branch (UNIQUE constraint)
- Mappings can be bulk-imported via CSV
- List view with search and filter by branch/district/province
- Deactivating a mapping shows "area not served" to customers with that Post Office

**UI Components (Admin Portal)**:
- Post Office Mapping table with branch selector
- Add/Edit mapping form
- CSV import for bulk mapping
- Search by Post Office name, filter by branch/district
- Active/Inactive toggle

**API Endpoints**:
```
GET    /api/v1/admin/branch-mappings               # List all mappings
POST   /api/v1/admin/branch-mappings               # Create mapping
PUT    /api/v1/admin/branch-mappings/:id            # Update mapping
DELETE /api/v1/admin/branch-mappings/:id            # Delete mapping
POST   /api/v1/admin/branch-mappings/import         # Bulk CSV import
```

**Database Tables**:
- `post_office_branch_mapping` (mapping_id, post_office, branch_id, branch_name, district, province, is_active)

---

## EPIC 11: Infrastructure & DevOps

### Feature 11.1: Dockerized Local Development Environment

**Description**: Complete Docker Compose setup for local development with all backend services

**Implementation Status**: ✅ Verified (February 17, 2026)

**Infrastructure Components**:
- PostgreSQL 15 with pgvector extension (`ankane/pgvector` image)
- Redis 7 with password authentication and AOF persistence
- MinIO (S3-compatible) for object storage with Console UI
- FastAPI backend with hot-reload development mode
- Automated MinIO bucket initialization (`sribees-assets`)

**Container Configuration**:

| Container | Image | Ports | Health Check |
|-----------|-------|-------|-------------|
| `sribees_postgres` | `ankane/pgvector` | 5432 | `pg_isready` |
| `sribees_redis` | `redis:7-alpine` | 6379 | `redis-cli ping` |
| `sribees_minio` | `minio/minio` | 9000, 9001 | HTTP health endpoint |
| `sribees_backend` | `fastapi_backend:dev` | 8000 | `/health` endpoint |

**Key Files**:
- `docker-compose.yml` - Service definitions and networking
- `docker-compose.prod.yml` - Production overrides
- `docker/postgres/init.sql` - Schema initialization (16 tables)
- `scripts/setup.sh` - Automated setup script

**Resolved Issues**:
- `s3_local` service renamed to `s3-local` (underscore invalid in DNS hostnames for boto3/mc)
- `init.sql` database name corrected from `sribees_db` to `sribeesonline`
- Migration scripts corrected for schema-qualified table names (`sribees.` prefix)
- ForeignKey constraints added to `Session.user_id` and `Address.user_id` in SQLAlchemy models

---

### Feature 11.2: Jenkins CI/CD Pipeline

**Description**: Automated build, test, and deployment pipeline

**Implementation Status**: ✅ Created

**Pipeline Stages**:
1. **Checkout** - Pull latest code from repository
2. **Build** - Build Docker image for FastAPI backend
3. **Lint & Test** - Run pytest inside container
4. **Health Check** - Verify all containers are healthy
5. **Deploy** - Push image and restart production containers

**Key File**: `Jenkinsfile` (root directory)

---

### Feature 11.3: Splash Video Storage & Management

**Description**: Upload, store, and serve splash video via MinIO (S3-compatible)

**Implementation Status**: ✅ Verified

**Architecture**:
- Video file stored in MinIO bucket `sribees-assets` under `splash/splash_video_initial.mp4`
- Public-read bucket policy for direct client access
- `app_settings` table stores the active `splash_video_url`
- Redis caching for the splash config response (high-frequency endpoint)

**API Endpoints**:
```
GET  /api/v1/app/splash-config          # Public: returns splash video URL (cached)
POST /api/v1/admin/settings/splash-video # Admin: upload new splash video
```

**Android Emulator Support**:
- `X-Client-Platform: android-emulator` header triggers URL rewriting
- `storage_service.py` converts `localhost:9000` URLs to `10.0.2.2:9000`

---

### Feature 11.4: Flutter Mobile App Launch & Connectivity

**Description**: Flutter app setup with Android emulator connectivity to Docker backend

**Implementation Status**: ✅ Verified (February 17, 2026)

**Configuration**:
- `app_config.dart` points to `http://10.0.2.2:8000/api/v1` for development
- `AndroidManifest.xml` includes `INTERNET` permission and `usesCleartextTraffic=true`
- Firebase dependencies stubbed (no-op) until `google-services.json` is configured
- Sentry SDK updated to v8.x callback signature
- Flutter 3.35+ `CardThemeData` API used (replacing deprecated `CardTheme`)

**App Startup Flow**:
1. Splash Screen → fetches video URL from `/api/v1/app/splash-config`
2. Video plays or static logo fallback (2 seconds)
3. Language Selection (if not previously set)
4. Address Selection → Branch Resolution
5. Home Screen (Quick Sale feed filtered by branch)

---

## 📊 Features Summary

| EPIC | Total Features | Priority |
|------|----------------|----------|
| EPIC 1: Authentication | 7 features | High |
| EPIC 2: Product Catalog | 6 features | High |
| EPIC 3: Shopping Cart | 6 features | High |
| EPIC 4: Checkout & Payment | 7 features | High |
| EPIC 5: Order Management | 7 features | High |
| EPIC 6: Reviews & Ratings | 4 features | Medium |
| EPIC 7: Admin Dashboard | 8 features | Medium |
| EPIC 8: Notifications | 5 features | Medium |
| EPIC 9: Advanced Watchlist | 3 features | Medium |
| EPIC 10: Location-Based Branch Routing | 4 features | High |
| EPIC 11: Infrastructure & DevOps | 4 features | Critical |

**Total Features**: 61


---

*Document Version: 5.0 (As-Built)*  
*Last Updated: February 17, 2026*  
*Total Features Documented: 61*  
*Document reflects actual implementation state including Docker infrastructure, CI/CD, and verified local development environment*
