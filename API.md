# API reference

Base URL: `http://127.0.0.1:8000`. Send JSON request bodies with
`Content-Type: application/json`. No authentication is implemented, as permitted
by the assignment. All `/admin/*` operations are administrative.

Full typed request and response schemas are available at `/openapi.json` and
interactive examples at `/docs`. Every money field ending in `_cents` is an
integer in USD. Timestamps are UTC ISO 8601 strings. IDs are opaque strings.
Body fields not listed in the request schemas are rejected.

## Endpoints

| Method and path | Request | Success response | Important errors |
| --- | --- | --- | --- |
| `GET /health` | No body | 200 `{"status":"ok"}` after checking DB connectivity | 503 database busy; 500 storage failure |
| `GET /products` | No body | 200 array of Product | 500 storage failure |
| `POST /carts` | No body | 201 Cart, open and empty | 503 busy |
| `GET /carts/{cart_id}` | No body | 200 Cart | 404 cart missing |
| `POST /carts/{cart_id}/items` | `{"product_id":"p1","quantity":2}` | 200 Cart; adds to existing quantity | 404 cart/product missing; 409 checked out or insufficient inventory; 422 invalid quantity |
| `PUT /carts/{cart_id}/items/{product_id}` | `{"quantity":3}` | 200 Cart; replaces quantity | 404 cart/product/item missing; 409 checked out or insufficient inventory; 422 invalid quantity |
| `DELETE /carts/{cart_id}/items/{product_id}` | No body | 200 Cart after removing line | 404 cart/item missing; 409 checked out |
| `POST /carts/{cart_id}/checkout` | `{}` or `{"coupon_code":"REWARD-<32 uppercase hexadecimal characters>"}`; required `Idempotency-Key` header | 201 Order, or 200 saved Order on replay | 404 cart missing; 409 empty/checked-out cart, insufficient inventory, redeemed coupon, key conflict; 422 missing/invalid header, body, or coupon; 503 busy |
| `GET /orders/{order_id}` | No body | 200 Order | 404 order missing |
| `POST /admin/coupons` | No body | 201 Coupon for oldest eligible milestone | 409 no eligible milestone; 503 busy |
| `GET /admin/coupons` | No body | 200 array of Coupon, milestone ascending | 500 storage failure |
| `GET /admin/orders` | No body | 200 array of Order, creation time then ID ascending | 500 storage failure |
| `GET /admin/report` | No body | 200 Report from one read snapshot | 500 storage failure |

All database-backed endpoints may return a retryable 503 on a database lock
timeout or a generic 500 for an unexpected internal failure. Lists are unpaginated
for this assignment's bounded evaluation data. Zero-quantity removal is not
supported; use DELETE. Quantities must be JSON integers from 1 to 1,000,000;
strings, decimals, booleans, zero, and negative values are rejected.

## Response shapes

**Product**

```json
{"id":"p1","name":"Notebook","unit_price_cents":1299,"inventory":100}
```

**Cart**

```json
{
  "id": "<cart-id>",
  "status": "open",
  "created_at": "2026-10-20T00:00:00.000Z",
  "currency": "USD",
  "items": [{
    "product_id": "p1", "product_name": "Notebook",
    "unit_price_cents": 1299, "quantity": 2,
    "line_total_cents": 2598, "inventory": 100
  }],
  "subtotal_cents": 2598,
  "order_id": null
}
```

Cart prices and inventory are current catalog values, including after checkout.
`status` becomes `checked_out` and `order_id` points to the saved receipt.
Stock is checked on cart edits but is not reserved until checkout.

**Order**

```json
{
  "id": "<order-id>",
  "cart_id": "<cart-id>",
  "currency": "USD",
  "gross_cents": 2598,
  "discount_cents": 260,
  "net_cents": 2338,
  "coupon_code": "REWARD-0123456789ABCDEF0123456789ABCDEF",
  "discount_percent": 10,
  "created_at": "2026-10-20T00:00:00.000Z",
  "items": [{
    "product_id": "p1", "product_name": "Notebook",
    "unit_price_cents": 1299, "quantity": 2, "line_total_cents": 2598
  }]
}
```

Without a coupon, `coupon_code` is null, `discount_percent` and `discount_cents`
are zero, and `net_cents` equals `gross_cents`. Order line data is a purchase-time
snapshot and never recomputed from products. A checkout has no intermediate
payment status: committing it is the simulated payment success.

**Coupon**

```json
{
  "code": "REWARD-0123456789ABCDEF0123456789ABCDEF",
  "milestone": 5,
  "discount_percent": 10,
  "redeemed_order_id": null,
  "created_at": "2026-10-20T00:00:00.000Z"
}
```

`redeemed_order_id` is null while available, then references its one successful
order. The milestone is a global successful order count, not a customer-specific
count. Coupons do not expire. Coupon codes are case sensitive.

**Report** (empty initial state)

```json
{
  "currency": "USD",
  "successful_orders": 0,
  "gross_cents": 0,
  "discount_cents": 0,
  "net_cents": 0,
  "coupons": {"generated": 0, "available": 0, "redeemed": 0},
  "products": [
    {"product_id":"p1","quantity":0},
    {"product_id":"p2","quantity":0},
    {"product_id":"p3","quantity":0},
    {"product_id":"p4","quantity":0},
    {"product_id":"p5","quantity":0}
  ]
}
```

Report totals derive from successful orders and their saved items. Coupon counts
derive from generated coupons and redemption links. With ongoing writes, two
separate requests can observe different committed states; each individual report
is internally consistent. GET requests never generate coupons or change state.

## Idempotency and retries

Supply exactly one `Idempotency-Key` header. Its value is 1–128 ASCII letters,
digits, `.`, `_`, `:`, or `-`. Duplicate headers are rejected with 422, including
duplicates that contain the same value, so intermediaries cannot disagree on the key.
Use a new unpredictable key for each intended checkout and reuse it if the
response is lost. It is globally scoped to this service database. It is not an
authentication credential and is not included in logs or order responses.

- First success: **201**, `Idempotency-Replayed: false`.
- Same key, cart, and normalized coupon (`{}` and `{"coupon_code":null}` are
  equivalent): **200**, `Idempotency-Replayed: true`, exactly the stored order.
- Same key with a different cart or coupon: **409 `idempotency_conflict`**.
- Different key on a completed cart: **409 `cart_checked_out`**.
- Failure before commit: no order, stock change, redemption, or key binding.
  The request may be corrected and retried with that key. If the cart changes
  before any successful checkout, the next attempt uses its current contents.
- A replay never revalidates prices, stock, or the now-redeemed coupon.
- Successful keys are retained as long as orders; there is no retry expiry.

Both successful checkout responses include `Location: /orders/{order_id}`.
Every handled response includes `X-Request-ID` for log correlation.
503 responses include `Retry-After: 1`; retry with backoff using the same key.
After a network failure or 500, retry with the same key to discover whether a
commit already succeeded. Never infer payment failure merely from a lost response.

Cart creation and additive POST item operations do not accept idempotency keys.
For retried quantity edits use PUT with the desired absolute quantity. Coupon
generation is milestone-unique but not request-idempotent: with a backlog,
repeated POSTs intentionally generate successive eligible milestones. Inspect
`GET /admin/coupons` after an ambiguous generation response.

## Errors

```json
{"error":{"code":"insufficient_inventory","message":"Cart quantity is unavailable"}}
```

| Status | Code | Meaning |
| --- | --- | --- |
| 404 | `cart_not_found`, `product_not_found`, `item_not_found`, `order_not_found` | Referenced resource is absent |
| 404 | `not_found` | Unknown API route |
| 409 | `cart_checked_out` | Cart is already completed |
| 409 | `empty_cart` | Cart has no items |
| 409 | `insufficient_inventory` | Requested quantity is no longer available |
| 409 | `coupon_redeemed` | Another successful order used the coupon |
| 409 | `idempotency_conflict` | Successful key belongs to another request |
| 409 | `no_eligible_milestone` | No unrewarded milestone has been reached |
| 422 | `validation_error` | Invalid JSON, shape, quantity, code format, or required header |
| 422 | `invalid_quantity` | Combined additive quantity exceeds the supported bound |
| 422 | `invalid_coupon` | Well-formed coupon code does not exist |
| 503 | `database_busy` | Writer lock could not be acquired within five seconds |
| 500 | `internal_error` | Unexpected failure; internal details remain in server logs |
| 405 or other HTTP error | `http_error` | Framework HTTP error, such as unsupported method |

Malformed body encodings that cannot be decoded (for example invalid UTF-8)
return 400 `http_error`. Decodable malformed JSON or schema validation failures
return 422 `validation_error`.

If several rules fail at once, the service returns the first detected error.
Clients should branch on the status and code, not message wording.

## Executable PowerShell example

Start the server with the default settings in another terminal. This creates five
orders, generates the milestone-5 coupon, redeems it, and replays that checkout:

```powershell
$base = 'http://127.0.0.1:8000'
function New-FilledCart {
    $cart = Invoke-RestMethod -Method Post -Uri "$base/carts"
    $body = @{ product_id = 'p1'; quantity = 1 } | ConvertTo-Json
    Invoke-RestMethod -Method Post -Uri "$base/carts/$($cart.id)/items" -ContentType 'application/json' -Body $body | Out-Null
    return $cart.id
}
1..5 | ForEach-Object {
    $cartId = New-FilledCart
    $headers = @{ 'Idempotency-Key' = [guid]::NewGuid().ToString() }
    Invoke-RestMethod -Method Post -Uri "$base/carts/$cartId/checkout" -Headers $headers -ContentType 'application/json' -Body '{}' | Out-Null
}
$coupon = Invoke-RestMethod -Method Post -Uri "$base/admin/coupons"
$cartId = New-FilledCart
$headers = @{ 'Idempotency-Key' = [guid]::NewGuid().ToString() }
$body = @{ coupon_code = $coupon.code } | ConvertTo-Json
$order = Invoke-RestMethod -Method Post -Uri "$base/carts/$cartId/checkout" -Headers $headers -ContentType 'application/json' -Body $body
$replay = Invoke-RestMethod -Method Post -Uri "$base/carts/$cartId/checkout" -Headers $headers -ContentType 'application/json' -Body $body
$order.id -eq $replay.id # True
Invoke-RestMethod -Uri "$base/orders/$($order.id)"
Invoke-RestMethod -Uri "$base/admin/report"
```

For an isolated, assertion-based demonstration with concurrency, run
`python -m scripts.smoke` using the development environment.
