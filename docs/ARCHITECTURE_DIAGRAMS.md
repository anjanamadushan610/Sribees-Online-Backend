# SRIBEESonline - Architecture Diagrams for Eraser.io

> Professional architecture diagrams for the SRIBEESonline e-commerce platform

---

## 📋 How to Use These Diagrams

1. Go to [Eraser.io](https://app.eraser.io/)
2. Create a new diagram
3. Copy the code from any section below
4. Paste into Eraser.io's diagram editor
5. The diagram will render automatically
6. Customize colors, layout, and styling as needed

---

## 1. System Architecture Overview

**Description**: High-level system architecture showing all major components

```eraser
// SRIBEESonline System Architecture

title SRIBEESonline E-Commerce Platform - System Architecture

// Client Layer
Client Applications [icon: monitor] {
  Web App (Next.js) [color: blue]
  Mobile App (Flutter) [color: blue]
  Admin Dashboard (React + Vite) [color: purple]
}

// API Gateway Layer
FastAPI Backend [icon: cloud, color: orange] {
  FastAPI Application
  Authentication (JWT)
  Rate Limiter (slowapi)
  CORS Middleware
}

// Services Layer
Services [icon: server, color: green] {
  Auth Service
  Product Service
  Cart Service
  Order Service
  Payment Service
  Notification Service
  Category Service
  Wishlist Service
}

// Data Layer
Databases [icon: database, color: red] {
  PostgreSQL (Primary DB)
  Redis (Cache/Sessions)
  Elasticsearch (Search)
}

// External Services
External Services [icon: globe, color: yellow] {
  Stripe/Razorpay (Payment)
  SendGrid (Email)
  Twilio (SMS)
  AWS S3 (Storage)
  Firebase (Push Notifications)
}

// Connections
Client Applications > FastAPI Backend
FastAPI Backend > Services
Services > Databases
Services > External Services
```

---

## 2. Modular Monolith Architecture

**Description**: Detailed microservices breakdown with inter-service communication

```eraser
title SRIBEESonline Microservices Architecture

// Frontend Layer
Web Application [icon: monitor, color: blue]
Mobile Application [icon: smartphone, color: blue]
Admin Panel [icon: settings, color: purple]

// API Gateway
API Gateway [icon: cloud, color: orange] {
  Kong Gateway
  JWT Validation
  Rate Limiting
  Request Routing
}

// Service Layer
User Service [icon: user, color: green] {
  Authentication
  Profile Management
  Address Management
  2FA
}

Product Service [icon: package, color: green] {
  Product CRUD
  Category Management
  Inventory Tracking
  Product Search Sync
}

Cart Service [icon: shopping-cart, color: green] {
  Add/Remove Items
  Quantity Updates
  Coupon Application
  Price Calculation
}

Order Service [icon: file-text, color: green] {
  Order Creation
  Order Tracking
  Order History
  Returns/Refunds
}

Payment Service [icon: credit-card, color: green] {
  Payment Processing
  Gateway Integration
  Refund Handling
  Transaction Logs
}

Notification Service [icon: bell, color: green] {
  Email Notifications
  SMS Alerts
  Push Notifications
  Queue Management
}

Search Service [icon: search, color: green] {
  Product Search
  Autocomplete
  Filters & Sorting
  Search Analytics
}

Review Service [icon: star, color: green] {
  Review CRUD
  Rating Calculation
  Review Moderation
  Helpful Votes
}

// Data Stores
PostgreSQL [icon: database, color: red]
MongoDB [icon: database, color: red]
Redis [icon: database, color: red]
Elasticsearch [icon: database, color: red]

// Message Queue
RabbitMQ [icon: message-square, color: pink]

// Connections
Web Application > API Gateway
Mobile Application > API Gateway
Admin Panel > API Gateway

API Gateway > User Service
API Gateway > Product Service
API Gateway > Cart Service
API Gateway > Order Service
API Gateway > Payment Service
API Gateway > Notification Service
API Gateway > Search Service
API Gateway > Review Service

User Service > PostgreSQL
Order Service > PostgreSQL
Payment Service > PostgreSQL
Review Service > PostgreSQL

Product Service > MongoDB
Product Service > Elasticsearch

Cart Service > Redis
User Service > Redis

Order Service > Notification Service: Order Events
Payment Service > Notification Service: Payment Events
Product Service > Search Service: Index Updates

Notification Service > RabbitMQ
```

---

## 3. Database Architecture

**Description**: Database schema and relationships

```eraser
title SRIBEESonline Database Architecture

// PostgreSQL - Transactional Data
PostgreSQL Primary [icon: database, color: red] {
  Users Table
  Addresses Table
  Orders Table
  Order Items Table
  Payments Table
  Reviews Table
  Sessions Table
  Social Accounts Table
  Coupons Table
  Password Resets Table
}

// MongoDB - Product Catalog
MongoDB [icon: database, color: green] {
  Products Collection
  Categories Collection
  Brands Collection
  Product Attributes
}

// Redis - Caching & Sessions
Redis Cache [icon: database, color: orange] {
  User Sessions
  Cart Data
  Product Cache
  Rate Limiting
  OTP Storage
}

// Elasticsearch - Search
Elasticsearch [icon: database, color: blue] {
  Product Index
  Search Analytics
  Autocomplete Data
}

// Relationships
PostgreSQL Primary > MongoDB: Product References
PostgreSQL Primary > Redis Cache: Session Storage
MongoDB > Elasticsearch: Search Indexing
```

---

## 4. User Authentication Flow

**Description**: Complete authentication and authorization flow

```eraser
title User Authentication Flow

User [icon: user]
Web App [icon: monitor, color: blue]
API Gateway [icon: cloud, color: orange]
Auth Service [icon: shield, color: green]
PostgreSQL [icon: database, color: red]
Redis [icon: database, color: orange]

// Login Flow (staff: the admin panel. Customers have no password -- phone OTP, Google or Apple)
User > Web App: Enter credentials
Web App > API Gateway: POST /admin/auth/login
API Gateway > Auth Service: Validate request
Auth Service > PostgreSQL: Query user
PostgreSQL > Auth Service: User data
Auth Service > Auth Service: Verify password (Argon2)
Auth Service > Redis: Create session
Auth Service > Auth Service: Generate JWT tokens
Auth Service > API Gateway: Return tokens
API Gateway > Web App: Access + Refresh tokens
Web App > Web App: Store in httpOnly cookies
Web App > User: Login successful

// Token Refresh Flow
Web App > API Gateway: Request with expired token
API Gateway > Auth Service: Refresh token
Auth Service > Redis: Validate session
Auth Service > Auth Service: Generate new tokens
Auth Service > API Gateway: New tokens
API Gateway > Web App: Updated tokens
```

---

## 5. Order Processing Flow

**Description**: End-to-end order processing workflow

```eraser
title Order Processing Workflow

Customer [icon: user]
Web App [icon: monitor, color: blue]
Cart Service [icon: shopping-cart, color: green]
Order Service [icon: file-text, color: green]
Payment Service [icon: credit-card, color: green]
Inventory Service [icon: package, color: green]
Notification Service [icon: bell, color: green]
PostgreSQL [icon: database, color: red]

// Order Flow
Customer > Web App: Add items to cart
Web App > Cart Service: Add items
Cart Service > Cart Service: Calculate totals
Customer > Web App: Proceed to checkout
Web App > Order Service: Create order
Order Service > Inventory Service: Check stock
Inventory Service > Order Service: Stock confirmed
Order Service > PostgreSQL: Save order
Order Service > Payment Service: Initiate payment
Payment Service > Payment Service: Process payment
Payment Service > PostgreSQL: Save transaction
Payment Service > Order Service: Payment confirmed
Order Service > Inventory Service: Deduct stock
Order Service > Notification Service: Send confirmation
Notification Service > Customer: Email + SMS
Order Service > Web App: Order confirmed
Web App > Customer: Show confirmation
```

---

## 6. Payment Processing Architecture

**Description**: Secure payment processing flow

```eraser
title Payment Processing Architecture

Customer [icon: user]
Frontend [icon: monitor, color: blue]
Payment Service [icon: credit-card, color: green]
Payment Gateway [icon: shield, color: purple] {
  Stripe API
  Razorpay API
}
PostgreSQL [icon: database, color: red]
Notification Service [icon: bell, color: green]

// Payment Flow
Customer > Frontend: Enter payment details
Frontend > Frontend: Client-side validation
Frontend > Payment Service: Create payment intent
Payment Service > Payment Gateway: Initialize payment
Payment Gateway > Payment Service: Payment intent created
Payment Service > Frontend: Client secret
Frontend > Payment Gateway: Confirm payment (3D Secure)
Payment Gateway > Payment Gateway: Process payment
Payment Gateway > Payment Service: Webhook - Payment success
Payment Service > PostgreSQL: Update payment status
Payment Service > Notification Service: Payment confirmed
Notification Service > Customer: Confirmation email
Payment Service > Frontend: Payment successful
```

---

## 7. Search Architecture

**Description**: Product search and indexing system

```eraser
title Product Search Architecture

User [icon: user]
Web App [icon: monitor, color: blue]
Search Service [icon: search, color: green]
Elasticsearch [icon: database, color: blue]
Product Service [icon: package, color: green]
MongoDB [icon: database, color: green]

// Search Flow
User > Web App: Enter search query
Web App > Search Service: GET /search?q=query
Search Service > Elasticsearch: Full-text search
Elasticsearch > Search Service: Search results
Search Service > Search Service: Apply filters & sorting
Search Service > Web App: Formatted results
Web App > User: Display products

// Indexing Flow
Product Service > MongoDB: Product updated
Product Service > Search Service: Index update event
Search Service > Elasticsearch: Update product index
Elasticsearch > Search Service: Index updated
```

---

## 8. Notification System Architecture

**Description**: Multi-channel notification delivery

```eraser
title Notification System Architecture

Trigger Events [icon: zap, color: yellow] {
  Order Created
  Payment Confirmed
  Order Shipped
  Order Delivered
  Password Reset
}

Notification Service [icon: bell, color: green] {
  Event Listener
  Template Engine
  Queue Manager
  Delivery Router
}

Message Queue [icon: message-square, color: pink] {
  RabbitMQ
  Bull Queue
}

Delivery Channels [icon: send, color: blue] {
  Email (SendGrid)
  SMS (Twilio)
  Push (Firebase)
  In-App
}

User Preferences [icon: settings, color: purple]
PostgreSQL [icon: database, color: red]

// Flow
Trigger Events > Notification Service: Event triggered
Notification Service > User Preferences: Check preferences
User Preferences > Notification Service: User settings
Notification Service > Notification Service: Render template
Notification Service > Message Queue: Queue message
Message Queue > Delivery Channels: Deliver notification
Delivery Channels > PostgreSQL: Log delivery status
```

---

## 9. Admin Dashboard Architecture

**Description**: Admin panel system architecture

```eraser
title Admin Dashboard Architecture

Admin User [icon: user-check, color: purple]
Admin Panel [icon: settings, color: purple]
API Gateway [icon: cloud, color: orange]

Admin Services [icon: server, color: green] {
  Product Management
  Order Management
  Customer Management
  Inventory Management
  Analytics Service
  Reports Service
  Settings Service
}

Databases [icon: database, color: red] {
  PostgreSQL
  MongoDB
  Redis
}

Analytics Engine [icon: bar-chart, color: blue] {
  Sales Analytics
  Customer Analytics
  Product Performance
  Revenue Reports
}

// Connections
Admin User > Admin Panel: Access dashboard
Admin Panel > API Gateway: Authenticated requests
API Gateway > Admin Services: Route to services
Admin Services > Databases: CRUD operations
Admin Services > Analytics Engine: Generate reports
Analytics Engine > Admin Panel: Display metrics
```

---

## 10. Deployment Architecture (AWS)

**Description**: Cloud infrastructure on AWS

```eraser
title AWS Deployment Architecture

// DNS & CDN
Route 53 [icon: globe, color: blue]
CloudFront CDN [icon: cloud, color: blue]

// Load Balancing
Application Load Balancer [icon: server, color: orange]

// Compute
ECS Cluster [icon: box, color: green] {
  Web App Containers
  API Gateway Containers
  Microservices Containers
}

// Databases
RDS PostgreSQL [icon: database, color: red]
DocumentDB MongoDB [icon: database, color: red]
ElastiCache Redis [icon: database, color: orange]
Elasticsearch Service [icon: database, color: blue]

// Storage
S3 Buckets [icon: folder, color: yellow] {
  Product Images
  User Uploads
  Static Assets
  Backups
}

// Monitoring
CloudWatch [icon: activity, color: purple]
X-Ray [icon: eye, color: purple]

// Security
WAF [icon: shield, color: red]
Secrets Manager [icon: key, color: red]

// Flow
Route 53 > CloudFront CDN
CloudFront CDN > WAF
WAF > Application Load Balancer
Application Load Balancer > ECS Cluster
ECS Cluster > RDS PostgreSQL
ECS Cluster > DocumentDB MongoDB
ECS Cluster > ElastiCache Redis
ECS Cluster > Elasticsearch Service
ECS Cluster > S3 Buckets
ECS Cluster > CloudWatch
CloudWatch > X-Ray
```

---

## 11. CI/CD Pipeline

**Description**: Continuous integration and deployment workflow

```eraser
title CI/CD Pipeline

Developer [icon: user]
GitHub [icon: github, color: black]
GitHub Actions [icon: zap, color: blue]

Build Stage [icon: package, color: green] {
  Install Dependencies
  Run Linters
  Run Unit Tests
  Build Docker Images
}

Test Stage [icon: check-circle, color: yellow] {
  Integration Tests
  E2E Tests
  Security Scans
  Code Coverage
}

Deploy Stage [icon: upload-cloud, color: purple] {
  Push to ECR
  Update ECS Services
  Run Migrations
  Health Checks
}

Environments [icon: server, color: orange] {
  Staging
  Production
}

Monitoring [icon: activity, color: red] {
  CloudWatch Logs
  Error Tracking
  Performance Metrics
}

// Workflow
Developer > GitHub: Push code
GitHub > GitHub Actions: Trigger workflow
GitHub Actions > Build Stage: Build & test
Build Stage > Test Stage: Run tests
Test Stage > Deploy Stage: Deploy if passed
Deploy Stage > Environments: Deploy to staging
Environments > Monitoring: Monitor deployment
Deploy Stage > Environments: Manual approval for production
```

---

## 12. Data Flow Diagram

**Description**: Data flow through the system

```eraser
title Data Flow Diagram

User Input [icon: user, color: blue]

Frontend Layer [icon: monitor, color: blue] {
  User Interface
  State Management
  API Client
}

API Layer [icon: cloud, color: orange] {
  API Gateway
  Authentication
  Rate Limiting
}

Business Logic [icon: server, color: green] {
  Microservices
  Business Rules
  Validation
}

Data Layer [icon: database, color: red] {
  PostgreSQL
  MongoDB
  Redis
  Elasticsearch
}

External APIs [icon: globe, color: yellow] {
  Payment Gateway
  Email Service
  SMS Service
}

User Output [icon: user, color: blue]

// Flow
User Input > Frontend Layer: User actions
Frontend Layer > API Layer: HTTP requests
API Layer > Business Logic: Validated requests
Business Logic > Data Layer: CRUD operations
Business Logic > External APIs: Third-party calls
Data Layer > Business Logic: Query results
External APIs > Business Logic: API responses
Business Logic > API Layer: Processed data
API Layer > Frontend Layer: JSON responses
Frontend Layer > User Output: Rendered UI
```

---

## 13. Security Architecture

**Description**: Security layers and mechanisms

```eraser
title Security Architecture

User [icon: user]

Security Layers [icon: shield, color: red] {
  WAF (Web Application Firewall)
  DDoS Protection
  SSL/TLS Encryption
  CORS Policy
}

Authentication [icon: key, color: orange] {
  JWT Tokens
  OAuth 2.0
  2FA
  Session Management
}

Authorization [icon: lock, color: yellow] {
  Role-Based Access Control
  Permission Checks
  API Key Validation
}

Data Protection [icon: database, color: green] {
  Encryption at Rest
  Encryption in Transit
  Password Hashing (Argon2)
  PII Masking
}

Monitoring [icon: eye, color: purple] {
  Audit Logs
  Security Alerts
  Intrusion Detection
  Vulnerability Scanning
}

// Flow
User > Security Layers: All requests
Security Layers > Authentication: Verify identity
Authentication > Authorization: Check permissions
Authorization > Data Protection: Access data
Data Protection > Monitoring: Log activity
```

---

## Usage Instructions

### For Eraser.io:

1. **Create New Diagram**
   - Go to https://app.eraser.io/
   - Click "New Diagram"
   - Select "Diagram as Code"

2. **Copy & Paste**
   - Copy any diagram code from above
   - Paste into Eraser.io editor
   - Diagram renders automatically

3. **Customize**
   - Adjust colors using `[color: colorname]`
   - Add icons using `[icon: iconname]`
   - Modify layout and connections
   - Export as PNG, SVG, or PDF

4. **Color Options**
   - blue, green, red, yellow, orange, purple, pink, black, gray

5. **Icon Options**
   - user, monitor, server, database, cloud, shield, key, lock, etc.

### Tips for Professional Diagrams:

- Use consistent color schemes (e.g., blue for frontend, green for services, red for databases)
- Group related components together
- Keep connection lines clear and minimal
- Add descriptive titles to each diagram
- Use icons to make diagrams more visual
- Export in high resolution for presentations

---

*Document Version: 1.0*  
*Last Updated: January 18, 2026*  
*Total Diagrams: 13*
