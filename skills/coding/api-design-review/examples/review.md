# API design review: Bookshop Orders API v1.4.0

This review covers `bookshop-v1.json`, the proposed `bookshop-v2.yaml`, and the TypeScript SDK surface in `sample-sdk.d.ts`. The Bookshop API is fictional and was written with planted mistakes to test this skill. The raw tool output is in `out/`, and this review was written from that output plus a manual read of the spec.

The API is assumed to be public, with paying customers on v1.

## Summary

The API is not ready for more public users, because two flaws can cost customers money and several others are breaking to fix. A retried `POST /payments` or `POST /orders/{order_id}/refund` can charge or refund twice, because neither accepts an idempotency key, and webhooks are unsigned, so anyone can fake an `order.shipped` event. The proposed v2 fixes some of these, but it also ships 24 breaking changes under a minor version number (1.4.0 to 1.5.0), so every existing integration would break without warning.

The top fixes are listed below:

1. Add `Idempotency-Key` to every POST that creates something or moves money, and sign webhooks.
2. Ship v2's breaking changes as a new major version with a migration guide, or make them additive (keep old fields as deprecated aliases).
3. Settle one error schema, one ID type (string), one money format (integer minor units plus currency) and one list envelope with cursor pagination, before more clients depend on the current shapes.

## Scorecard

| Area | Score | Reason |
|---|---|---|
| Naming and consistency | 1 | snake_case fields with seven camelCase exceptions, a verb path (`/getCustomers`), a singular collection (`/customer/{customerId}`) and camelCase path segments |
| Resource modelling | 2 | Orders, items and payments are clear resources, but refund is an action that returns the Order instead of a Refund resource |
| Errors | 0 | Two error shapes, neither has a machine code or request id, and six operations document no errors at all |
| Evolution and versioning | 1 | URL version `/v1` exists, but v2 shows no compatibility rules, and the version number does not signal breaking changes |
| Pagination, filtering, sorting | 0 | Two of three list endpoints return everything as a bare array, and the third uses page numbers |
| Reliability | 0 | No idempotency anywhere, and the one 429 has no Retry-After |
| Long-running operations | n/a | None in this API |
| Webhooks | 0 | Unsigned, no event id, no event type field, no creation time |
| Auth ergonomics | 2 | Bearer tokens in the Authorization header, with `/health` correctly public, but 401 and 403 are documented on only one operation |
| Data formats | 0 | IDs are integers and strings, timestamps come in three formats, and money is a float in four places |
| SDK ergonomics | 1 | Promise-based with a raw `request` escape hatch, but long positional arguments, boolean flags, a callback method, `any` types and two unrelated error roots |
| CLI conventions | n/a | No CLI |

## Findings

### P0: Payments and refunds can run twice

**Where:** `POST /payments`, `POST /orders/{order_id}/refund` (also `POST /orders`, as P1).

If the response to `POST /payments` is lost (timeout, dropped connection, deploy), the client cannot tell whether the payment happened. A retry may charge the customer a second time, and not retrying may lose the order. Stripe solves this with an `Idempotency-Key` header, which the IETF is standardising in the draft "The Idempotency-Key HTTP Header Field". Google uses a `request_id` field (AIP-155) and Azure uses `Repeatability-Request-ID`.

Before:
```
POST /payments
{"order_id": "ord_1", "amount": 19.99}
```
After:
```
POST /payments
Idempotency-Key: 3f8c6b1e-9a2d-4a57-8c51-0b6f8d9e2a11
{"order_id": "ord_1", "amount": 1999, "currency": "EUR"}
```
The server stores the key with the first response for at least 24 hours and returns that stored response on a retry. If the same key arrives with a different body, the server returns 422. The SDK generates a key per call and reuses it across its own retries. Adding an optional header is not breaking.

### P0: Webhooks are unsigned

**Where:** `webhooks.order.shipped`.

Any server on the internet can POST a fake `order.shipped` event to a receiver's endpoint, and a receiver that ships goods or sends emails on that event will act on it. The payload also has no event `id` to drop duplicates, no `type`, and no creation time to order events by.

After:
```
POST <receiver URL>
webhook-id: evt_2Lk9
webhook-timestamp: 1762611780
webhook-signature: v1,<base64 HMAC-SHA256 of "evt_2Lk9.1762611780.<raw body>">

{"id": "evt_2Lk9", "type": "order.shipped", "created_at": "2026-11-08T14:03:00Z",
 "data": {"order_id": "ord_1"}}
```
This follows the Standard Webhooks spec. Stripe's `Stripe-Signature` header works the same way. Tell receivers to reject timestamps older than five minutes, to drop duplicate ids, and to fetch the order rather than trust a possibly stale copy in the event, because delivery order is not guaranteed. Changing the payload is breaking for current receivers, so send the new shape as a new event version and run both during a migration window.

### P1: List endpoints return everything, as a bare array

**Where:** `GET /orders`, `GET /getCustomers`.

Both endpoints return every record in one JSON array. Response time and memory grow with the data until the calls time out, and a top-level array cannot gain a `has_more` or cursor field later without breaking every client. The guidelines agree on this point. Stripe, Google (AIP-158), Microsoft and Zalando all wrap lists in an object, and Zalando prefers cursors over offsets.

Before:
```
GET /orders  ->  [ {...}, {...}, ... ]
```
After:
```
GET /orders?limit=50&starting_after=ord_8Fk2
->  {"data": [...], "has_more": true, "next_cursor": "ord_9Lm3"}
```
Document the default page size (e.g. 20) and the maximum (100). `GET /orders/{order_id}/items` already has the right envelope, but it uses `page`, which skips or repeats items when the list changes between pages. An order's items rarely change, so page numbers are acceptable there (P3).

### P1: Errors have no machine-readable code and two shapes

**Where:** schema `Error` (`{message}`, 5 responses) and schema `ErrorResponse` (`{error: {msg, status}}`, 2 responses), plus six operations with no documented error at all (`DELETE /orders/{order_id}`, `GET /getCustomers`, `GET /health`, `GET /shippingLabels/{label_id}`, `PATCH /orders/{order_id}`, `POST /orders/{order_id}/refund`).

Clients that need to react to an error (e.g. a declined card on `POST /payments`, versus out of stock on `POST /orders`) can only match on message text. After that, nobody can reword a message without breaking someone. Two shapes also mean two error handlers in every client.

After (RFC 9457 problem details, used for every 4xx and 5xx):
```
HTTP/1.1 402 Payment Required
Content-Type: application/problem+json
Request-Id: req_7Hq2LmX

{"type": "https://docs.bookshop.example/errors/card-declined",
 "title": "Card declined", "status": 402,
 "detail": "The card was declined by the issuer. Ask the customer for another card.",
 "code": "card_declined", "request_id": "req_7Hq2LmX"}
```
Migration: add `code`, `type` and `request_id` to both existing shapes now, which is additive. Keep `message` populated in `Error` (and `error.msg` in `ErrorResponse`) as deprecated fields until the next major version. The proposed v2 drops `message` outright, which the diff flags as breaking.

### P1: IDs are integers in some places and strings in others

**Where:** `Customer.id`, `Order.customer_id`, `NewOrder.customer_id` and the `{customerId}` path parameter are integers. `Order.id`, `Payment.id` and `Payment.order_id` are strings.

Clients have to remember which kind each ID is. Customer IDs above 2^53 will be corrupted by JavaScript clients, and sequential customer IDs reveal how many customers the shop has. Use opaque strings everywhere, ideally prefixed (`cus_`, `ord_`) as Stripe does, so a support engineer can tell an order ID from a customer ID.

Changing a type is breaking. Do it in the next major version, or add `customer_ref` as a string now and deprecate the integer field.

### P1: Money is a floating point number

**Where:** `Order.total` (double), `OrderItem.unitPrice`, `Payment.amount`, and the refund `amount` (float). `OrderItem.unitPrice`, `Payment.amount` and the refund `amount` have no currency.

Floats cannot represent 0.10 exactly, so clients in different languages round differently, and totals stop matching line items. An amount with no currency is ambiguous once a second currency exists.

After:
```json
{"total_amount": 1999, "currency": "EUR"}
```
This uses integer minor units like Stripe. Zalando's money object and Google's Money type are decimal alternatives. Add the new fields alongside the old ones, and deprecate the float fields.

### P1: Duplicate operationId breaks generated SDKs

**Where:** `GET /orders/{order_id}` and `DELETE /orders/{order_id}` are both `getOrder`.

Code generators either fail or silently produce one method that hides the other. Rename the DELETE to `deleteOrder`. Renaming an operationId changes generated method names, so it counts as breaking for SDK users (the diff reports it). The SDK should ship it in a major release, or keep `getOrder` as a deprecated alias in the handwritten SDK.

### P2: Naming is inconsistent

**Where:**
- Paths. `/getCustomers` has a verb, `/customer/{customerId}` is singular, and `/shippingLabels` and `/getCustomers` are camelCase while every other path is lowercase.
- Fields. Seven names are camelCase in a snake_case API, which are `Order.shippingAddress`, `Order.updatedDate`, `OrderItem.unitPrice`, the PATCH body's `shippingAddress`, and the `customerId` path and query parameters.
- Enum. `OrderStatus` mixes `pending`, `Shipped` and `CANCELLED`.

After:
```
GET /getCustomers          ->  GET /customers
GET /customer/{customerId} ->  GET /customers/{customer_id}
GET /shippingLabels/{id}   ->  GET /shipping-labels/{label_id}
shippingAddress            ->  shipping_address
OrderStatus: pending | shipped | cancelled
```
Add the new paths and names, keep the old ones as deprecated aliases with `Deprecation` and `Sunset` headers (RFC 9745, RFC 8594), and remove them in the next major version.

### P2: Timestamps use three formats

**Where:** `created_at` is an RFC 3339 string, `Order.updatedDate` is a string with no format, and `Order.shipped` is Unix seconds.

Use RFC 3339 UTC strings everywhere with `format: date-time`, and one suffix (`_at`). After the change, the fields are `updated_at` and `shipped_at`.

### P2: PATCH requires fields, and customers only have PUT

**Where:** `PATCH /orders/{order_id}` requires `shippingAddress` and `notes`. `PUT /customer/{customerId}` has no PATCH alternative.

A client that only wants to change the notes must resend the address. With PUT, a client built before a new customer field existed will wipe that field on every update. Make every PATCH field optional (JSON Merge Patch, RFC 7396) and add `PATCH /customers/{customer_id}`. The v2 draft already makes the PATCH fields optional, which is a safe change.

### P2: Status codes and error docs are incomplete

- `POST /orders` and `POST /payments` return 200 for a create. Return 201 with a `Location` header in the next major version, because changing it now is breaking (the diff flags v2 for exactly this).
- The 429 on `POST /orders` has no `Retry-After`, so clients guess how long to wait. Add `Retry-After`, and document 429 on every operation.
- Eleven operations need auth but do not document 401. Four operations that address one resource do not document 404.

### P2: SDK ergonomics (`sample-sdk.d.ts`)

`sdk_surface.py` reported the following:

- `new BookshopClient(apiKey, baseUrl?, timeout?, retries?, debug?)` takes five positional arguments, while a `ClientOptions` interface already exists and is unused. Use `new BookshopClient({ apiKey, timeout })`, and read the key from an environment variable by default.
- `orders.list(status?, expand: boolean)` puts a required parameter after an optional one and uses a boolean flag, so callers must write `list(undefined, true)`.
- `orders.create(customerId: number, items, notes?, sendEmail?, coupon?)` takes five positional arguments with a boolean flag. Use `create({ customerId, items, sendEmail })`.
- `orders.cancel(id, callback)` is the only callback method in a Promise-based SDK.
- `customers: any`, `updateOrder(id, data: any): Promise<any>` and `WebhookHandler = (event: any)` turn off type checking where users need it most.
- `ApiError extends Error` and `RateLimitError` have no common base, so `catch (e) { if (e instanceof ApiError) }` misses rate limits. Make `RateLimitError extends ApiError`, and add `code` and `requestId` to `ApiError`.
- `ClientOptions.timeout` has no default. Ship a finite default (e.g. 60 seconds) and automatic retries on 429 and 503.
- `orders.list()` returns `Promise<Order[]>`, which will hide pagination. Return an async iterator that fetches pages as needed.
- Only 3 of 10 exported declarations have doc comments.

## Breaking-change risk list (v1.4.0 to proposed v2)

`breaking_diff.py` found 24 breaking, 2 risky and 34 safe changes (full output in `out/diff-v1-v2.md`). The version moves from 1.4.0 to 1.5.0, which tells users nothing broke. Either ship v2 as `/v2` (2.0.0) with a migration guide and a sunset date for v1, or rework it so every change is additive.

| Change in v2 | Who breaks | Additive alternative |
|---|---|---|
| `GET /getCustomers` removed | Every client listing customers | Keep it, mark it deprecated with Deprecation and Sunset headers, and point to `GET /customers` |
| `GET /orders` returns an object instead of an array | Every client listing orders | Serve the envelope only on `/v2`, or behind a version header |
| `Customer.id`, `customer_id` and `{customerId}` change from integer to string | Typed clients, and anyone who stored IDs as integers | Add string `customer_ref`, deprecate the integer |
| `Order.total` removed (renamed `total_amount`, integer) | Anyone showing totals | Return both fields during the deprecation period |
| `Error.message` removed, `ErrorResponse` replaced on 400 and 402 | Every error handler | Keep `message` (and `error.msg`) alongside the new fields |
| `NewOrder.currency` newly required | Every `POST /orders` call | Make it optional, with a default of the account currency |
| `POST /orders` and `POST /payments` now return 201 | Clients that check `== 200` | Only in a major version |
| `Order.shippingAddress` becomes nullable | Clients that read it without a null check | Return an empty string, or only change it in v2 |
| `PATCH` `shippingAddress` maxLength drops from 500 to 200 | Clients with longer addresses get 400 | Keep 500 |
| Refund `amount` changes from number to integer, `reason` becomes an enum | Every refund call | Add `amount_minor` and accept free-text `reason` with a warning |
| `GET /health` now requires auth | Load balancers and uptime checks | Keep it public |
| `operationId` of DELETE renamed | Generated SDK users | Release with the SDK major version |
| Webhook payload drops `event` | Every receiver | Send a new event version and keep both during migration |
| `OrderStatus` gains `refunded` (risky) | Clients with exhaustive switches | Announce it, and document that clients must handle unknown values |
| `customerId` query filter removed from `GET /orders` (risky) | Clients filtering by customer now get all orders, silently | Keep the filter, or reject it with a 400 so the change is visible |

The second risky change is the most dangerous one in the list, because the request still succeeds. A client that filtered orders by customer now gets every customer's orders with no error.

The diff also confirms that v2 makes several safe improvements, which are cursor pagination on `/customers` and `/orders`, an optional `Idempotency-Key` on `POST /orders`, `Retry-After` on 429, signed webhooks with `id`, `type` and `created_at`, problem details fields on errors, optional PATCH fields, and a deprecation flag on `DELETE /orders/{order_id}`.

## What's good

- Auth uses a bearer token in the `Authorization` header, and `/health` is explicitly public with `security: []`.
- `Order.id`, `Payment.id` and the order path parameters are already opaque strings.
- `created_at` is an RFC 3339 `date-time` on both Customer and Order, which gives a clear standard to align the rest to.
- `GET /orders/{order_id}/items` already uses a `{data, has_more}` envelope with a documented maximum page size of 100, so it is the model for the other lists.
- `Order` carries `currency` next to `total`.
- `quantity` has `minimum: 1`, and `notes` has a `maxLength`, so validation rules are in the spec where SDK generators can use them.
- The SDK is Promise-based, exposes `readonly orders` as a resource namespace that mirrors the REST paths, and has a typed raw `request<T>()` escape hatch.
- The v2 draft fixes many of the right things. Its problem is how it ships them.

## Not checked

- Runtime behaviour, which includes real latency, actual rate limits, whether the server really ignores unknown fields, and default sort order of lists.
- Documentation prose and examples, which `dx-audit` covers as part of onboarding.
- Security beyond the API surface (authorization rules, tenant isolation).
- The `./pagination` module re-exported by the SDK, which was not provided.
