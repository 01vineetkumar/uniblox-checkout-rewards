"""Regression and requirement gaps identified during the full project audit."""

from decimal import ROUND_HALF_UP, Decimal

import pytest
from conftest import cart_with, checkout, earn_coupon
from fastapi.testclient import TestClient

from checkout.api import create_app
from checkout.db import Database


@pytest.mark.parametrize("path", ["", "   ", ":memory:"])
def test_unusable_database_paths_fail_before_serving(path):
    with pytest.raises(ValueError, match="DATABASE_PATH|file database"):
        Database(path, 2, 10)


def test_empty_database_environment_fails_at_startup(monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", "")
    with pytest.raises(ValueError, match="DATABASE_PATH"):
        with TestClient(create_app()):
            pass


def test_oversized_milestone_interval_is_rejected_clearly(tmp_path):
    with pytest.raises(ValueError, match="REWARD_EVERY"):
        Database(str(tmp_path / "invalid.sqlite3"), 2**63, 10)


@pytest.mark.parametrize("keys", [("first", "second"), ("same", "same")])
def test_duplicate_idempotency_headers_cannot_bind_a_key(client, keys):
    cart_id = cart_with(client)
    response = client.post(
        f"/carts/{cart_id}/checkout",
        json={},
        headers=[("Idempotency-Key", key) for key in keys],
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert client.get("/admin/report").json()["successful_orders"] == 0
    assert client.get(f"/carts/{cart_id}").json()["status"] == "open"
    assert checkout(client, cart_id, keys[0]).status_code == 201


def test_multi_product_receipt_and_mixed_coupon_report(client):
    coupon = earn_coupon(client)
    for index in range(4):
        assert checkout(client, cart_with(client), f"backlog-{index}").status_code == 201
    assert client.post("/admin/coupons").status_code == 201
    assert client.post("/admin/coupons").status_code == 201
    cart_id = cart_with(client, "p1", 2)
    assert (
        client.post(
            f"/carts/{cart_id}/items",
            json={
                "product_id": "p2",
                "quantity": 3,
            },
        ).status_code
        == 200
    )
    cart = client.get(f"/carts/{cart_id}").json()
    assert len(cart["items"]) == 2
    assert cart["subtotal_cents"] == 4395
    response = checkout(client, cart_id, "two-products", coupon)
    assert response.status_code == 201
    order = response.json()
    assert order["gross_cents"] == 4395
    assert order["discount_cents"] == 440
    assert order["net_cents"] == 3955
    assert [item["line_total_cents"] for item in order["items"]] == [2598, 1797]
    report = client.get("/admin/report").json()
    assert report["successful_orders"] == 7
    assert report["coupons"] == {"generated": 3, "available": 2, "redeemed": 1}
    assert report["products"][:2] == [
        {"product_id": "p1", "quantity": 8},
        {"product_id": "p2", "quantity": 3},
    ]
    assert report["gross_cents"] == 6 * 1299 + 4395
    assert report["discount_cents"] == 440
    assert report["net_cents"] == report["gross_cents"] - 440
    db = client.app.state.store.db
    with db.transaction() as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("percent", [1, 10, 33, 67, 100])
def test_money_matches_decimal_reference_for_zero_and_small_totals(tmp_path, percent):
    with TestClient(create_app(str(tmp_path / "rounding.sqlite3"), 1, percent)) as client:
        assert checkout(client, cart_with(client), "initial").status_code == 201
        for price in [0, 1, 4, 5, 15, 101, 1299]:
            coupon = client.post("/admin/coupons").json()["code"]
            with client.app.state.store.db.transaction(write=True) as conn:
                conn.execute("UPDATE products SET unit_price_cents = ? WHERE id = 'p1'", (price,))
            response = checkout(client, cart_with(client), f"price-{price}", coupon)
            assert response.status_code == 201
            order = response.json()
            expected = int(
                (Decimal(price) * Decimal(percent) / 100).quantize(
                    Decimal(1),
                    rounding=ROUND_HALF_UP,
                )
            )
            assert order["gross_cents"] == price
            assert order["discount_cents"] == expected
            assert order["net_cents"] == price - expected >= 0


def test_omitted_and_null_coupons_replay_equally(client):
    schema = client.get("/openapi.json").json()
    assert schema["components"]["schemas"]["CheckoutRequest"]["examples"] == [{}]
    cart_id = cart_with(client)
    first = client.post(
        f"/carts/{cart_id}/checkout",
        json={},
        headers={
            "Idempotency-Key": "normalized",
        },
    )
    assert first.status_code == 201
    replay = checkout(client, cart_id, "normalized", None)
    assert replay.status_code == 200
    assert replay.json() == first.json()


def test_unknown_coupon_and_failed_orders_never_earn_rewards(client):
    assert checkout(client, cart_with(client), "first-success").status_code == 201
    cart_id = cart_with(client)
    response = checkout(client, cart_id, "retry", "REWARD-" + "F" * 32)
    assert response.status_code == 422
    assert client.post("/admin/coupons").status_code == 409
    assert client.get("/admin/report").json()["successful_orders"] == 1
    assert checkout(client, cart_id, "retry").status_code == 201
    assert client.post("/admin/coupons").json()["milestone"] == 2
