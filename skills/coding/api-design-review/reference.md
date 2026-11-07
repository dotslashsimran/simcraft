# API design review reference

This file holds the judgement behind the skill. SKILL.md says what to do, and this file says how to decide. Each section gives the rule, the reason for it, what the public guidelines say, and a rewrite you can copy into a review.

The public guidelines cited here are:

- **Google AIP**, the API Improvement Proposals at aip.dev, cited by number (e.g. AIP-158).
- **Microsoft**, the Microsoft REST API Guidelines and the Azure REST API Guidelines, both in the microsoft/api-guidelines repository on GitHub.
- **Stripe**, the public Stripe API reference and docs (errors, pagination, idempotent requests, versioning, webhooks).
- **Zalando**, the Zalando RESTful API and Event Guidelines at opensource.zalando.com/restful-api-guidelines, cited by rule title.
- **IETF RFCs and drafts**, cited by number.

When the guidelines disagree, say so in the review and recommend one choice for the API under review. Consistency inside one API counts for more than which convention it follows.

## 1. Scorecard rubric

Score each area from 0 to 3. Score only the areas that apply, and write "n/a" for the rest (an API with no webhooks has no webhook score).

| Area | 0 | 1 | 2 | 3 |
|---|---|---|---|---|
| Naming and consistency | Several cases and styles, verbs in paths | One main style with many exceptions | One style with a few exceptions | One style everywhere, and names are guessable |
| Resource modelling | RPC endpoints named after functions | Resources exist but actions and resources are mixed up | Clear resources, a few odd actions | Clear resources, sub-resources and custom actions follow one pattern |
| Errors | No documented errors, or text-only messages | One error schema, no machine code | Machine code and message, used everywhere | Problem details with code, message, field, request id and docs link, used everywhere |
| Evolution and versioning | No versioning story, breaking changes ship silently | Versioned, but no rules for what counts as breaking | Written compatibility rules, deprecation with notice | Rules enforced in CI with a diff, plus deprecation headers and sunset dates |
| Pagination, filtering, sorting | Lists return everything | Offset paging, inconsistent parameters | Cursor paging on most lists | Same cursor paging, filter and sort syntax on every list |
| Reliability (idempotency, retries, rate limits, timeouts) | Retrying a create can duplicate it, and limits are invisible | Some idempotency, 429 without Retry-After | Idempotency on creates and 429 with Retry-After | Plus documented retry policy, timeouts and limit headers |
| Long-running operations | Requests hang until work ends | Polling exists but each one differs | One operation resource pattern | Plus cancel and progress |
| Webhooks | Unsigned, no event ids | Signed, but no replay protection or retry docs | Signed with timestamp, retries documented | Plus event ids for dedupe, ordering guidance and a way to replay events |
| Auth ergonomics | Credentials in URLs, unclear scopes | Works, but errors do not say what is missing | Clear 401 and 403, scoped keys | Plus test keys, key prefixes and rotation without downtime |
| Data formats (IDs, time, money, enums) | Mixed formats | One format with exceptions | Consistent | Consistent and self-describing (prefixed IDs, currency next to amounts) |
| SDK ergonomics | Thin wrapper that leaks HTTP | Typed, but long positional arguments and no retries | Idiomatic, typed, with retries and timeouts by default | Plus pagination iterators, one error hierarchy, raw response access |
| CLI conventions | Unparseable output, exit 0 on failure | Some flags follow conventions | Exit codes, --json, stdout and stderr split correctly | Plus stdin support, no prompts when not on a terminal, --dry-run |

Give an overall verdict in one sentence, e.g. "Fine for a private beta; fix errors and pagination before any public launch, because both are breaking to fix later."

## 2. Severity

Rank findings by what they cost the API's users and how expensive they get to fix.

- **P0**: can lose data, move money twice, or let someone forge input. Examples are creating POSTs on payments without idempotency, and unsigned webhooks.
- **P1**: forces every client to write a workaround, and fixing it later is a breaking change. Examples are lists with no pagination, bare array responses, errors with no machine code, and mixed ID types.
- **P2**: inconsistency or friction that people can work around. Examples are mixed casing, missing 404 docs, and offset paging on small collections.
- **P3**: polish, such as missing summaries or examples.

Raise a finding one level when the API is public or has many clients, and lower it one level for an internal API with one client that ships together with it. A breaking fix is cheap before launch and very expensive after it, so before launch, P1 findings are the most important part of the review.

## 3. Naming and consistency

Users guess names. Every name they can guess correctly is a docs lookup they skip, so the review checks whether names follow one rule.

- **Case.** Pick one case for JSON fields and query parameters. Stripe and Zalando use snake_case, and Microsoft and Google's JSON use camelCase. Mixing them is the problem, not the choice. Path segments are usually lowercase kebab-case (Zalando: "Use kebab-case for path segments").
- **Plural collections.** `/orders/{order_id}`, not `/order/{id}` (Google AIP-122, Zalando "Pluralize resource names").
- **No CRUD verbs in paths.** The HTTP method is the verb, so `GET /orders` is right and `GET /getOrders` is wrong. Real actions that are not CRUD are fine as custom methods, e.g. `POST /orders/{id}/cancel`, or `POST /orders/{id}:cancel` in Google style (AIP-136).
- **Same concept, same name.** If one endpoint says `customer_id`, no other endpoint says `customerId`, `client_id` or `buyer`. Check booleans the same way, e.g. `is_active` in one place and `enabled` in another.
- **Standard field names.** Use the same names for timestamps (`created_at` and `updated_at`, or Google's `create_time` and `update_time`, AIP-148) and for display names everywhere.

Before and after:

```
GET  /getCustomers                 ->  GET  /customers
GET  /customer/{customerId}        ->  GET  /customers/{customer_id}
POST /orders/create                ->  POST /orders
POST /orders/{id}/doRefund         ->  POST /orders/{order_id}/refunds   (a refund is a resource with its own ID)
{"shippingAddress": .., "created_at": ..}  ->  {"shipping_address": .., "created_at": ..}
```

## 4. Resource modelling

Model the nouns users think about, and give each one a stable ID and a URL.

- If an action creates something with its own lifecycle, model it as a resource. A refund has an amount, a status and can fail, so `POST /orders/{id}/refunds` returning a Refund is better than `POST /orders/{id}/refund` returning the Order.
- Use sub-resources only for real ownership. `/customers/{id}/orders` is a fine filter view, but the canonical order URL is `/orders/{id}`, so an order has one address.
- Keep nesting to two levels. `/a/{a}/b/{b}/c/{c}/d` makes clients collect three IDs to reach one object.
- State changes that have rules (cancel, archive, publish) are custom actions, not a writable `status` field. A PATCH that sets `status: "cancelled"` cannot say why a cancel failed, and it lets clients set states they should not reach.
- Return the full resource from create and update calls, so clients do not need a second GET.

## 5. Errors

Errors are the part of the API that users read most when things go wrong. A good error answers four questions, which are what happened, whether a program can react to it, how a person fixes it, and how support finds it.

Every error response needs:

- **A stable machine code**, e.g. `card_declined` or `ORDER_ALREADY_SHIPPED`. Clients branch on the code, so the code never changes. Google puts it in `ErrorInfo.reason` (AIP-193), Stripe in `code`, Microsoft in `error.code`.
- **A human message** that says what went wrong and how to fix it. You can reword the message any time, because no program should parse it.
- **The field that caused it**, for validation errors (Stripe `param`, Microsoft `target`).
- **A request id**, in the body and in a response header, so a user can quote it to support.
- **A docs link**, if you have per-code docs (Stripe `doc_url`).

RFC 9457 (Problem Details for HTTP APIs, which replaced RFC 7807) defines a standard shape with the media type `application/problem+json`. Its members are `type` (a URI that identifies the problem type), `title`, `status`, `detail` and `instance`, and you may add your own members. Zalando requires problem JSON ("Support problem JSON") and says not to expose stack traces.

Before:

```json
HTTP/1.1 400 Bad Request
{"message": "Invalid order"}
```

After:

```json
HTTP/1.1 422 Unprocessable Content
Content-Type: application/problem+json
Request-Id: req_7Hq2LmX

{
  "type": "https://docs.example.com/errors/insufficient-stock",
  "title": "Not enough stock",
  "status": 422,
  "detail": "Only 2 copies of ISBN 9780262046305 are left, but the order asks for 5.",
  "code": "insufficient_stock",
  "errors": [{"field": "items[0].quantity", "code": "insufficient_stock"}],
  "request_id": "req_7Hq2LmX"
}
```

Also check the following:

- **One error schema for the whole API.** Two shapes means every client writes two error handlers.
- **Status codes are right and few.** Use 400 or 422 for bad input, 401 when there are no credentials or they are wrong, 403 when the caller is not allowed, 404, 409 for conflicts and idempotency key reuse, 412 for a failed If-Match, 429, and 500 or 503. Zalando: "Use only the most common HTTP status codes".
- **Never 200 with an error in the body.** Retries, monitoring and SDKs all key off the status.
- **Validation errors list every bad field at once**, so users do not fix them one request at a time.
- **No internals.** Stack traces, SQL and hostnames do not belong in error bodies.

## 6. Evolution, versioning and breaking changes

The most expensive design mistakes are the ones you cannot fix without breaking clients. Judge every change by one rule: **requests may get looser and responses may get richer, never the reverse.** A client that worked yesterday must work today.

### What counts as breaking

| Change | Class | Why |
|---|---|---|
| Remove or rename an endpoint, field, parameter or enum value that clients send | Breaking | Existing calls fail or are silently ignored |
| Remove or rename a response field | Breaking | Clients read it |
| Add a required request field or parameter | Breaking | Existing calls lack it |
| Make an optional request field required | Breaking | Same reason |
| Tighten validation (shorter maxLength, new pattern, smaller maximum) | Breaking | Values that were valid now fail |
| Change a field's type or format (integer to string, number to integer) | Breaking | Typed clients fail to parse it |
| Make a response field nullable or no longer always present | Breaking | Clients dereference it |
| Change a success status code (200 to 201) | Breaking | Clients that check the exact code fail |
| Change a bare array response to an object | Breaking | Every list client fails |
| Require auth on an endpoint that was public, or add a scope | Breaking | Callers get 401 or 403 |
| Change the default of a parameter, or default sort order | Breaking (behaviour) | No schema change, so diff tools miss it. Look for it by hand |
| Rename an operationId | Breaking for generated SDKs | Method names change |
| Add an enum value or event type to a response | Risky | Additive by Stripe's and Google's policies (AIP-180), but clients with exhaustive switches fail. Document that clients must tolerate unknown values |
| Add a new error status | Risky | Safe if it only documents existing behaviour |
| Add an optional request field, a response field, an endpoint | Safe | Clients that ignore unknown fields absorb it |
| Loosen validation, make a request field optional | Safe | |

The "safe" rows assume clients ignore unknown fields. Zalando writes this down as a rule ("Prepare clients to accept compatible API extensions"), and Stripe lists new response properties and new event types as backwards compatible in its upgrade docs. Say in your docs that clients must ignore unknown fields and handle unknown enum values, and make your SDKs do it, so that additive changes really are safe.

Run `scripts/breaking_diff.py` on every change. Behaviour changes with no schema change (defaults, ordering, rate limits, side effects) need a human to look.

### Versioning approaches

- **Major version in the path** (`/v1/`), Google's choice for major versions (AIP-185). It is simple and visible, but a new major version means all clients migrate at once.
- **Date-named versions pinned per account**, Stripe's approach. Each account stays on the version it started with, and a header (`Stripe-Version`) overrides it per request. The server translates old requests and responses through a chain of small version changes. It costs the most to build, and clients never break unless they opt in.
- **A required `api-version` query parameter with a date value**, the Azure approach.
- **Avoid versioning and only make compatible changes**, Zalando's default ("Prefer compatible extensions"), with media type versioning when a break cannot be avoided. Zalando says not to put versions in the URI.

Any of the four works. The review checks that the API has one, that it is written down, and that `info.version` follows semver for the spec itself.

### Deprecation

- Mark it in the spec (`deprecated: true`) and in the docs, with the replacement named.
- Send the `Deprecation` header (RFC 9745) and a `Sunset` header with the removal date (RFC 8594) on every response from the old endpoint.
- Tell users by email or changelog, then measure who still calls the endpoint before removing it.
- Keep a renamed field as a deprecated alias for one major version. Returning both `total` and `total_amount` costs a few bytes and saves every client a code change.

## 7. Pagination

Every list endpoint needs pagination from the first release, because adding it later changes what an unchanged client gets back.

- **Wrap lists in an object**: `{"data": [...], "has_more": true, "next_cursor": "..."}`. A bare JSON array cannot grow new fields.
- **Prefer opaque cursors.** Offset paging skips or repeats rows when data changes between pages, and deep offsets are slow. Zalando says to prefer cursor-based pagination. Google uses `page_size`, `page_token` and `next_page_token` (AIP-158). Stripe uses `limit`, `starting_after`, `ending_before` and `has_more`. Microsoft returns `value` and `nextLink`.
- **Document the default and the maximum page size**, and clamp a size that is too large instead of failing.
- **Cursors are opaque** strings, so you can change what is inside them. Encode the filters in the cursor, or reject a cursor used with different filters (Google AIP-158 requires an error for that case).
- **Keep the order stable.** Sort by a unique key, or by a key plus the ID, so pages do not overlap.
- **Total counts are optional and expensive.** Offer them only if users need them, and say if they are estimates.

```
Before:  GET /orders                 -> [ {...}, {...}, ... 40,000 items ]
After:   GET /orders?limit=50&starting_after=ord_8Fk2
         -> {"data": [...50 items], "has_more": true, "next_cursor": "ord_9Lm3"}
```

## 8. Filtering and sorting

- Use the same syntax on every list. Simple equality filters as query parameters (`?status=shipped&customer_id=cus_1`) cover most needs. Google uses one `filter` string with a small grammar (AIP-160), and Microsoft uses OData-style `$filter`, `$orderby` and `$top`.
- For sorting, use one parameter such as `order_by=created_at desc` (AIP-132) or `sort=-created_at`.
- Reject unknown filter fields with a 400. If you ignore them silently, a typo returns unfiltered data and the client acts on all of it.
- Range filters on time need a documented, inclusive or exclusive rule, e.g. `created[gte]=...` in Stripe style.

## 9. Partial updates and concurrency

- Offer PATCH. PUT replaces the whole resource, so a client built before a new field existed will wipe that field on every PUT.
- With JSON Merge Patch (RFC 7396), a field that is absent stays unchanged, a value replaces, and `null` clears the field. Arrays are replaced whole. If you need "clear" separate from "set to null", or edits inside arrays, use JSON Patch (RFC 6902) or an update mask (Google AIP-134).
- Every field in a PATCH schema is optional. A PATCH schema with `required` fields is a PUT.
- For resources that several people edit, return an `ETag` and accept `If-Match`. Return 412 when the version is stale, so the last writer does not silently overwrite the first (Google AIP-154, Microsoft guidelines).

## 10. Idempotency and retries

Networks fail after the server has done the work but before the client hears back. Then the client cannot tell whether to retry. GET, PUT and DELETE are idempotent by definition, which means repeating them gives the same result. POST is not idempotent, so a retried POST can create a second order or a second charge.

- Accept an `Idempotency-Key` header on every POST that creates something or moves money (Stripe, IETF draft "The Idempotency-Key HTTP Header Field"). Azure uses `Repeatability-Request-ID` and `Repeatability-First-Sent`, and Google uses a `request_id` field (AIP-155).
- Store the key with the first response. On a retry with the same key, return the stored response without doing the work again. Stripe keeps keys for at least 24 hours.
- If the same key arrives with a different body, return an error (409 or 422), because the client has a bug.
- If the first request is still running, return 409 so the client waits.
- Document which errors are safe to retry. Retry 429, 503 and network errors, and do not retry 400.
- SDKs should retry safe failures by default with exponential backoff and jitter, and generate idempotency keys for POSTs automatically.

```
Before:  POST /payments {"order_id": "ord_1", "amount": 19.99}
After:   POST /payments
         Idempotency-Key: 3f8c6b1e-9a2d-4a57-8c51-0b6f8d9e2a11
         {"order_id": "ord_1", "amount": 1999, "currency": "EUR"}
```

## 11. Rate limits and timeouts

- Return 429 when a client is over its limit, with `Retry-After` in seconds (RFC 9110). Zalando requires 429 with headers for rate limits.
- Tell clients where they stand before they hit the limit. The IETF draft "RateLimit header fields for HTTP" defines `RateLimit-Policy` and `RateLimit`, and many APIs use the older `X-RateLimit-Limit`, `X-RateLimit-Remaining` and `X-RateLimit-Reset`. Pick one set and document it.
- Document the limits themselves (per key, per endpoint, per second or minute) in the docs, not only in headers.
- Use 503 with `Retry-After` for overload or maintenance, so clients back off instead of hammering.
- Document the server-side timeout for each endpoint. A request that takes longer belongs in a long-running operation (section 12).
- Client libraries must ship a finite default timeout. The Python `requests` library has no default timeout, and `urllib.request.urlopen` uses the global socket default, which is no timeout unless someone sets one. A call with no timeout can hang a worker forever.

## 12. Long-running operations

If work can take longer than a normal request timeout (exports, video processing, provisioning), do not hold the connection open.

- Return `202 Accepted` with an operation resource, and a `Location` or `Operation-Location` header pointing at it (the Azure pattern).
- The operation has an ID, a status (e.g. `running`, `succeeded`, `failed`, `cancelled`), the result or an error in the standard error shape when done, and optionally progress. Google returns an `Operation` with `name`, `done`, `metadata` and `response` or `error` (AIP-151).
- Let clients cancel, with `POST /operations/{id}/cancel`.
- Offer a webhook for completion as well as polling, and send `Retry-After` on polls to set the polling rate.
- SDKs should offer a `wait()` or poller helper so users do not write polling loops.

## 13. Webhooks

Webhooks are an API you call on someone else's server, so the same rules apply in reverse, and receivers must be able to trust and process events safely.

- **Signing.** Sign each delivery with HMAC-SHA256 using a per-endpoint secret. Sign the event id, a timestamp and the raw body together, and send them as headers. The Standard Webhooks spec (standardwebhooks.com) uses `webhook-id`, `webhook-timestamp` and `webhook-signature`. Stripe sends `Stripe-Signature: t=<timestamp>,v1=<hmac>` and its libraries reject timestamps older than five minutes by default, which blocks replays.
- **Delivery is at least once.** Every event has a unique `id`, and receivers drop duplicates by id.
- **Order is not guaranteed.** Include `created_at`, and consider sending a small event with the object ID so the receiver fetches the current state. Fetching the current state avoids acting on stale data when events arrive out of order.
- **Retries.** Retry failed deliveries with backoff for a documented period (Stripe retries for up to three days), and tell receivers to return 2xx quickly and do work asynchronously.
- **Event types** use one naming pattern, e.g. `order.shipped`. Tell receivers to ignore types they do not know, so adding a type is safe.
- **Tooling.** Let users list past events, resend one, and send a test event. Support two active secrets during rotation.

## 14. Auth ergonomics

- Send credentials in the `Authorization` header, never in the URL, because URLs end up in logs and browser history.
- Prefix keys so people and secret scanners can recognise them and tell test from live (Stripe uses `sk_test_` and `sk_live_`).
- Separate test and live modes, with test keys that cannot touch real data.
- Use scoped keys and least privilege, and name the missing scope in the 403 error.
- 401 means "who are you?" and 403 means "you can't do this". Many APIs mix them up.
- Allow two active keys at once so users can rotate without downtime.
- For OAuth, document token lifetimes and refresh, and make SDKs refresh tokens automatically.

## 15. Data formats

- **IDs** are opaque strings everywhere, including IDs that are integers in your database. Integers above 2^53 lose precision in JavaScript, sequential IDs reveal volume and invite enumeration, and a string lets you change the scheme later. Prefixed IDs (`ord_`, `cus_`) tell a person which kind of object an ID belongs to. Stripe does this.
- **Time.** Use RFC 3339 strings in UTC with `format: date-time`, e.g. `2026-11-08T14:03:00Z` (Google AIP-142, Zalando, Microsoft). Stripe uses Unix seconds instead, and either is fine if used everywhere. Use `date` (`2026-11-08`) for calendar dates with no time, such as birthdays. Durations are seconds as a number with the unit in the name (`timeout_seconds`), or ISO 8601 / protobuf duration strings.
- **Money.** Never use floats. Stripe sends an integer in the currency's smallest unit (`1999` means 19.99 for EUR, and zero-decimal currencies such as JPY have no minor unit). Zalando's money object has an `amount` that clients must parse as a decimal, plus `currency`. Google's Money type has `units`, `nanos` and `currency_code`. Always put the ISO 4217 currency next to the amount.
- **Enums** are strings in one case (Stripe lower snake_case, Google and Zalando UPPER_SNAKE_CASE). Document that clients must handle unknown values, and give SDK enums an "unknown" member. Zalando recommends open-ended (extensible) enums for this reason.
- **Booleans** are named so `true` is the readable case (`is_active`, `send_email`). Use an enum when a third state is likely.
- **Nullable versus absent.** Decide whether "no value" is `null` or a missing field, and do the same everywhere.

## 16. SDK ergonomics

A good SDK makes the right call the easiest one to write. Review the public surface with `scripts/sdk_surface.py` and check the following.

- **Defaults.** Finite timeout, automatic retries with backoff on 429, 503 and network errors, idempotency keys generated for retried POSTs, and the API version pinned in the SDK.
- **Required arguments are positional and few, and everything else is keyword-only or an options object.** `create(customer_id, items, None, True)` is unreadable. `create(customer_id=..., items=..., send_email=True)` is readable.
- **No boolean flag arguments.** `list(True)` tells the reader nothing. Use a keyword argument or two methods.
- **Types.** Python packages ship type hints and `py.typed`, and TypeScript packages avoid `any`. Responses are typed models, and enums have an unknown fallback.
- **One error hierarchy.** `except BookshopError` catches everything. Subclasses map to codes (`RateLimitError`, `NotFoundError`), and each error carries the status, the machine code and the request id.
- **Pagination.** An iterator that fetches pages as needed, e.g. `for order in client.orders.list():`. Stripe's Python library offers `auto_paging_iter()`.
- **Async.** Offer async alongside sync with the same names (`AsyncClient`), and use Promises in TypeScript, not callbacks.
- **Discoverability.** `client.orders.create()` mirrors the REST resources, so the API docs and the SDK use the same words. IDEs can autocomplete the resource tree.
- **Escape hatches.** Users need raw response access (headers, status), a generic `request(method, path)` for endpoints the SDK lacks, and a way to pass extra headers and a custom HTTP client or proxy.
- **Configuration** reads keys from an environment variable by default, so examples do not put secrets in code.

The Azure SDK design guidelines (azure.github.io/azure-sdk) list the goals as idiomatic, consistent, approachable, diagnosable and dependable, and give per-language rules. They are a good source to cite for SDK findings.

Before and after (TypeScript):

```ts
// Before
client.orders.create(42, items, undefined, true, "SPRING");
client.orders.cancel("ord_1", (err, order) => { ... });

// After
await client.orders.create({ customerId: "cus_42", items, sendEmail: true, coupon: "SPRING" });
const order = await client.orders.cancel("ord_1");
for await (const o of client.orders.list({ status: "shipped" })) { ... }
```

## 17. CLI conventions

The Command Line Interface Guidelines at clig.dev are the main public reference. Check the following:

- **Output.** Data goes to stdout, and messages, progress and errors go to stderr, so `tool list | jq` works. Offer `--json` (or `--output json`) with a stable schema for scripts, and keep human output free to change.
- **Exit codes.** 0 means success, and anything else means failure. Use 1 for general errors and 2 for wrong usage, and document any others (`sysexits.h` defines 64 to 78 if you want more). Never exit 0 after an error.
- **Flags.** Long flags for everything (`--output`), short flags only for the common ones (`-o`). `--help` and `--version` always work. Prefer flags over positional arguments when there is more than one.
- **Input.** Accept `-` to mean stdin, e.g. `tool apply -f -`.
- **Interactivity.** Prompt only when stdin is a terminal. Every prompt has a flag equivalent (`--yes`, `--force`), so scripts and CI never hang.
- **Destructive commands** ask for confirmation or need `--force`, and offer `--dry-run`.
- **Config precedence** is flags, then environment variables, then project config, then user config.
- **Color.** Respect `NO_COLOR`, and disable color when output is not a terminal.
- **Errors** say what went wrong and what to run next, e.g. "Not logged in. Run `tool login`."
- **Naming.** Use one pattern (`tool <noun> <verb>`, e.g. `tool orders list`) and the same nouns as the API.

## 18. Review checklist for code instead of a spec

When there is no OpenAPI file, read the route definitions and handlers and build a list of endpoints, methods, request fields, response fields and error responses. Then apply the same sections. Things to grep for:

- Route tables (`@app.get`, `router.post`, `@RequestMapping`, `HandleFunc`) for path naming.
- Error handlers and `raise`/`throw` sites for the error shape and status codes.
- Serializers and models for field names, ID types, time and money formats.
- `limit`, `offset`, `cursor` in list handlers.
- Any retry, idempotency or rate-limit middleware.

If the framework can export an OpenAPI file (FastAPI, NestJS, Spring with springdoc, and others), export it and run the scripts on it.
