# Verification record

Local verification performed on Windows with Python 3.12.14 on **20 October 2026
(Asia/Kolkata)**. Approximate AI-assisted implementation and initial checks:
**15 minutes**; follow-up requirement audit and fixes: **15 minutes**; live Swagger
verification and submission preparation: **10 minutes**. Approximately **40 minutes**
total, excluding earlier requirement/JD discussion and the submitter's own review time.

## Results

| Check | Result |
| --- | --- |
| `python -m pytest -q` | 60 tests passed, no warnings |
| `python -m ruff check .` | Passed |
| `python -m ruff format --check .` | Passed |
| `python -m pip check` | No broken requirements |
| `python -m scripts.smoke` | Real HTTP/TCP flow passed |
| Fresh virtual environment using only `requirements-dev.txt` | Installed successfully; dependency checks and full suite passed |
| Dependency vulnerability audit | `pip-audit` reported no known vulnerabilities in pinned runtime/development packages |
| Executable PowerShell example from API.md | Passed against an isolated live server: six orders, one coupon redemption, identical replay |
| Swagger UI browser check | Rendered all routes and checkout parameters/responses; no browser warnings/errors |
| Complete flow executed through Swagger | 44 recorded actions passed: cart edits, six orders, checkout replays, coupon generation/redemption/reuse rejection, reconciled reports |
| Concurrent cold-start probe | 120 initializations across ten fresh files; zero failures after the fix |
| Crash recovery and database integrity | Before/after-commit process death tests passed; SQLite integrity and foreign-key checks passed |

The smoke run starts a real Uvicorn process with a temporary database. Twelve
concurrent attempts to use one coupon yield one successful redemption and eleven
conflicts. Twelve simultaneous retries replay the same saved order. Twelve carts
competing for the limited-stock product yield one order and eleven stock conflicts.
The resulting four orders reconcile with report revenue/discount/coupon counts.
API documentation, OpenAPI, startup logs, and request logs are also checked.
The runner shuts down its server and removes its temporary state on completion.

A measured 12-contender coupon race completed in approximately **200 ms** on this
machine in the follow-up audit. This is a smoke timing, not a sustained throughput benchmark.

## Issues reproduced and fixed

1. **Unusable database configuration:** an empty path opened a private temporary
   SQLite database during each operation, so startup succeeded but later requests
   failed. Empty/whitespace paths now fail explicitly at startup. Oversized reward
   intervals also fail validation instead of overflowing the SQLite integer binding.
2. **Ambiguous checkout keys:** duplicate `Idempotency-Key` headers silently selected
   one value. Duplicate headers now return 422 before binding a key or changing state.
3. **Concurrent first startup:** enabling WAL while other workers initialized a
   fresh file could fail immediately with `database is locked`. The original probe
   saw 22 failures in 120 initializations. Startup now retries transient lock errors
   within a bounded window; the same probe reports zero failures. Connection setup
   failures also close their connections and request-time locks return the same 503
   error whether encountered during setup or transaction execution.
4. **Documentation gaps:** clarified malformed-encoding errors and configuration
   bounds. Swagger's default checkout example now uses `{}` so the example does
   not imply a generated sample coupon exists. CI now also checks dependency consistency.

Regression tests first reproduced the configuration/header failures against the
old implementation, then passed after the fixes. Additional tests cover successful
multi-product purchases, mixed available/redeemed coupons, Decimal-based rounding
comparisons at five percentages, backlog generation races, null/omitted coupon
equivalence, and actual process death immediately before and after checkout commit.

## Requirement-to-evidence map

| Assignment requirement | Implementation | Verification |
| --- | --- | --- |
| At least five seeded products, limited inventory | `checkout/db.py` | Cart lifecycle test; limited-stock races |
| Create/get cart; add/change/remove items | `checkout/api.py`, `checkout/service.py` | Cart lifecycle, invalid quantity, concurrent-add tests |
| Product/quantity validation | Strict Pydantic models plus service validation | Parameterized invalid values; missing products; stock rejection |
| No overselling | Writer transaction and conditional stock update | Eight simultaneous API contenders; two independent processes; real TCP race |
| Cart checked out once | Unique order cart ID and serialized cart state | Different-key same-cart race; immutable completed cart checks |
| Retry without duplicate inventory/order | Key/fingerprint persisted on order | Twelve simultaneous retries; restart replay; different-cart key race |
| Order receipt survives product changes | Saved order lines and totals | Purchase price/name snapshot test and replay after price change |
| Exact money and deterministic discounts | Integer cents and order-level rounding | Multi-product receipt; Decimal reference for seven totals at five percentages; 100% discount |
| Coupon eligibility and one generation per milestone | Serialized generation and unique milestone | Eligibility, backlog order, competing-generation tests |
| Coupon single redemption under concurrency | Atomic redemption plus unique order coupon | Eight API contenders; two independent processes; twelve real HTTP contenders |
| Failed checkout never consumes coupon | One transaction for all writes | Out-of-stock rollback and late injected storage failure |
| Reconciled read-only reporting | One read snapshot over orders/items/coupons | Repeated reads, multi-product/mixed-coupon reconciliation, reports during concurrent checkout |
| Distinguishable useful errors | Common error envelope and HTTP status codes | Invalid JSON/header/body, missing resource, conflicts, 503, safe 500 |
| Persistence and restart behavior | File-backed SQLite, idempotent initialization | Order/stock/key/coupon restarts; actual pre/post-commit crashes; cold-start race; changed-config rejection |
| Repeatable setup and run | Pinned requirements and README commands | Fresh-environment installation, launch, health, full suite and real HTTP verification |
| API documentation | API.md plus typed OpenAPI and Swagger | Schema tests; real-server checks; browser rendering; executed PowerShell example |
| Design rationale and AI use | DECISIONS.md | Explicit invariants, 10 decision records, deferrals, scale strategy, corrections |
| Repository history | Separate implementation, test, and documentation commits | Grouped after review and verification, as authorized; no push performed |

## Scope of evidence

The Python 3.12 Windows run is complete. A GitHub Actions workflow is supplied for
Python 3.12 and 3.13 on Linux; hosted CI has not been run because nothing has been
pushed. No production deployment or external service is required or configured.
No separate frontend is included. `/docs` and `/openapi.json` are tested through
HTTP. Initial browser documentation inspection used an isolated live server, which
was stopped after the audit. A subsequent complete flow was executed using
Swagger's actual Try it out / Execute controls on the local port-8000 service.
That session intentionally created six orders and one redeemed coupon, leaving a
seventh cart open after its coupon was rejected. See the
[Swagger evidence](evidence/swagger/README.md) for saved responses and screenshots.
Local database files are excluded from the submission; fresh checkouts start with
the original product inventory and no orders or coupons.

All required backend behavior and documentation are implemented and verified in
the scenarios above. Local commits package the verified work for submission;
remote repository sharing and hosted CI remain pending because no push is authorized.
No deployment was performed. AI ownership and the submitter's ability to explain
the design require human review, not an automated test result.

Tests demonstrate the listed scenarios. They do not prove the absence of every
possible defect. Intentional production deferrals are described in DECISIONS.md.
