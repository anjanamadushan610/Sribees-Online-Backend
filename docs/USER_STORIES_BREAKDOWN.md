# SRIBEESonline - Complete User Stories Breakdown

> **Customer sign-in (as built, 2026-09-25):** customers sign in with a phone OTP,
> Google or Apple only. Email/password registration, email verification, password
> reset and customer password change were removed from the API and the app; the
> email/password stories below are the original plan and are not implemented.
> Staff (`/admin/auth/*`) and partners (`/partner/auth/login`) keep password logins.

> **Document Status**: As-Built Implementation (January 2026)  
> **Last Updated**: January 29, 2026

> Comprehensive breakdown of all user stories with story points, dependencies, technical specifications, and implementation details. This document reflects the actual implemented features.

---

## 📑 Document Overview

This document provides a complete breakdown of all 51 user stories including:
- User story details and acceptance criteria
- Story points (effort estimation)
- Priority level
- Dependencies on other stories
- Technical implementation notes
- Database requirements
- API specifications
- UI/UX considerations
- Test scenarios

---

## 📊 Story Points Guide

- **1 point**: Very simple, < 4 hours
- **2 points**: Simple, 4-8 hours
- **3 points**: Medium, 1-2 days
- **5 points**: Complex, 2-3 days
- **8 points**: Very complex, 3-5 days
- **13 points**: Extremely complex, 1+ week

---

## EPIC 1: User Authentication & Account Management

### US-1.1: User Registration

**Story ID**: US-1.1  
**Story Points**: 5  
**Priority**: High (Must Have)  
**Sprint**: Sprint 1  
**Dependencies**: None (foundational feature)

**As a** new customer  
**I want to** register for an account with my email and password  
**So that** I can make purchases and track my orders

#### Acceptance Criteria

1. **Registration Form**
   - ✅ Form includes fields: Email, Password, Confirm Password, Full Name
   - ✅ All fields are required
   - ✅ Terms and conditions checkbox is required
   - ✅ Form has clear labels and placeholders

2. **Email Validation**
   - ✅ Email format validation (RFC 5322 compliant)
   - ✅ Email uniqueness check (no duplicate accounts)
   - ✅ Email domain validation (MX record check)
   - ✅ Max length: 255 characters

3. **Password Validation**
   - ✅ Minimum 8 characters
   - ✅ At least 1 uppercase letter
   - ✅ At least 1 lowercase letter
   - ✅ At least 1 number
   - ✅ At least 1 special character (!@#$%^&*)
   - ✅ Password strength indicator (weak/medium/strong)
   - ✅ Password and confirm password must match

4. **Email Verification**
   - ✅ Verification email sent immediately after registration
   - ✅ Email contains verification link with secure token
   - ✅ Token expires after 24 hours
   - ✅ Account status is "unverified" until email is confirmed
   - ✅ User can resend verification email (max 3 times per hour)

5. **User Feedback**
   - ✅ Success message: "Registration successful! Please check your email to verify your account"
   - ✅ Error messages for validation failures
   - ✅ Loading state during form submission

#### Technical Implementation

**Frontend Components**:
```
/components/auth/
  ├── RegisterForm.jsx
  ├── PasswordStrengthIndicator.jsx
  ├── EmailVerificationBanner.jsx
  └── ResendVerificationButton.jsx
```

**API Endpoints**:
```
POST /api/v1/auth/register
Request Body:
{
  "email": "user@example.com",
  "password": "SecurePass123!",
  "full_name": "John Doe",
  "accept_terms": true
}

Response (201 Created):
{
  "success": true,
  "message": "Registration successful. Please verify your email.",
  "user_id": "uuid-here"
}

POST /api/v1/auth/verify-email
Request Body:
{
  "token": "verification-token-here"
}

Response (200 OK):
{
  "success": true,
  "message": "Email verified successfully"
}

POST /api/v1/auth/resend-verification
Request Body:
{
  "email": "user@example.com"
}
```

**Database Schema**:
```sql
CREATE TABLE users (
  user_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email VARCHAR(255) UNIQUE NOT NULL,
  password_hash VARCHAR(255) NOT NULL,
  full_name VARCHAR(100) NOT NULL,
  phone VARCHAR(20),
  profile_picture_url TEXT,
  is_verified BOOLEAN DEFAULT FALSE,
  two_factor_enabled BOOLEAN DEFAULT FALSE,
  two_factor_secret VARCHAR(255),
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  last_login TIMESTAMP
);

CREATE TABLE email_verifications (
  verification_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES users(user_id) ON DELETE CASCADE,
  token VARCHAR(255) UNIQUE NOT NULL,
  expires_at TIMESTAMP NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_email_verifications_token ON email_verifications(token);
CREATE INDEX idx_email_verifications_user_id ON email_verifications(user_id);
```

**Security Considerations**:
- Password hashing: Argon2id with salt
- Token generation: Cryptographically secure random (32 bytes)
- Rate limiting: Max 5 registration attempts per IP per hour
- CSRF protection on form submission
- Sanitize all input to prevent XSS

**Email Template**:
```html
Subject: Verify Your SRIBEESonline Account

Hi {{full_name}},

Welcome to SRIBEESonline! Please verify your email address by clicking the link below:

{{verification_link}}

This link will expire in 24 hours.

If you didn't create this account, please ignore this email.

Best regards,
The SRIBEESonline Team
```

#### Test Scenarios

1. **Happy Path**
   - Input: Valid email, strong password, matching confirm password, terms accepted
   - Expected: Success message, verification email sent, user created with is_verified=false

2. **Email Already Exists**
   - Input: Email that's already registered
   - Expected: Error "An account with this email already exists"

3. **Weak Password**
   - Input: Password "12345678"
   - Expected: Error "Password must contain uppercase, lowercase, number, and special character"

4. **Password Mismatch**
   - Input: Password "SecurePass123!", Confirm "DifferentPass123!"
   - Expected: Error "Passwords do not match"

5. **Terms Not Accepted**
   - Input: Valid data but terms checkbox unchecked
   - Expected: Error "You must accept the terms and conditions"

6. **Email Verification**
   - Action: Click verification link in email
   - Expected: Account verified, redirect to login with success message

7. **Expired Verification Token**
   - Action: Click verification link after 24 hours
   - Expected: Error "Verification link expired" with resend option

8. **Resend Verification**
   - Action: Click "Resend verification email"
   - Expected: New email sent, success message

#### Definition of Done

- [ ] All acceptance criteria met
- [ ] Unit tests written (>80% coverage)
- [ ] Integration tests written
- [ ] Code reviewed and approved
- [ ] Security review completed
- [ ] Email templates created and tested
- [ ] Documentation updated
- [ ] Deployed to staging and tested
- [ ] Product owner approval

---

### US-1.2: User Login

**Story ID**: US-1.2  
**Story Points**: 3  
**Priority**: High (Must Have)  
**Sprint**: Sprint 1  
**Dependencies**: US-1.1 (User Registration)

**As a** registered customer  
**I want to** log in to my account securely  
**So that** I can access my profile, orders, and make purchases

#### Acceptance Criteria

1. **Login Form**
   - ✅ Email and password input fields
   - ✅ "Remember me" checkbox
   - ✅ "Forgot password?" link
   - ✅ Submit button
   - ✅ Link to registration page

2. **Authentication**
   - ✅ Validate email and password
   - ✅ Check if account is verified
   - ✅ Generate JWT access token (15 min expiry)
   - ✅ Generate JWT refresh token (7 days expiry)
   - ✅ "Remember me" extends refresh token to 30 days

3. **Session Management**
   - ✅ Store tokens in httpOnly cookies
   - ✅ Session stored in Redis
   - ✅ Session ID in JWT payload
   - ✅ Session persists across browser tabs

4. **Security**
   - ✅ Account lockout after 5 failed attempts
   - ✅ Lockout duration: 15 minutes
   - ✅ Rate limiting: 10 login attempts per IP per minute
   - ✅ Log all login attempts (success and failure)

5. **User Experience**
   - ✅ Redirect to intended page after login (or homepage)
   - ✅ Clear error messages
   - ✅ Loading state during authentication
   - ✅ Success message on login

#### Technical Implementation

**Frontend Components**:
```
/components/auth/
  ├── LoginForm.jsx
  ├── RememberMeCheckbox.jsx
  └── ForgotPasswordLink.jsx
```

**API Endpoints**:
```
POST /api/v1/auth/login
Request Body:
{
  "email": "user@example.com",
  "password": "SecurePass123!",
  "remember_me": true
}

Response (200 OK):
{
  "success": true,
  "message": "Login successful",
  "user": {
    "user_id": "uuid",
    "email": "user@example.com",
    "full_name": "John Doe",
    "profile_picture_url": "url"
  },
  "access_token": "jwt-token",
  "refresh_token": "jwt-refresh-token"
}

Response (401 Unauthorized - Invalid Credentials):
{
  "success": false,
  "error": "Invalid email or password"
}

Response (403 Forbidden - Account Locked):
{
  "success": false,
  "error": "Account locked due to multiple failed login attempts. Try again in 15 minutes.",
  "locked_until": "2026-01-18T04:00:00Z"
}

Response (403 Forbidden - Unverified):
{
  "success": false,
  "error": "Please verify your email address before logging in",
  "resend_verification_available": true
}

POST /api/v1/auth/logout
Headers: Authorization: Bearer {access_token}

Response (200 OK):
{
  "success": true,
  "message": "Logged out successfully"
}

POST /api/v1/auth/refresh-token
Request Body:
{
  "refresh_token": "jwt-refresh-token"
}

Response (200 OK):
{
  "success": true,
  "access_token": "new-jwt-token",
  "refresh_token": "new-refresh-token"
}
```

**Database Schema**:
```sql
CREATE TABLE sessions (
  session_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES users(user_id) ON DELETE CASCADE,
  refresh_token_hash VARCHAR(255) NOT NULL,
  ip_address INET,
  user_agent TEXT,
  expires_at TIMESTAMP NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE login_attempts (
  attempt_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email VARCHAR(255) NOT NULL,
  ip_address INET NOT NULL,
  success BOOLEAN NOT NULL,
  failure_reason VARCHAR(100),
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_sessions_user_id ON sessions(user_id);
CREATE INDEX idx_login_attempts_email ON login_attempts(email);
CREATE INDEX idx_login_attempts_ip ON login_attempts(ip_address);
```

**Redis Schema (As-Built)**:
```
# Session Management Keys
sessions:{userId}:{sessionId} → {
  user_id: "uuid",
  email: "user@example.com",
  sessionId: "uuid",
  deviceInfo: "string",
  ipAddress: "string",
  userAgent: "string",
  createdAt: timestamp,
  lastActivityAt: timestamp,
  expiresAt: timestamp,
  isRememberMe: boolean
}
TTL: 7 days (standard) or 30 days (remember me)

# User's Active Sessions Set
user:sessions:{userId} → SET of sessionIds

# Token Blacklist
blacklist:token:{jti} → "1"
TTL: 24 hours

# User-wide Blacklist (logout all)
blacklist:user:{userId} → timestamp
No TTL (permanent until login)

# Rate Limiting
rate:login:{ip_address} → count
TTL: 15 minutes

# Account Lockout
account_lock:{user_id} → locked_until_timestamp
TTL: 15 minutes
```

**JWT Payload (As-Built)**:
```json
{
  "user_id": "uuid",
  "email": "user@example.com",
  "session_id": "uuid",
  "jti": "unique-token-id",  // For token blacklisting
  "type": "access",          // Or "refresh"
  "iat": 1705542000,
  "exp": 1705542900
}
```

**Security Implementation**:
- Password verification: Argon2id.verify(hash, password)
- JWT signing: RS256 algorithm with private key
- Token storage: httpOnly, secure, sameSite cookies
- CSRF token for state-changing operations

#### Test Scenarios

1. **Successful Login**
   - Input: Valid email and password
   - Expected: Tokens issued, redirected to homepage, session created

2. **Invalid Password**
   - Input: Correct email, wrong password
   - Expected: Error "Invalid email or password", attempt logged

3. **Unverified Account**
   - Input: Valid credentials for unverified account
   - Expected: Error "Please verify your email", resend option shown

4. **Account Lockout**
   - Input: 5 consecutive failed login attempts
   - Expected: Account locked, error message with lockout time

5. **Remember Me**
   - Input: Valid credentials with "Remember me" checked
   - Expected: Refresh token expires in 30 days instead of 7

6. **Session Persistence**
   - Action: Login, close browser, reopen
   - Expected: Still logged in (if remember me was checked)

7. **Logout**
   - Action: Click logout
   - Expected: Session destroyed, tokens invalidated, redirected to homepage

8. **Token Refresh**
   - Action: Access token expires, make API request
   - Expected: Automatic token refresh using refresh token

#### Definition of Done

- [ ] All acceptance criteria met
- [ ] Unit tests written (>80% coverage)
- [ ] Integration tests written
- [ ] Security testing completed
- [ ] Rate limiting tested
- [ ] Session management tested
- [ ] Code reviewed and approved
- [ ] Documentation updated
- [ ] Deployed to staging and tested

---

### US-1.3: Social Login (Google/Facebook)

**Story ID**: US-1.3  
**Story Points**: 5  
**Priority**: Medium (Should Have)  
**Sprint**: Sprint 2  
**Dependencies**: US-1.1, US-1.2

**As a** new or existing customer  
**I want to** log in using my Google or Facebook account  
**So that** I can quickly access the platform without creating a new password

#### Acceptance Criteria

1. **Social Login Buttons**
   - ✅ "Continue with Google" button with Google branding
   - ✅ "Continue with Facebook" button with Facebook branding
   - ✅ Buttons on login and registration pages
   - ✅ Proper OAuth flow initiated on click

2. **Google OAuth Integration**
   - ✅ OAuth 2.0 flow implemented
   - ✅ Scopes requested: email, profile
   - ✅ User consent screen shown
   - ✅ Authorization code exchange for tokens
   - ✅ User profile data retrieved

3. **Facebook OAuth Integration**
   - ✅ OAuth 2.0 flow implemented
   - ✅ Permissions requested: email, public_profile
   - ✅ User consent screen shown
   - ✅ Authorization code exchange for tokens
   - ✅ User profile data retrieved

4. **Account Creation**
   - ✅ New users automatically registered on first social login
   - ✅ Email from social provider used as primary email
   - ✅ Full name populated from social profile
   - ✅ Profile picture imported from social account
   - ✅ Account marked as verified (email already verified by provider)
   - ✅ No password required for social-only accounts

5. **Account Linking**
   - ✅ Existing users can link social accounts to their profile
   - ✅ If email matches existing account, link instead of creating new
   - ✅ User can link multiple social providers
   - ✅ User can unlink social accounts from settings
   - ✅ At least one login method must remain (password or social)

6. **Session Management**
   - ✅ Same JWT tokens issued as regular login
   - ✅ Session created in Redis
   - ✅ Social provider tokens stored securely

#### Technical Implementation

**Frontend Components**:
```
/components/auth/
  ├── GoogleLoginButton.jsx
  ├── FacebookLoginButton.jsx
  ├── SocialLoginSection.jsx
  └── SocialAccountManager.jsx (settings page)
```

**API Endpoints**:
```
GET /api/v1/auth/google
Response: Redirect to Google OAuth consent screen

GET /api/v1/auth/google/callback?code={authorization_code}
Response (200 OK):
{
  "success": true,
  "message": "Login successful",
  "user": {...},
  "access_token": "jwt-token",
  "refresh_token": "jwt-refresh-token",
  "is_new_user": true
}

GET /api/v1/auth/facebook
Response: Redirect to Facebook OAuth consent screen

GET /api/v1/auth/facebook/callback?code={authorization_code}
Response: Same as Google callback

POST /api/v1/users/social-accounts/link
Headers: Authorization: Bearer {access_token}
Request Body:
{
  "provider": "google",
  "code": "authorization_code"
}

DELETE /api/v1/users/social-accounts/:provider
Headers: Authorization: Bearer {access_token}
```

**Database Schema**:
```sql
CREATE TABLE social_accounts (
  social_account_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES users(user_id) ON DELETE CASCADE,
  provider VARCHAR(20) NOT NULL, -- 'google' or 'facebook'
  provider_user_id VARCHAR(255) NOT NULL,
  email VARCHAR(255),
  access_token TEXT,
  refresh_token TEXT,
  token_expires_at TIMESTAMP,
  profile_data JSONB,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(provider, provider_user_id)
);

CREATE INDEX idx_social_accounts_user_id ON social_accounts(user_id);
CREATE INDEX idx_social_accounts_provider ON social_accounts(provider, provider_user_id);
```

**Environment Variables**:
```
GOOGLE_CLIENT_ID=your-google-client-id
GOOGLE_CLIENT_SECRET=your-google-client-secret
GOOGLE_REDIRECT_URI=https://yourapp.com/api/v1/auth/google/callback

FACEBOOK_APP_ID=your-facebook-app-id
FACEBOOK_APP_SECRET=your-facebook-app-secret
FACEBOOK_REDIRECT_URI=https://yourapp.com/api/v1/auth/facebook/callback
```

**OAuth Flow**:
```
1. User clicks "Continue with Google"
2. Frontend redirects to /api/v1/auth/google
3. Backend redirects to Google OAuth consent screen
4. User grants permission
5. Google redirects to /api/v1/auth/google/callback?code=...
6. Backend exchanges code for access token
7. Backend fetches user profile from Google API
8. Backend checks if email exists in database
   - If yes: Link social account to existing user
   - If no: Create new user account
9. Backend creates session and issues JWT tokens
10. Backend redirects to frontend with tokens
11. Frontend stores tokens and redirects to homepage
```

**Security Considerations**:
- State parameter to prevent CSRF attacks
- Validate redirect URI
- Encrypt social provider tokens before storing
- Revoke tokens on account unlink
- Implement token refresh for long-lived sessions

#### Test Scenarios

1. **First-time Google Login**
   - Action: Click "Continue with Google", grant permissions
   - Expected: New account created, logged in, profile populated

2. **Existing User Google Login**
   - Action: User with Google account logs in
   - Expected: Logged in successfully, existing account used

3. **Link Google to Existing Account**
   - Action: Logged-in user links Google account
   - Expected: Google account linked, can login with Google

4. **Email Conflict**
   - Action: Social login with email that already exists
   - Expected: Accounts merged/linked, user notified

5. **OAuth Cancellation**
   - Action: User cancels OAuth consent screen
   - Expected: Redirected to login page with message "Login cancelled"

6. **Unlink Social Account**
   - Action: User unlinks Google account (has password)
   - Expected: Social account unlinked, can still login with password

7. **Prevent Unlinking Last Method**
   - Action: User tries to unlink only login method
   - Expected: Error "Cannot unlink. Set a password first."

8. **Facebook Login**
   - Action: Click "Continue with Facebook"
   - Expected: Same flow as Google, successful login

#### Definition of Done

- [ ] Google OAuth integration complete
- [ ] Facebook OAuth integration complete
- [ ] Account linking functionality working
- [ ] Security review completed
- [ ] Unit and integration tests written
- [ ] OAuth error handling tested
- [ ] Code reviewed and approved
- [ ] Documentation updated

---

### US-1.4: Password Reset

**Story ID**: US-1.4  
**Story Points**: 3  
**Priority**: High (Must Have)  
**Sprint**: Sprint 1  
**Dependencies**: US-1.1

**As a** customer who forgot my password  
**I want to** reset my password via email  
**So that** I can regain access to my account

#### Acceptance Criteria

1. **Forgot Password Page**
   - ✅ Email input field
   - ✅ Submit button
   - ✅ Link back to login page
   - ✅ Clear instructions

2. **Request Password Reset**
   - ✅ Validate email format
   - ✅ Send reset email if account exists
   - ✅ Show generic success message (prevent user enumeration)
   - ✅ Rate limit: Max 3 reset requests per email per hour
   - ✅ Generate secure reset token (32 bytes)
   - ✅ Token expires after 1 hour

3. **Reset Password Page**
   - ✅ New password input field
   - ✅ Confirm password input field
   - ✅ Password strength indicator
   - ✅ Submit button
   - ✅ Token validation on page load

4. **Password Update**
   - ✅ Validate new password meets requirements
   - ✅ Hash new password with Argon2id
   - ✅ Update password in database
   - ✅ Invalidate all existing sessions
   - ✅ Mark reset token as used
   - ✅ Send confirmation email

5. **Security**
   - ✅ Reset token can only be used once
   - ✅ Expired tokens show error with option to request new one
   - ✅ All sessions terminated after password reset
   - ✅ Log password reset events

#### Technical Implementation

**Frontend Components**:
```
/components/auth/
  ├── ForgotPasswordForm.jsx
  ├── ResetPasswordForm.jsx
  └── PasswordResetSuccess.jsx
```

**API Endpoints**:
```
POST /api/v1/auth/forgot-password
Request Body:
{
  "email": "user@example.com"
}

Response (200 OK):
{
  "success": true,
  "message": "If an account exists with this email, you will receive password reset instructions."
}

GET /api/v1/auth/validate-reset-token/:token
Response (200 OK):
{
  "success": true,
  "valid": true
}

Response (400 Bad Request - Expired):
{
  "success": false,
  "error": "Reset link has expired",
  "can_resend": true
}

POST /api/v1/auth/reset-password
Request Body:
{
  "token": "reset-token-here",
  "new_password": "NewSecurePass123!"
}

Response (200 OK):
{
  "success": true,
  "message": "Password reset successfully. Please login with your new password."
}
```

**Database Schema**:
```sql
CREATE TABLE password_resets (
  reset_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES users(user_id) ON DELETE CASCADE,
  token VARCHAR(255) UNIQUE NOT NULL,
  expires_at TIMESTAMP NOT NULL,
  used BOOLEAN DEFAULT FALSE,
  used_at TIMESTAMP,
  ip_address INET,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_password_resets_token ON password_resets(token);
CREATE INDEX idx_password_resets_user_id ON password_resets(user_id);
```

**Redis Rate Limiting**:
```
password_reset:{email} → count
TTL: 1 hour
Max: 3 requests
```

**Email Template**:
```html
Subject: Reset Your SRIBEESonline Password

Hi {{full_name}},

We received a request to reset your password. Click the link below to create a new password:

{{reset_link}}

This link will expire in 1 hour.

If you didn't request this, please ignore this email. Your password will remain unchanged.

Best regards,
The SRIBEESonline Team
```

**Password Reset Confirmation Email**:
```html
Subject: Your SRIBEESonline Password Was Reset

Hi {{full_name}},

Your password was successfully reset on {{date}} at {{time}}.

If you didn't make this change, please contact our support team immediately.

Best regards,
The SRIBEESonline Team
```

#### Test Scenarios

1. **Request Reset for Existing Account**
   - Input: Valid registered email
   - Expected: Generic success message, reset email sent

2. **Request Reset for Non-existent Account**
   - Input: Email not in database
   - Expected: Same generic success message (security), no email sent

3. **Click Valid Reset Link**
   - Action: Click link within 1 hour
   - Expected: Redirected to reset password form

4. **Click Expired Reset Link**
   - Action: Click link after 1 hour
   - Expected: Error "Link expired", option to request new link

5. **Set New Password**
   - Input: Valid new password
   - Expected: Password updated, all sessions invalidated, confirmation email sent

6. **Reuse Reset Token**
   - Action: Try to use same reset link twice
   - Expected: Error "Invalid or expired reset link"

7. **Rate Limiting**
   - Action: Request reset 4 times in 1 hour
   - Expected: 4th request shows error "Too many requests. Try again later."

8. **Login After Reset**
   - Action: Login with new password
   - Expected: Successful login

#### Definition of Done

- [ ] All acceptance criteria met
- [ ] Unit tests written
- [ ] Integration tests written
- [ ] Security testing completed
- [ ] Email templates created and tested
- [ ] Rate limiting tested
- [ ] Code reviewed and approved
- [ ] Documentation updated

---

## Summary Table: EPIC 1 User Stories

| Story ID | Title | Points | Priority | Sprint | Dependencies | Status |
|----------|-------|--------|----------|--------|--------------|--------|
| US-1.1 | User Registration | 5 | High | 1 | None | ✅ Implemented |
| US-1.2 | User Login | 3 | High | 1 | US-1.1 | ✅ Implemented |
| US-1.3 | Social Login | 5 | Medium | 2 | US-1.1, US-1.2 | ⏸️ Planned |
| US-1.4 | Password Reset | 3 | High | 1 | US-1.1 | ✅ Implemented |
| US-1.5 | Profile Management | 3 | Medium | 2 | US-1.2 | ✅ Implemented |
| US-1.6 | Address Management | 3 | High | 2 | US-1.2 | ✅ Implemented |
| US-1.7 | Two-Factor Authentication | 5 | Low | 3 | US-1.2 | ⏸️ Planned |

**Total Story Points for EPIC 1**: 27 points (~3-4 weeks)

---

## 🆕 Additional User Stories (As-Built)

### US-ADMIN-1: Multi-Branch RBAC System

**Story ID**: US-ADMIN-1  
**Story Points**: 13  
**Priority**: High (Must Have)  
**Sprint**: Sprint 3-4  
**Dependencies**: US-1.2  
**Status**: ✅ Implemented

**As an** administrator  
**I want to** manage multiple branch locations with role-based access control  
**So that** each branch can operate independently while maintaining central oversight

#### Acceptance Criteria (Implemented)

1. **Admin Roles**
   - ✅ Super Admin: Full system access, all branches
   - ✅ Branch Manager: Own branch only, limited management
   - ✅ Staff: Basic operations, own branch only
   - ✅ Support: Customer support, cross-branch read access
   - ✅ Inventory: Stock management, cross-branch

2. **Branch Isolation**
   - ✅ Automatic branch filter injection for restricted roles
   - ✅ SQL queries include branch_id filter automatically
   - ✅ API responses scoped to user's branch
   - ✅ Super Admin can switch between branches

3. **Database Tables (Implemented)**
```sql
-- Branches
CREATE TABLE branches (
    branch_id UUID PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    code VARCHAR(10) UNIQUE NOT NULL,
    address TEXT,
    city VARCHAR(100),
    phone VARCHAR(20),
    manager_id UUID REFERENCES admin_users,
    is_active BOOLEAN DEFAULT TRUE
);

-- Admin Users with Branch Assignment
CREATE TABLE admin_users (
    admin_id UUID PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    full_name VARCHAR(100) NOT NULL,
    role admin_role_enum NOT NULL,
    branch_id UUID REFERENCES branches,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Admin Audit Logs
CREATE TABLE admin_audit_logs (
    log_id UUID PRIMARY KEY,
    admin_id UUID REFERENCES admin_users,
    branch_id UUID REFERENCES branches,
    action VARCHAR(100) NOT NULL,
    resource_type VARCHAR(100) NOT NULL,
    resource_id VARCHAR(255),
    old_value JSONB,
    new_value JSONB,
    ip_address INET,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

4. **Branch Inventory**
```sql
CREATE TABLE branch_inventory (
    inventory_id UUID PRIMARY KEY,
    branch_id UUID REFERENCES branches NOT NULL,
    product_id UUID REFERENCES products NOT NULL,
    variant_id UUID REFERENCES product_variants,
    stock_quantity INTEGER DEFAULT 0,
    reserved_quantity INTEGER DEFAULT 0,
    low_stock_threshold INTEGER DEFAULT 10,
    UNIQUE(branch_id, product_id, variant_id)
);

CREATE TABLE stock_transfers (
    transfer_id UUID PRIMARY KEY,
    from_branch_id UUID REFERENCES branches,
    to_branch_id UUID REFERENCES branches NOT NULL,
    product_id UUID REFERENCES products NOT NULL,
    variant_id UUID REFERENCES product_variants,
    quantity INTEGER NOT NULL,
    status transfer_status_enum DEFAULT 'pending',
    requested_by_id UUID REFERENCES admin_users,
    approved_by_id UUID REFERENCES admin_users,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

### US-PRODUCT-1: Product Variants System

**Story ID**: US-PRODUCT-1  
**Story Points**: 8  
**Priority**: High  
**Sprint**: Sprint 4  
**Status**: ✅ Implemented

**As a** customer  
**I want to** select product variants (size, color, weight)  
**So that** I can purchase exactly what I need

#### Database Schema (Implemented)
```sql
CREATE TABLE variant_types (
    variant_type_id UUID PRIMARY KEY,
    name VARCHAR(50) UNIQUE NOT NULL,
    display_name VARCHAR(100) NOT NULL,
    display_order INTEGER DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE
);

CREATE TABLE variant_options (
    variant_option_id UUID PRIMARY KEY,
    variant_type_id UUID REFERENCES variant_types,
    value VARCHAR(100) NOT NULL,
    display_value VARCHAR(100),
    color_hex CHAR(7),  -- For color swatches
    display_order INTEGER DEFAULT 0
);

CREATE TABLE product_variants (
    variant_id UUID PRIMARY KEY,
    product_id UUID REFERENCES products ON DELETE CASCADE,
    sku VARCHAR(100) UNIQUE,
    name VARCHAR(255),
    price DECIMAL(10, 2) NOT NULL,
    compare_at_price DECIMAL(10, 2),
    stock_quantity INTEGER DEFAULT 0,
    weight DECIMAL(10, 3),
    image_url TEXT,
    is_default BOOLEAN DEFAULT FALSE,
    is_active BOOLEAN DEFAULT TRUE
);

CREATE TABLE product_variant_options (
    variant_id UUID REFERENCES product_variants ON DELETE CASCADE,
    variant_type_id UUID REFERENCES variant_types,
    variant_option_id UUID REFERENCES variant_options,
    PRIMARY KEY (variant_id, variant_type_id)
);
```

---

### US-WISHLIST-1: Variant-Aware Wishlist

**Story ID**: US-WISHLIST-1  
**Story Points**: 5  
**Priority**: Medium  
**Sprint**: Sprint 5  
**Status**: ✅ Implemented

**As a** customer  
**I want to** add specific product variants to my wishlist with price tracking  
**So that** I can be notified when prices drop

#### Implementation Details (As-Built)

**Database Schema**:
```sql
CREATE TABLE wishlist_items (
    wishlist_item_id UUID PRIMARY KEY,
    user_id UUID REFERENCES users NOT NULL,
    product_id VARCHAR(255) NOT NULL,
    variant_id UUID REFERENCES product_variants,
    price_at_watch DECIMAL(10, 2) NOT NULL,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, product_id, variant_id)
);
```

**Redis Cache**:
```
Key: watchlist:{userId}
Type: Redis SET
Value: "{productId}:{variantId}" strings
TTL: 7 days (auto-renewed on access)

Operations:
SADD    - Add item (O(1))
SREM    - Remove item (O(1))
SISMEMBER - Check if exists (O(1))
SMEMBERS  - Get all items (O(n))
```

---

*Note: This document has been updated to reflect the as-built implementation state. Additional EPICs and stories should follow the same pattern.*

---

## Document Metadata

**Version**: 2.0 (As-Built)  
**Last Updated**: January 29, 2026  
**Author**: Development Team  
**Status**: Updated to reflect implementation  
**Coverage**: EPIC 1 Complete + Admin RBAC + Product Variants + Wishlist
