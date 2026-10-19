# Design decisions

## Goal, scope, and invariants

Build the assignment's checkout/rewards backend within a focused service, using
Python (one of the languages in the Uniblox JD), typed REST contracts, SQL,
tests, request logging, and an automated verification workflow.

The invariants are:

1. Inventory never becomes negative. Cart contents are not stock reservations.
2. A cart yields at most one successful order and cannot change after completion.
3. One successful idempotency key identifies exactly one cart/coupon request and
   replays exactly one immutable order, including after a restart.
4. Order insertion, inventory deduction, coupon redemption, cart closure, and
   successful key binding either all commit or all roll back.
5. A milestone yields at most one coupon. Only committed orders count toward it.
6. A coupon funds at most one successful order. Failed checkout cannot consume it.
7. Gross equals the sum of saved line prices times quantities. Net equals gross
   minus the rounded discount, and remains nonnegative.
8. Reports derive from stored orders/coupons in one consistent read snapshot and
   never mutate business state.

Enforcement lives in `checkout/service.py` and `checkout/db.py`, with relational
uniqueness, foreign keys, and numeric checks in `checkout/schema.sql`. Tests target
observable outcomes, including real simultaneous requests and independent processes.

## Decision: FastAPI with synchronous SQL operations

**Context:** Provide understandable REST contracts, validation, error handling,
interactive documentation, and a repeatable local setup in the timebox.

**Options considered:** TypeScript/Express, Python/FastAPI, Go HTTP, and a Python
standard-library HTTP server. The JD accepts TypeScript, Python, or Go.

**Choice:** Python 3.12+, FastAPI/Pydantic, Uvicorn, sqlite3, pytest, and Ruff.
Route handlers are synchronous functions; FastAPI runs them in its worker pool.

**Why:** Validation and typed OpenAPI response contracts require little plumbing;
sqlite3 is sufficient without adding an ORM or async database driver. Business
logic is separated from HTTP handlers for direct transaction testing.

**Consequences:** Blocking database calls do not run directly on the async event
loop. Requests may wait for a database writer; a bounded timeout yields 503.
Production throughput needs measurement before changing this model.

## Decision: Durable SQLite transactions instead of in-memory locks

**Context:** Protect invariants across threads, retries, restarts, and competing
application processes.

**Options considered:** In-memory state plus a mutex, SQLite transactions,
PostgreSQL with row locks, and optimistic version checks.

**Choice:** Each operation gets its own connection. Every mutation starts
`BEGIN IMMEDIATE` before reading state used for decisions. Reads use explicit
transactions. Enable foreign keys, WAL, a five-second busy timeout, and FULL
synchronous commits. No correctness depends on a Python mutex.
Initialization retries transient lock errors during the initial WAL transition
with a bounded retry window. Failed connection setup closes its connection;
request-time lock errors during either connection setup or transaction execution
use the same retryable 503 contract.

**Why:** SQLite's single writer serializes the entire state transition and avoids
deferred-transaction read-to-write upgrade races. WAL permits readers to observe
committed snapshots while a writer operates. Persisted state survives restarts.

**Consequences:** Writers serialize even when carts touch different products;
this trades throughput for a small, inspectable correctness boundary. Multiple
processes on one machine sharing the same local file are tested. Independent
hosts with their own SQLite files are not a shared service. Network filesystems
are not the deployment design. Lock timeouts return 503 with `Retry-After`.

## Decision: Checkout is one atomic transaction

**Context:** Checkout must not partially deduct stock, consume a coupon, or leave
a cart checked out without a corresponding order.

**Options considered:** Separate writes plus compensation, a payment workflow,
or one local transaction.

**Choice:** Within the writer transaction: resolve a successful key replay;
validate cart/coupon; conditionally decrement stock; save order and line snapshots;
link coupon redemption; close cart; commit. Exceptions roll back all writes.

**Why:** There are no external payment side effects. A single SQL transaction is
the smallest complete atomicity boundary. Conditional inventory updates and
database nonnegative checks add protection beyond application validation.

**Consequences:** Returning from the transaction context commits before the HTTP
handler can send success. A crash before commit leaves no order; a crash after
commit but before response is handled by replay. Storage exceptions are logged
internally and returned as safe errors. Injected triggers test failures after
earlier stock writes and after order/coupon writes, not just validation failures.

## Decision: Successful idempotency keys live on orders

**Context:** A timeout can conceal success; two requests can race with the same
key or different keys for the same cart.

**Options considered:** Per-cart uniqueness only, cached HTTP responses with TTL,
or a persistent idempotency record associated with the committed order.

**Choice:** Global unique key on orders, with a SHA-256 fingerprint of a canonical
`[cart_id, coupon_code]` JSON array. Null and omitted coupons normalize equally.
The order also has a unique cart ID. Same key and fingerprint returns 200 with
the saved receipt; different fingerprint returns 409. First success returns 201.

**Why:** A durable key and order inserted in the same transaction cannot disagree.
The cart uniqueness rule also protects clients that accidentally choose a fresh
key during a retry. A replay must precede current cart/coupon validation.

**Consequences:** Failed attempts do not bind keys, so a corrected cart can be
retried. A key identifies the checkout command for the cart, not a separate cart
version: edits before the first successful attempt affect that attempt. Cart
edits serialize with checkout. Keys never expire while orders are retained.
This service has no tenants; production keys would be scoped to an authenticated
merchant/customer. Key retention and request fingerprints are documented in API.md.
The HTTP layer rejects duplicate key headers, even when their values match, to
avoid first-value/last-value ambiguity between HTTP clients and intermediaries.

## Decision: Current catalog prices, immutable purchase snapshots

**Context:** Product price and availability may change after a customer adds an item.

**Options considered:** Reserve stock and prices on add, expire reservations,
reject changed prices, or calculate from current data at checkout.

**Choice:** Carts show current catalog prices and check stock on edits without
reserving it. Checkout uses current prices and rechecks stock in its transaction.
Orders store product ID/name, unit price, quantity, line totals, gross, coupon,
percentage, discount, currency, and net. No order API permits mutation.

**Why:** Avoid abandoned-cart reservations and preserve enough data to explain
the receipt after catalog changes. This is an explicit business policy rather
than an accidental consequence of joining product tables.

**Consequences:** A valid cart can later fail for insufficient stock, or its price
can change. The cart view is an estimate, not a price promise. Completed cart
views still use current catalog data; their order link is the authoritative
receipt. Product-management endpoints are outside the assignment, so tests use
controlled database changes to simulate catalog updates.

## Decision: Global milestone coupons generated explicitly

**Context:** The assignment does not define customer ownership, backlog,
expiration, eligibility timing, stacking, or generation retries.

**Options considered:** Automatic creation at checkout versus explicit admin
generation; customer-bound versus transferable; latest-only versus queued milestones.

**Choice:** Global committed order count determines eligibility. Admin generation
creates one coupon for the oldest eligible ungenerated milestone: n, 2n, 3n, etc.
Backlog accumulates. Coupons are transferable, single use, have no expiration,
apply to the whole subtotal, and cannot stack. A coupon can only be used once it
has been generated, so it cannot fund the order that first establishes its milestone.

**Why:** This satisfies explicit generation, preserves every earned reward, and
makes concurrent generation understandable. Unique milestone and order coupon
columns reinforce the transaction's single-generation/single-redemption rules.

**Consequences:** Two concurrent admin requests can each succeed if two milestones
are eligible, but never for the same milestone. Retrying admin POST with a backlog
can generate the next reward; only checkout has request idempotency. Coupon list
inspection resolves an ambiguous admin response. UUID-based codes reduce guessing,
but are not a substitute for authorization in a real service.

## Decision: Persist reward configuration

**Context:** Reinterpreting old counts with a different n can duplicate or skip rewards.

**Options considered:** Live mutable environment settings, versioned reward programs,
or fixed configuration per database.

**Choice:** Save n and x on initialization. Reject startup when requested settings
differ from the saved values. Store the percentage on each generated coupon too.
Require a nonempty filesystem path; SQLite's empty-path temporary database is not
valid because operations use separate connections. Bound n to the signed SQLite
integer range so invalid configuration fails before any service is exposed.

**Why:** Avoid silently rewriting history and keep the assignment's configuration
simple and reproducible.

**Consequences:** Different settings require a new evaluation database. A production
system would introduce explicitly versioned programs and effective dates. This
schema is the initial version; initialization is idempotent, not a migration
framework for arbitrary future schema edits.

## Decision: Integer cents and one order-level rounding step

**Context:** Binary floating-point calculations can produce unstable totals.

**Options considered:** Floating point, Decimal amounts, or integer minor units.

**Choice:** Use USD cents everywhere. Gross is the integer sum of saved line
totals. Discount is `(gross_cents * percent + 50) // 100`, nearest cent with ties
rounded up, computed once over the gross. Percentages are integers from 1 to 100.
Net is gross minus discount; no taxes, fees, or shipping.

**Why:** Integer arithmetic is exact and the rounding rule is deterministic.
The allowed percentage range proves discount cannot exceed gross, including 100%.

**Consequences:** Fractional percentages and currencies with different minor units
need an explicit contract extension. Quantities are bounded at 1,000,000; the
seed catalog and exposed operations stay well within SQLite's signed integer range.
Any future catalog-write API must also bound prices and total amounts. Tests
cover half-cent ties, rounding down, ordinary multi-item values, and full discounts.

## Decision: Reports derive from authoritative records

**Context:** Maintaining independent revenue/reward counters can drift on failures.

**Options considered:** Mutable aggregate counters, materialized summaries, or
aggregation from immutable order items and coupon records.

**Choice:** Aggregate successful orders and saved items at request time, and count
coupon redemption links. All report queries run in one explicit read transaction.
Include zero-purchase products. Expose admin lists to make reconciliation possible.

**Why:** No extra accounting writes are needed; one snapshot prevents internally
mixed totals during concurrent writes. Milestones count committed order rows.

**Consequences:** Reports scan history and lists are unpaginated. Production would
need indexes, pagination, query profiling, and possibly incremental projections
with reconciliation. This avoids premature infrastructure in the timebox.

## Decision: Stable errors and focused observability

**Context:** Clients must distinguish validation, absence, conflicts, and retryable failures.

**Options considered:** Generic 400 errors, framework-native envelopes, or a uniform
`error.code` / `error.message` contract.

**Choice:** 422 for invalid input/coupon, 404 for missing resources, 409 for state
conflicts, 503 for lock timeouts, and 500 for unexpected faults. Normalize JSON
validation and HTTP errors. Add `X-Request-ID`, log method, route template, status,
and duration; keep unexpected traces server-side.

**Why:** Status/code pairs let clients implement coherent behavior. Template-based
logging avoids leaking submitted bodies, idempotency keys, and actual resource IDs.

**Consequences:** No metrics backend, distributed tracing pipeline, or request log
storage is provisioned. `/health` verifies connectivity; it does not certify that
the database can accept writes under all conditions. Failed checkout errors use
the first detected rule and do not promise an exhaustive list of all problems.

## Implemented and intentionally deferred

Implemented: all cart/product/order operations, durable atomic checkout, retry
handling, concurrent inventory/coupon protection, milestone generation, consistent
reports and admin inspection, strict input validation, structured errors, purchase
snapshots, seeds, pinned dependencies, typed OpenAPI, executable examples,
fault injection/concurrency/restart/live HTTP tests, request logs, and CI workflow.

Deferred by design: auth/authorization (explicitly optional), real payments
(explicitly optional), frontend (optional), catalog management, stock replenishment,
returns/cancellations, customer identities, taxes/shipping, multi-currency, expiring
coupons, promotion ownership, schema migration tooling, pagination, networked
production database deployment, backups/restore drills, TLS/reverse proxy setup,
rate limiting/body-size policies, metrics infrastructure, and sustained capacity
or chaos testing. These are scope boundaries, not claims of production readiness.
The included real-server concurrency timing is a smoke measurement, not a load SLA.

## Evolution to multiple hosts and production

Use one managed PostgreSQL database rather than separate SQLite files. Keep
unique cart, scoped key, coupon code, and reward-program/milestone constraints.
In one transaction, lock the cart and affected products in a stable ID order,
lock the coupon if present, validate stock, save the immutable receipt and key,
and commit. Conditional stock updates remain useful. Handle unique-key races
and serialization/deadlock failures with bounded retries and replay lookup.

Milestone generation needs a transactional reward-program counter or equivalent
coordination; `COUNT(*)` plus insert without SQLite's writer serialization would
not be enough in a production database. Increment the success counter with order
commit, lock the program row during generation, and maintain unique milestones.
Use repeatable-read reports or a single consistent SQL query for report snapshots.

Introduce real payments through a persisted state machine: reserve/expire stock,
send provider operations with idempotency keys, reconcile callbacks, and use an
outbox for durable events. Do not hold a database lock while making a network
payment request. Add auth, tenant isolation, bounded requests, pagination, migrations,
backup recovery tests, deployment secrets, metrics, and alerting before public use.

## AI use and ownership

Codex assisted with reading the assignment/JD, selecting the stack, implementing
the transaction and HTTP layers, drafting documentation, and running checks.
The design was redirected by checking the actual job description before continuing:
the JD permits Python, TypeScript, or Go; it does not require a speculative stack.

A concrete rejected design is treating idempotency as just an in-memory response
cache or validating the cart before looking up a saved success. A cache would lose
keys after restart; validating first would reject legitimate retries because the
cart is already checked out and its coupon is redeemed. The implementation saves
the key with the order and resolves replay first. Restart and concurrent replay
tests verify those choices.

Generated output was also corrected based on executable evidence: the initial
Windows smoke runner terminated a Python launcher in a way that interfered with
temporary log cleanup. It now signals graceful shutdown over standard input,
waits for the server, and only then releases its temporary database/log directory.
The live HTTP test verifies that the runner completes and cleans up successfully.
No private AI transcripts are included. A human submitter should review and be
able to explain each invariant, test, and trade-off before submission.

## First priorities with another two hours

1. Profile longer mixed workloads and lock waits; extend the tested process-crash
   cases to storage-full faults, host power loss, and longer-running recovery drills.
2. Implement PostgreSQL migrations and run the same concurrency suite against
   independent workers, including row-lock ordering and unique-key conflict handling.
3. Exercise backup/restore and add bounded request sizes and pagination before
   considering a public deployment.

## Execution and repository note

Implementation and local verification were performed in this working session.
The approximate hands-on AI-assisted elapsed time is recorded in VERIFICATION.md;
this is not a claim that the candidate personally spent the assignment's entire
4–6-hour allowance. The submitter should report their actual total time, including
review, modifications, and discussion preparation.

Commits were deferred during implementation at the user's request. After the
automated audit and complete Swagger flow were verified, the completed work was
grouped into separate implementation, automated verification, and submission
documentation commits. These group the reviewed work by concern; they do not
claim to reconstruct the original development timeline. Remote publishing and
deployment have not been performed.

## Follow-up requirement audit

The post-implementation audit reproduced and fixed ambiguous duplicate checkout
key headers, empty/whitespace database paths, oversized milestone settings, and
competing workers' initial WAL setup. Before the startup fix, a cold-start probe
observed 22 lock failures across 120 simultaneous initializations; the regression
test now exercises three fresh files with twelve competing initializers each.
Regression tests were written first and failed against the old behavior before
the fixes were applied. The audit also verified successful multi-product receipts,
mixed available/redeemed coupon counts, concurrent backlog generation, Decimal-based
rounding comparisons, fresh-environment setup, the executable PowerShell example,
browser documentation rendering, and actual process death immediately before and
after transaction commit. These corrections are another concrete example of
validating and redirecting AI-generated implementation through evidence.
