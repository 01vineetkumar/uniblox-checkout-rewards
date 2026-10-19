# Live Swagger verification

The complete flow was executed on **20 October 2026**, approximately **03:00–03:05
Asia/Kolkata**, through Swagger's **Try it out / Execute** controls. Requests were
submitted through the browser UI, not a separate API script.

The session began with zero orders and coupons. The [recorded responses](results.json)
contain 44 checked steps, including cart creation and edits, order retrieval,
ordinary and discounted checkout retries, key conflicts, coupon eligibility,
generation, redemption, duplicate generation rejection, and coupon reuse rejection.

| Check | Observed result |
| --- | --- |
| First checkout | 201, one saved order |
| Same request retried twice | 200, identical receipt, stock deducted once |
| Different key on completed cart | 409 `cart_checked_out` |
| Previously successful key on another cart | 409 `idempotency_conflict` |
| Generation before five successful orders | 409 `no_eligible_milestone` |
| Generation at milestone five | 201, one 10% coupon |
| Repeat generation for that milestone | 409 `no_eligible_milestone` |
| Coupon applied to sixth order | Gross 1299, discount 130, net 1169 cents |
| Discounted checkout retried | 200, identical discounted receipt |
| Coupon reused on seventh cart | 409 `coupon_redeemed`; cart remains open |
| Report repeated and reconciled with saved orders | Six orders; gross 7794, discount 130, net 7664 cents |
| Inventory and coupons | Notebook stock 94; one coupon generated and redeemed |

Screenshots:

- [Discounted checkout retry](discounted-retry.png)
- [Coupon reuse rejected](coupon-reuse-rejected.png)
- [Final reconciled report](final-report.png)

These are historical verification artifacts, not seed data. Database files are
excluded from Git. A new installation starts with five seeded products, zero
orders, and zero coupons. See [API.md](../../API.md) to reproduce the flow and
[VERIFICATION.md](../../VERIFICATION.md) for the automated and concurrency checks.
