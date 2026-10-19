import pytest
from fastapi.testclient import TestClient

from checkout.api import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(str(tmp_path / "store.sqlite3"), 2, 10)) as client:
        yield client


def cart_with(client, product="p1", quantity=1):
    created = client.post("/carts")
    assert created.status_code == 201
    cart_id = created.json()["id"]
    added = client.post(
        f"/carts/{cart_id}/items", json={"product_id": product, "quantity": quantity}
    )
    assert added.status_code == 200
    return cart_id


def checkout(client, cart_id, key, coupon=None):
    return client.post(
        f"/carts/{cart_id}/checkout",
        headers={"Idempotency-Key": key},
        json={"coupon_code": coupon},
    )


def earn_coupon(client):
    for index in range(2):
        result = checkout(client, cart_with(client), f"seed-{index}")
        assert result.status_code == 201
    response = client.post("/admin/coupons")
    assert response.status_code == 201
    return response.json()["code"]
