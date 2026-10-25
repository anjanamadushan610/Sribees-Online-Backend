# Sribees shopping assistant

The assistant supports Sinhala Unicode, English and Tamil. Sinhala remains the primary localized experience, including when a customer types Singlish. Product facts, prices, stock, basket state, order state and saved preferences come from application services rather than model claims.

## Supported customer flows

- Search the selected branch's real catalog and show product cards.
- Track the signed-in customer's orders and show a localized timeline.
- Explain app flows and open allowlisted app pages.
- Compare up to four real products. The server calculates the current effective price, cashback and per-100 g/ml price when a single explicit pack size can be verified from the catalog name.
- Find same-category alternatives without silently substituting an item.
- Add, set or remove basket items conversationally. The assistant first shows a ten-minute preview; the customer must confirm it before Redis changes.
- View, save or selectively forget favourite brands, ingredients to avoid, household size and budget. Every save/delete is previewed and explicitly confirmed.
- Suggest replenishment intervals from the customer's own delivered orders and create, snooze or stop optional reminders. Nothing is enabled from purchase history alone.
- Suggest recipes from explicitly named pantry ingredients and search the catalog only for missing ingredients.

Native shortcuts for tracking, recent orders, app help, saved preferences, reminders, reminder suggestions and pantry recipes work without model classification. Natural-language requests use a validated Gemini JSON plan.

## Tool-using agent (`ASSISTANT_AGENT_V2`, on since 2026-10-25; `false` restores the planner)

`app/services/assistant_agent.py` answers free-text messages as a loop instead
of one classify-then-template call. The model calls tools -- `search_products`,
`my_orders`, `app_help`, `stage_basket_change`, `compare_products`,
`saved_preferences`, `reminders`, `pantry_recipes` -- reads their results, may
search again when nothing matched, can serve several requests in one message,
and finishes with `respond(text, show_product_ids)`. At most
`ASSISTANT_AGENT_MAX_ROUNDS` model calls (default 4); the last may only respond.

Every tool is an existing server function, so writes are still previews the
customer confirms. After the model answers, the server keeps only product IDs a
tool returned this turn, rejects a price that no tool (or the customer) stated,
rejects delivery-time promises and wrong-script replies, and otherwise falls
back to server-written copy. A failing tool is reported to the model rather
than failing the turn. Gemini 3 requires its `thoughtSignature` back, so model
turns are echoed exactly as received.

Measured 2026-10-25 on the live model with the production catalogue snapshot:
about two model calls and 7-11 s per message, about Rs 1.8 per message
(the single-call planner: Rs 0.7, 5 s). Tests: `tests/test_assistant_agent.py`.

## Trust and write boundaries

The model can choose only a bounded intent and typed arguments. It cannot supply SQL, URLs, arbitrary routes, payment actions or executable instructions. The app rejects malformed IDs and command tokens on both server and client.

Basket, memory and reminder writes use `POST /api/v1/chat/confirm` with a random, user-scoped preview token. The client cannot submit the underlying write payload. The server revalidates the authenticated user, selected branch, current catalog price, variant and stock before applying a basket preview. A Redis Lua compare-and-set rejects stale previews if the basket changed. Confirmation receipts make retries idempotent.

Basket previews show goods subtotal only. Delivery, coupons and the final payable total remain under the normal basket/checkout pricing flow. The assistant cannot place, pay for, cancel, refund or modify an order.

Preference rows are keyed by authenticated `user_id`. Only fields the customer explicitly asks to save are updated; inferred facts are never persisted. Chat history and reminders have separate controls. `avoid_ingredients` is a preference and is not presented as verified allergy protection.

Reminder suggestions inspect only delivered orders owned by the authenticated user from the previous 180 days. At least two distinct purchase dates are required. The median interval is bounded to 3–90 days and shown as a proposal. Confirmed reminders create an in-app notification at 09:00 Sri Lanka time and attempt push delivery; a transaction advances the next due date together with the in-app notification so a worker retry does not duplicate it. Active reminders are unique per customer/product.

Pantry recipes treat customer text as the pantry source. Recipe text is model-generated, while missing-product cards and prices are database-grounded. Customers are told to check quantities and dietary suitability.

## Language, privacy and history

`Accept-Language` controls all deterministic tool copy and the model language instruction. Product matching checks English, Sinhala and Tamil catalog names. Redis stores at most ten exchanges for seven days; the customer can clear them.

For signed-in natural-language turns, Gemini receives only the current customer-controlled preference fields and minimal basket entries (`product_id`, `variant_id`, name and quantity), plus recent customer-authored conversation. It does not receive profile names, addresses, delivery verification codes, phone numbers or payment details. Previous product IDs can resolve pronouns, but price and availability are refreshed from the selected branch.

Order queries always include `Order.user_id`. Guests are asked to sign in. Historical tracking cards are marked as saved snapshots and provide a refresh action. Courier data is never used to invent rider GPS or a precise ETA.

## Reliability

- Chat and confirmation endpoints are rate limited to twelve requests per minute and use a per-account/session Redis lock.
- Model output is validated by Pydantic. The model call has bounded retry/time budgets and the mobile client has a 60-second timeout.
- JPEG, PNG and WebP uploads are signature checked and limited to 4 MB decoded.
- The Flutter confirmation button disables while a request is running and after success. Basket state refreshes only after the server acknowledges the change.
- Reminder rows are claimed with PostgreSQL `FOR UPDATE SKIP LOCKED`, allowing multiple API instances without duplicate in-app notifications.
- In-app reminder delivery is durable. FCM push remains best effort; a failed push does not roll back or duplicate the saved notification.

## Database rollout

Apply [migration 053](../migrations/053_assistant_preferences_reminders.sql) before starting this backend version. It creates:

- `assistant_profiles`
- `assistant_reminders`
- `assistant_receipts`

The application starts a reminder sweep in the FastAPI lifespan and checks due reminders every minute. Deploy the migrated backend before releasing the updated mobile client. Existing Gemini, Redis, PostgreSQL and Firebase configuration is reused; no new secret is required.

## Verification

From the backend root:

```text
python -m pytest tests/test_assistant_commerce.py tests/test_chat_assistant.py tests/test_app_startup.py tests/test_order_workflow.py tests/test_order_dispatch.py tests/test_courier_contract_client.py tests/test_delivery_verification_code.py tests/test_pricing_service_delivery_quote.py --no-cov
python -m scripts.evaluate_assistant --live --suite commerce --limit 6
```

The commerce tests execute the real Redis Lua confirmation script in an emulator and cover stale basket/price/stock/branch rejection, idempotency, ownership, selective forgetting, opt-in reminders, worker locking, unit-price parsing, same-category alternatives and missing-only pantry searches.

From the mobile root:

```text
flutter test test/chat_assistant_test.dart test/cart_delivery_test.dart
flutter analyze lib/features/chat lib/core/l10n/assistant_strings.dart test/chat_assistant_test.dart
```

The widget tests cover Sinhala panels, narrow screens with enlarged text, malformed command rejection, one-time confirmation, basket refresh after acknowledgement, account disposal, history, navigation allowlists and retries.

## Local verification record — 2026-09-21

- 69 focused backend assistant tests passed.
- 179 backend assistant, startup, order, courier, delivery-verification and pricing regression tests passed.
- 15 Flutter assistant/basket tests passed.
- Targeted Dart analysis completed with no errors; two style notices were corrected afterwards.
- The configured Gemini endpoint returned HTTP 503 during the synthetic six-intent commerce evaluation, so live model routing could not be certified in this run. No customer data was sent.
- No migration, production deployment, live customer database write, push delivery or courier/device end-to-end action was performed.

Gemini request fields follow the [official GenerateContent reference](https://ai.google.dev/api/generate-content).
