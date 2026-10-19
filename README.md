# Checkout and Rewards Service

Submission repository: [01vineetkumar/uniblox-checkout-rewards](https://github.com/01vineetkumar/uniblox-checkout-rewards).

A persistent ecommerce backend built with Python 3.12+, FastAPI, and SQLite.
It implements the [original assignment](ASSIGNMENT.md) and matches the Uniblox
role's Python, REST API, SQL, testing, logging, and CI/CD interests.

## Quick start

Install Python 3.12 or 3.13. No credentials, external services, or database installation needed.
Run these commands from this folder.

**Windows PowerShell**

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m uvicorn checkout.main:app --host 127.0.0.1 --port 8000
```

**macOS / Linux**

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m uvicorn checkout.main:app --host 127.0.0.1 --port 8000
```

For a runtime-only install, use `requirements.txt` instead.

- Interactive API: [Swagger UI](http://127.0.0.1:8000/docs)
- OpenAPI schema: [openapi.json](http://127.0.0.1:8000/openapi.json)
- Readiness check: [health](http://127.0.0.1:8000/health)
- [API contracts and examples](API.md)
- [Design decisions and trade-offs](DECISIONS.md)
- [Verification results and requirement checklist](VERIFICATION.md)
- [Recorded Swagger flow and screenshots](evidence/swagger/README.md)

The database and five seed products are created on startup. Restarting retains
orders, carts, stock, coupons, and idempotency keys; seeding never resets existing stock.

## Configuration

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `DATABASE_PATH` | `data/store.sqlite3` | Nonempty local SQLite file path; temporary/in-memory storage is rejected |
| `REWARD_EVERY` | `5` | Successful order milestone interval (`n`), integer 1–9223372036854775807 |
| `DISCOUNT_PERCENT` | `10` | Coupon percentage (`x`), integer from 1 to 100 |

For example, in PowerShell, set `$env:REWARD_EVERY = '2'` before starting the server.
Settings are persisted in the database. Starting with different reward settings
against the same file fails explicitly, so old milestones cannot be reinterpreted.
Use a different `DATABASE_PATH` to evaluate another configuration.

All money uses **integer USD cents**. There is no tax or shipping calculation.
Products are `p1` Notebook (1299 cents, stock 100), `p2` Pen set (599, 100),
`p3` Desk lamp (3499, 25), `p4` Travel mug (1899, 50), and
`p5` Limited edition backpack (7999, stock **1**).

## Verification

Windows:

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
.venv\Scripts\python.exe -m pip check
.venv\Scripts\python.exe -m scripts.smoke
```

On macOS/Linux, substitute `.venv/bin/python`.
Tests use temporary databases; the smoke check starts and stops its own real HTTP
server on an available local port and reports timings. It exercises twelve
concurrent coupon contenders, twelve retries of the winner, twelve contenders
for one stock item, and reconciliation of the resulting orders and report.
Timings are observations, not a production performance guarantee.

The test suite also checks strict quantity validation, empty carts, immutable
order prices, restart persistence, key conflicts, coupon backlog, rounding,
full discounts, multi-product receipts, mixed coupon states, cart-edit races,
concurrent generation/backlogs, independent process contention, lock timeouts,
rollback after an injected storage failure, and process death before/after commit.

GitHub Actions runs dependency checks, lint, formatting, and all tests on Python 3.12 and 3.13.
The workflow is included; a hosted CI run requires pushing this repository.

## Correctness model

Every mutation opens a separate SQLite connection and starts `BEGIN IMMEDIATE`
before reading decision-making state. SQLite serializes writers, including
separate application processes sharing the **same local database file**. Checkout
commits the stock deduction, immutable order, key, coupon redemption, and cart
closure in one transaction. A failure rolls all of them back.

Checkout requires an `Idempotency-Key` header. A successful retry with the same
key, cart, and coupon returns the exact stored order with status 200. The original
request returns 201. Reusing a key for a different cart or coupon returns 409.
Only successful checkouts bind keys. A second key cannot check out an already
completed cart. Cart changes serialize with checkout.

Carts do not reserve inventory. The current product price and inventory are used
at checkout. Order line names and prices are saved permanently. For a checked-out
cart, follow its `order_id` for the authoritative receipt.

Each `n`th successful order makes one global coupon eligible. `POST /admin/coupons`
generates one coupon for the oldest eligible unrewarded milestone. Unclaimed
milestones accumulate. Coupons are single use, transferable, have no expiration,
and can only be applied to a later checkout. There is at most one coupon per order.

## Scope and submission

All required backend operations, seed data, automated tests, API documentation,
and `DECISIONS.md` are included. Authentication and real payment processing are
excluded as permitted by the assignment. `/admin/*` routes are administrative;
the demo defaults to loopback and should only be exposed with access control.
Production evolution and other intentional deferrals are documented in `DECISIONS.md`.

The repository includes separate implementation, test, and documentation commits,
prepared after the completed work was reviewed and verified. The project is
published in the public repository linked above; no application deployment has
been performed. The original assignment text is in
`ASSIGNMENT.md`. Local databases, environments, and caches are excluded from Git;
evaluation starts with fresh seed data after setup.

Approximate AI-assisted implementation, audit, and manual verification time:
**40 minutes**. See `DECISIONS.md` for scope, deferrals, and AI use. The submitter
should include their own review and preparation time when reporting total time spent.

## File layout

```text
checkout/api.py       HTTP contracts, validation, errors, request logging
checkout/models.py    Typed request and response schemas
checkout/service.py   Cart, checkout, coupon, and reporting business logic
checkout/db.py        Connection lifecycle, transactions, initialization, seeds
checkout/schema.sql   Relational constraints
tests/                Domain, API, concurrency, failure, and real HTTP tests
scripts/smoke.py      Standalone real-server evaluation and timing
API.md                Endpoint reference and runnable examples
DECISIONS.md          Reasoning, alternatives, limitations, and AI use
VERIFICATION.md       Requirement checklist and verification results
evidence/swagger/    Recorded live Swagger responses and screenshots
.github/workflows/    Automated verification
```
