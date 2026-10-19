import sqlite3

import pytest
from conftest import cart_with, checkout, earn_coupon
from fastapi.testclient import TestClient

from checkout.api import create_app
from checkout.db import Database


def test_cart_lifecycle_and_validation(client):
    products = client.get("/products").json()
    assert len(products) >= 5
    assert any(product["inventory"] == 1 for product in products)
    cart_id = cart_with(client, quantity=2)
    route = f"/carts/{cart_id}"
    response = client.post(route + "/items", json={"product_id": "p1", "quantity": 1})
    assert response.json()["items"][0]["quantity"] == 3
    assert response.json()["subtotal_cents"] == 3897
    assert client.put(route + "/items/p1", json={"quantity": 4}).json()["subtotal_cents"] == 5196
    assert client.put(route + "/items/p2", json={"quantity": 1}).status_code == 404
    assert (
        client.post(route + "/items", json={"product_id": "missing", "quantity": 1}).status_code
        == 404
    )
    assert (
        client.post(route + "/items", json={"product_id": "p5", "quantity": 2}).status_code == 409
    )
    assert client.delete(route + "/items/p1").json()["items"] == []
    assert client.delete(route + "/items/p1").status_code == 404
    assert checkout(client, cart_id, "empty").json()["error"]["code"] == "empty_cart"


@pytest.mark.parametrize("quantity", [0, -1, 1.5, "2", True, None, 1_000_001])
def test_invalid_quantities_never_enter_cart(client, quantity):
    cart_id = client.post("/carts").json()["id"]
    response = client.post(
        f"/carts/{cart_id}/items", json={"product_id": "p1", "quantity": quantity}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert client.get(f"/carts/{cart_id}").json()["items"] == []


def test_checkout_replay_and_key_conflicts(client):
    cart_id = cart_with(client, quantity=2)
    first = checkout(client, cart_id, "unique-key")
    assert first.status_code == 201
    assert first.headers["Idempotency-Replayed"] == "false"
    replay = checkout(client, cart_id, "unique-key")
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert first.json() == replay.json()
    assert client.get(first.headers["Location"]).json() == first.json()
    assert checkout(client, cart_id, "different").json()["error"]["code"] == "cart_checked_out"
    other = cart_with(client)
    assert checkout(client, other, "unique-key").json()["error"]["code"] == "idempotency_conflict"
    assert checkout(client, cart_id, "unique-key", "REWARD-" + "A" * 32).status_code == 409
    assert client.get("/products").json()[0]["inventory"] == 98
    assert client.get("/admin/report").json()["successful_orders"] == 1
    assert (
        client.post(f"/carts/{cart_id}/items", json={"product_id": "p2", "quantity": 1}).status_code
        == 409
    )
    assert client.put(f"/carts/{cart_id}/items/p1", json={"quantity": 1}).status_code == 409
    assert client.delete(f"/carts/{cart_id}/items/p1").status_code == 409


def test_price_at_checkout_and_immutable_order_snapshots(client):
    cart_id = cart_with(client)
    with client.app.state.store.db.transaction(write=True) as conn:
        conn.execute(
            "UPDATE products SET name = 'New name', unit_price_cents = 1501 WHERE id = 'p1'"
        )
    assert client.get(f"/carts/{cart_id}").json()["subtotal_cents"] == 1501
    placed = checkout(client, cart_id, "price").json()
    assert placed["items"][0]["product_name"] == "New name"
    assert placed["gross_cents"] == 1501
    with client.app.state.store.db.transaction(write=True) as conn:
        conn.execute(
            "UPDATE products SET name = 'Later name', unit_price_cents = 9999 WHERE id = 'p1'"
        )
    assert client.get(f"/orders/{placed['id']}").json() == placed
    assert checkout(client, cart_id, "price").json() == placed
    assert client.get(f"/carts/{cart_id}").json()["order_id"] == placed["id"]


def test_coupon_milestones_redemption_replay_and_report(client):
    assert client.post("/admin/coupons").json()["error"]["code"] == "no_eligible_milestone"
    coupon = earn_coupon(client)
    assert client.post("/admin/coupons").status_code == 409
    cart_id = cart_with(client, quantity=3)
    placed = checkout(client, cart_id, "discount", coupon)
    assert placed.status_code == 201
    assert placed.json()["gross_cents"] == 3897
    assert placed.json()["discount_cents"] == 390
    assert placed.json()["net_cents"] == 3507
    assert checkout(client, cart_id, "discount", coupon).json() == placed.json()
    rejected = checkout(client, cart_with(client), "used", coupon)
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "coupon_redeemed"
    report = client.get("/admin/report").json()
    assert report == client.get("/admin/report").json()
    orders = client.get("/admin/orders").json()
    coupons = client.get("/admin/coupons").json()
    assert report["successful_orders"] == len(orders) == 3
    for field in ("gross_cents", "discount_cents", "net_cents"):
        assert report[field] == sum(order[field] for order in orders)
    assert report["gross_cents"] - report["discount_cents"] == report["net_cents"]
    assert report["coupons"] == {"generated": 1, "available": 0, "redeemed": 1}
    assert coupons[0]["redeemed_order_id"] == placed.json()["id"]
    for product in report["products"]:
        assert product["quantity"] == sum(
            item["quantity"]
            for order in orders
            for item in order["items"]
            if item["product_id"] == product["product_id"]
        )


def test_failure_rolls_back_inventory_coupon_cart_and_key(client):
    coupon = earn_coupon(client)
    cart_id = cart_with(client, "p1")
    client.post(f"/carts/{cart_id}/items", json={"product_id": "p5", "quantity": 1})
    # Another checkout buys p5 after it was added. p1 is decremented first in the transaction.
    assert checkout(client, cart_with(client, "p5"), "last-stock").status_code == 201
    before = client.get("/products").json()
    report = client.get("/admin/report").json()
    failed = checkout(client, cart_id, "recoverable", coupon)
    assert failed.status_code == 409
    assert client.get("/products").json() == before
    assert client.get("/admin/report").json() == report
    assert client.get("/admin/coupons").json()[0]["redeemed_order_id"] is None
    assert client.get(f"/carts/{cart_id}").json()["status"] == "open"
    client.delete(f"/carts/{cart_id}/items/p5")
    assert checkout(client, cart_id, "recoverable", coupon).status_code == 201


def test_late_storage_failure_rolls_back_every_write(client):
    coupon = earn_coupon(client)
    cart_id = cart_with(client)
    store = client.app.state.store
    before = store.report()
    inventory = store.products()
    with store.db.transaction(write=True) as conn:
        conn.execute("""CREATE TRIGGER fail_checkout BEFORE UPDATE OF status ON carts
                        BEGIN SELECT RAISE(ABORT, 'injected storage failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="injected storage failure"):
        store.checkout(cart_id, "failure", coupon)
    assert store.report() == before
    assert store.products() == inventory
    assert store.cart(cart_id)["status"] == "open"
    with store.db.transaction(write=True) as conn:
        conn.execute("DROP TRIGGER fail_checkout")
    assert checkout(client, cart_id, "failure", coupon).status_code == 201


@pytest.mark.parametrize("price,percent,discount", [(5, 10, 1), (4, 10, 0), (1299, 100, 1299)])
def test_rounding_and_full_discount(tmp_path, price, percent, discount):
    with TestClient(create_app(str(tmp_path / "rounding.sqlite3"), 1, percent)) as client:
        checkout(client, cart_with(client), "seed")
        coupon = client.post("/admin/coupons").json()["code"]
        with client.app.state.store.db.transaction(write=True) as conn:
            conn.execute("UPDATE products SET unit_price_cents = ? WHERE id = 'p1'", (price,))
        result = checkout(client, cart_with(client), "rounded", coupon).json()
        assert result["discount_cents"] == discount
        assert result["net_cents"] == price - discount >= 0


def test_backlog_milestones_generated_in_order(client):
    for index in range(6):
        assert checkout(client, cart_with(client), f"order-{index}").status_code == 201
    assert [client.post("/admin/coupons").json()["milestone"] for _ in range(3)] == [2, 4, 6]
    assert client.post("/admin/coupons").status_code == 409
    assert client.get("/admin/report").json()["coupons"] == {
        "generated": 3,
        "available": 3,
        "redeemed": 0,
    }


def test_validation_errors_and_resource_errors(client):
    cart_id = cart_with(client)
    path = f"/carts/{cart_id}/checkout"
    assert client.post(path, json={}).status_code == 422
    assert client.post(path, json={}, headers={"Idempotency-Key": "bad key"}).status_code == 422
    assert (
        client.post(path, json={"unexpected": True}, headers={"Idempotency-Key": "ok"}).status_code
        == 422
    )
    assert (
        checkout(client, cart_id, "invalid", "REWARD-" + "F" * 32).json()["error"]["code"]
        == "invalid_coupon"
    )
    assert (
        client.post(
            path, content="{", headers={"Content-Type": "application/json", "Idempotency-Key": "ok"}
        ).status_code
        == 422
    )
    for path in ("/carts/missing", "/orders/missing", "/missing"):
        response = client.get(path)
        assert response.status_code == 404
        assert "error" in response.json()
    assert client.get("/health").json() == {"status": "ok"}
    assert "X-Request-ID" in client.get("/health").headers
    schema = client.get("/openapi.json").json()
    assert schema["paths"]["/carts/{cart_id}/checkout"]["post"]["responses"]["200"]
    assert client.get("/docs").status_code == 200


def test_restart_retains_orders_inventory_and_keys(tmp_path):
    path = str(tmp_path / "restart.sqlite3")
    with TestClient(create_app(path, 2, 10)) as client:
        cart_id = cart_with(client)
        placed = checkout(client, cart_id, "restart").json()
        stock = client.get("/products").json()
    with TestClient(create_app(path, 2, 10)) as client:
        assert checkout(client, cart_id, "restart").json() == placed
        assert client.get("/products").json() == stock
    with pytest.raises(ValueError, match="saved settings"):
        Database(path, 3, 10)


def test_coupon_and_discounted_replay_survive_restart(tmp_path):
    path = str(tmp_path / "coupon-restart.sqlite3")
    with TestClient(create_app(path, 2, 10)) as client:
        coupon = earn_coupon(client)
    with TestClient(create_app(path, 2, 10)) as client:
        cart_id = cart_with(client)
        response = checkout(client, cart_id, "persisted-coupon", coupon)
        assert response.status_code == 201
        placed = response.json()
    with TestClient(create_app(path, 2, 10)) as client:
        response = checkout(client, cart_id, "persisted-coupon", coupon)
        assert response.status_code == 200
        assert response.json() == placed
        assert client.get("/admin/coupons").json()[0]["redeemed_order_id"] == placed["id"]


def test_empty_report_is_zero_and_has_no_side_effects(client):
    initial = client.get("/admin/report").json()
    assert initial["successful_orders"] == 0
    assert initial["gross_cents"] == initial["discount_cents"] == initial["net_cents"] == 0
    assert initial["coupons"] == {"generated": 0, "available": 0, "redeemed": 0}
    assert len(initial["products"]) == 5
    assert all(product["quantity"] == 0 for product in initial["products"])
    assert client.get("/admin/report").json() == initial
    assert client.get("/admin/orders").json() == []
    assert client.get("/admin/coupons").json() == []


@pytest.mark.parametrize("n,x", [(0, 10), (1, 0), (1, 101), (True, 10)])
def test_invalid_reward_configuration(tmp_path, n, x):
    with pytest.raises(ValueError):
        Database(str(tmp_path / "invalid.sqlite3"), n, x)
