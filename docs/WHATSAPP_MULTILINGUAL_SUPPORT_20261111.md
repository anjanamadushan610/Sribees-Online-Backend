# Sribees Online WhatsApp support implementation

## Existing architecture and audit

The backend uses FastAPI, async SQLAlchemy and PostgreSQL. Redis stores branch context and other shared application state. The existing WhatsApp webhook is `GET/POST /api/v1/whatsapp/webhook`. It verifies the Meta App Secret signature, validates the configured phone number ID, deduplicates inbound message IDs and commits queued replies before acknowledging receipt. `whatsapp_worker.py` sends durable replies and `whatsapp_receipts.py` reconciles delivery statuses. The existing staff interface is Admin Support Tickets with claim, release, history and queued replies.

The existing tables are `whatsapp_conversations`, `whatsapp_replies`, `support_tickets`, `users`, `orders`, `order_items`, `payment_attempts`, `wallets`, coupons, free-delivery grants and referral benefits. Existing ticket statuses are pending, in_progress, resolved and closed. Existing order/payment enums are mapped exhaustively in `whatsapp_support.py`.

The earlier bot offered a three-button English/Sinhala menu, delivery/app information and generic support requests. It lacked first-contact language selection, Tamil, persistent input stages, order/ticket ownership checks, structured complaints, private media access and ticket email notifications.

A read-only public check on 2026-11-11 confirmed that the production webhook rejects an unauthenticated verification request with HTTP 403. GitHub contains the six existing WHATSAPP configuration secret names. This check does not establish the runtime WABA ID, subscription, phone-number value, token scope or actual delivery of this new implementation. Existing successful live staff chat was tested earlier in this project.

## Implementation and reuse

- Language selection offers English, Sinhala and Tamil in that order. The selected language persists on the existing conversation row. Existing rows retain their saved language after migration.
- Main, orders, delivery complaints, item complaints, account, payment, offers and human-support menus use the existing Cloud API transport. List rows have short titles within Meta limits. Longer localized labels use row descriptions. Universal commands are MENU, BACK, LANGUAGE and CANCEL.
- Inputs persist in conversation stage/context. Arbitrary text does not select a menu action. New-customer greetings show language selection. Incomplete automated requests expire after 24 hours. Staff-owned conversations remain attached until staff release/closure.
- Customers must match exactly one active, phone-verified, unblocked, non-deleted user using canonical phone normalization. Order queries include that user's ID. Ticket queries require the exact ticket reference and normalized sender phone. Existence and ownership failures share a safe response.
- Tracking reads actual order/payment status, the customer's delivery address and an existing delivery slot when recorded. The bot does not calculate a delivery estimate.
- Cancellation uses the existing `OrderService.customer_can_cancel` and cancellation-state rules. An unbooked order requires a reason and an explicit confirmation, rechecks the locked order and calls the existing cancellation service inside the inbound transaction. The optional `commit=False` argument preserves the default behavior for existing callers. Courier-booked cancellations create staff requests because delivery charges and wallet settlement require discussion. Delivered and terminal orders are refused.
- Item complaints collect the owned order, product, quantity, description and optional photo. Payment complaints collect the order, method, reference, description and optional receipt. Customers without an order number can skip it for failed/debited/other payment complaints. Payment confirmation reads the actual order payment status. Refund inquiries display the recorded refund amount/status and collect a staff investigation request.
- Account guidance uses existing phone, Google and Apple authentication and the app's verified account-deletion route. Android's official Play URL is provided. An iOS download link is withheld until public availability is verified.
- Current coupons reuse `get_available_coupons` including branch, expiry, audience and usage checks. Free-delivery summaries read existing personal grants, the selected branch offer and referral-benefit summary. Eligibility remains subject to the existing checkout rules. Wallet reads the existing Sribees Wallet balance and configured cashback remains product dependent.
- Human-agent selection creates one existing SupportTicket immediately. Repeated messages attach to that ticket. The claim/release UI and permission checks are reused. Staff takeover pauses automated menus even when the customer types MENU. Release/closure restores automated navigation.
- Previous inquiry-stage messages are linked to the created ticket, along with the final message and following messages. The existing message log remains the conversation history.
- Images/PDFs retain only Meta media IDs and MIME references in PostgreSQL. Staff use a new authenticated ticket-scoped attachment endpoint. The server obtains the current private Meta media URL, validates its host, refuses redirects, limits files to 5 MB and returns a private no-store download. The Admin chat downloads it through the existing authorized API client.
- New support tickets queue one staff email through the existing SMTP sender when configured. Email includes the reference/category/language and directs staff to the dashboard. Volunteered customer text and attachments stay out of email. Missing SMTP leaves the ticket pending for notification. Failed/ambiguous email sends are recorded for inspection instead of repeatedly emailing staff.
- Ticket status changes queue a localized customer update inside the existing service window. Outside the window, a new customer message or an approved template is required. No template approval is assumed.
- Webhook signatures, deduplication and delivery-receipt handling remain active. State changes use savepoints. Exceptions produce a localized safe fallback. Per-sender inbound rate checks suppress reply amplification beyond 30 messages per minute. Credential-like text and card-number-length digit sequences are redacted before retention.

## Files

Existing backend files modified

- `app/models/whatsapp.py`
- `app/models/support_ticket.py`
- `app/services/whatsapp_service.py`
- `app/services/whatsapp_menu.py`
- `app/services/whatsapp_worker.py`
- `app/services/order_service.py`
- `app/api/v1/admin_whatsapp.py`
- `app/api/v1/admin_support.py`
- Existing WhatsApp unit and PostgreSQL integration tests

New backend files

- `app/services/whatsapp_content.py` holds all three-language text and menus
- `app/services/whatsapp_support.py` implements support states and verified lookups
- `app/services/whatsapp_benefits.py` reads existing delivery benefits
- `app/services/whatsapp_media.py` provides private attachment retrieval
- `app/services/whatsapp_notifications.py` queues status updates and staff email
- `migrations/108_whatsapp_multilingual_support.sql`
- `tests/test_whatsapp_multilingual.py`

Admin frontend files modified in its separate checkout

- `src/api/support.api.ts`
- `src/pages/Support/WhatsAppTicketChat.tsx`
- `src/pages/Support/WhatsAppTicketChat.test.tsx`

## Database and API changes

Migration 108 extends existing conversation records with language_selected, menu, stage and context, extends reply records with inbound_media, and extends tickets with preferred_language, related_order_id and notification_status. It adds order-reference, pending-notification and sender/time indexes. Statements are replayable. The migration must run before the new backend starts.

The webhook URL stays `https://api.sribeesonline.com/api/v1/whatsapp/webhook`.

New staff endpoint

`GET /api/v1/admin/support/tickets/{ticket_id}/whatsapp/messages/{message_id}/attachment`

Existing staff history includes attachment availability and ticket language/order/email-status metadata. Staff status updates and release actions reuse their existing routes.

## Environment and Meta setup

Existing server-side values are WHATSAPP_ENABLED, WHATSAPP_ACCESS_TOKEN, WHATSAPP_APP_SECRET, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_VERIFY_TOKEN and WHATSAPP_GRAPH_VERSION. Existing SMTP MAIL_* values are needed for email delivery to support@sribeesonline.com. No credentials belong in frontend code or this document.

Meta must retain the published app, intended business phone, messages webhook subscription and token permissions for messaging/media access. Runtime WABA ownership and the exact phone number ID still need an authenticated Meta check. Existing webhook verification stays in place if the deployment uses the same secret and callback.

Outside-window customer messages require an actually approved template or a new customer message. This implementation does not create or assume approved templates. The email worker reports configuration/delivery separately from ticket creation. No staff availability roster exists in the current implementation, so existing staff claim/assignment is used.

## Verification and release status

Local verification completed on 2026-11-11. Backend unit tests passed with 2,152 passed and 22 skipped. Ruff and git diff whitespace checks passed. Admin TypeScript checking, four focused chat tests and the production build passed. The Admin build retains its bundle-size warning. PostgreSQL integration, migration replay, GitHub security CI and live three-language WhatsApp verification remain pending. Git commit/push was blocked by automatic approval review because the earlier project rules prohibit it. A new explicit approval has been requested. Local unit tests use synthetic fixtures. PostgreSQL integration tests refuse production databases and run against a disposable test database in CI. Actual live three-language WhatsApp conversations, staff attachments and any cancellation/refund behavior must be verified using test accounts before declaring this implementation production-ready.

This branch implements the code. Deployment and live-message delivery must be recorded explicitly after they occur. Existing production behavior is not evidence that the new branch has been deployed.
