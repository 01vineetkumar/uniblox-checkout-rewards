import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from conftest import cart_with, checkout, earn_coupon

from checkout.db import Database
from checkout.errors import DomainError
from checkout.service import Store


def race(actions):
    barrier = Barrier(len(actions))

    def run(action):
        barrier.wait(timeout=15)
        try:
            return action()
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=len(actions)) as pool:
        return list(pool.map(run, actions))


def test_concurrent_workers_can_initialize_a_fresh_database(tmp_path):
    # No fixture has initialized this file before workers compete for WAL/schema setup.
    for run in range(3):
        path = str(tmp_path / f"cold-start-{run}.sqlite3")
        stores = race([lambda path=path: Store(Database(path, 2, 10)) for _ in range(12)])
        assert all(isinstance(store, Store) for store in stores)
        assert all(store.products()[-1]["inventory"] == 1 for store in stores)
        assert stores[0].report()["successful_orders"] == 0
        with stores[0].db.transaction() as conn:
            assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 5


def test_concurrent_http_retries_return_one_order(client):
    cart_id = cart_with(client, quantity=2)
    responses = race([lambda: checkout(client, cart_id, "same-key") for _ in range(12)])
    assert sorted(response.status_code for response in responses) == [200] * 11 + [201]
    assert len({response.json()["id"] for response in responses}) == 1
    assert client.get("/admin/report").json()["successful_orders"] == 1
    assert client.get("/products").json()[0]["inventory"] == 98


def test_concurrent_different_keys_same_cart(client):
    cart_id = cart_with(client)
    responses = race(
        [lambda index=index: checkout(client, cart_id, f"different-{index}") for index in range(8)]
    )
    assert sorted(response.status_code for response in responses) == [201] + [409] * 7
    assert client.get("/admin/report").json()["successful_orders"] == 1


def test_concurrent_inventory_competition(client):
    carts = [cart_with(client, "p5") for _ in range(8)]
    responses = race(
        [lambda cart_id=cart_id: checkout(client, cart_id, cart_id) for cart_id in carts]
    )
    assert sorted(response.status_code for response in responses) == [201] + [409] * 7
    assert client.get("/products").json()[-1]["inventory"] == 0
    assert client.get("/admin/report").json()["successful_orders"] == 1


def test_concurrent_coupon_redemption(client):
    coupon = earn_coupon(client)
    carts = [cart_with(client) for _ in range(8)]
    responses = race(
        [lambda cart_id=cart_id: checkout(client, cart_id, cart_id, coupon) for cart_id in carts]
    )
    assert sorted(response.status_code for response in responses) == [201] + [409] * 7
    assert all(
        response.json()["error"]["code"] == "coupon_redeemed"
        for response in responses
        if response.status_code == 409
    )
    assert client.get("/admin/report").json()["coupons"]["redeemed"] == 1
    assert client.get("/products").json()[0]["inventory"] == 97


def test_concurrent_coupon_generation(client):
    for index in range(2):
        checkout(client, cart_with(client), f"earn-{index}")
    responses = race([lambda: client.post("/admin/coupons") for _ in range(8)])
    assert sorted(response.status_code for response in responses) == [201] + [409] * 7
    assert [coupon["milestone"] for coupon in client.get("/admin/coupons").json()] == [2]


def test_concurrent_generation_drains_backlog_once_per_milestone(client):
    for index in range(6):
        assert checkout(client, cart_with(client), f"backlog-{index}").status_code == 201
    responses = race([lambda: client.post("/admin/coupons") for _ in range(8)])
    assert sorted(response.status_code for response in responses) == [201] * 3 + [409] * 5
    assert sorted(
        response.json()["milestone"] for response in responses if response.status_code == 201
    ) == [2, 4, 6]
    coupons = client.get("/admin/coupons").json()
    assert len({coupon["code"] for coupon in coupons}) == 3
    assert client.get("/admin/report").json()["coupons"] == {
        "generated": 3,
        "available": 3,
        "redeemed": 0,
    }


def test_cart_edit_races_checkout(client):
    cart_id = cart_with(client)
    results = race(
        [
            lambda: checkout(client, cart_id, "edit-race"),
            lambda: client.put(f"/carts/{cart_id}/items/p1", json={"quantity": 2}),
        ]
    )
    order, edit = results
    assert order.status_code == 201
    quantity = 2 if edit.status_code == 200 else 1
    assert edit.status_code in (200, 409)
    assert order.json()["items"][0]["quantity"] == quantity
    assert client.get("/products").json()[0]["inventory"] == 100 - quantity


def test_same_key_competes_across_different_carts(client):
    carts = [cart_with(client) for _ in range(8)]
    responses = race(
        [lambda cart_id=cart_id: checkout(client, cart_id, "shared-key") for cart_id in carts]
    )
    assert sorted(response.status_code for response in responses) == [201] + [409] * 7
    assert all(
        response.json()["error"]["code"] == "idempotency_conflict"
        for response in responses
        if response.status_code == 409
    )
    assert client.get("/admin/report").json()["successful_orders"] == 1


def test_concurrent_additions_do_not_lose_quantities(client):
    cart_id = cart_with(client)
    responses = race(
        [
            lambda: client.post(f"/carts/{cart_id}/items", json={"product_id": "p1", "quantity": 1})
            for _ in range(8)
        ]
    )
    assert all(response.status_code == 200 for response in responses)
    assert client.get(f"/carts/{cart_id}").json()["items"][0]["quantity"] == 9


def test_reports_remain_internally_consistent_during_writes(client):
    carts = [cart_with(client) for _ in range(12)]

    def read_reports():
        for _ in range(30):
            response = client.get("/admin/report")
            assert response.status_code == 200
            report = response.json()
            count = report["successful_orders"]
            assert report["gross_cents"] == report["net_cents"] == count * 1299
            assert report["discount_cents"] == 0
            assert report["products"][0]["quantity"] == count
        return "consistent"

    results = race(
        [
            read_reports,
            *[lambda cart_id=cart_id: checkout(client, cart_id, cart_id) for cart_id in carts],
        ]
    )
    assert results[0] == "consistent"
    assert all(response.status_code == 201 for response in results[1:])
    assert client.get("/admin/report").json()["successful_orders"] == 12


def process_checkout(path, cart_id, barrier, queue, coupon):
    store = Store(Database(path, 2, 10))
    barrier.wait(timeout=30)
    try:
        store.checkout(cart_id, cart_id, coupon)
        queue.put("success")
    except DomainError as exc:
        queue.put(exc.code)


@pytest.mark.parametrize("competition", ["stock", "coupon"])
def test_independent_processes_share_database_lock(client, competition):
    coupon = earn_coupon(client) if competition == "coupon" else None
    carts = [cart_with(client, "p1" if coupon else "p5") for _ in range(2)]
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    queue = context.Queue()
    processes = [
        context.Process(
            target=process_checkout,
            args=(client.app.state.store.db.path, cart_id, barrier, queue, coupon),
        )
        for cart_id in carts
    ]
    try:
        for process in processes:
            process.start()
        outcomes = [queue.get(timeout=40) for _ in processes]
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        assert sorted(outcomes) == [
            "coupon_redeemed" if coupon else "insufficient_inventory",
            "success",
        ]
        report = client.get("/admin/report").json()
        assert report["successful_orders"] == (3 if coupon else 1)
        products = client.get("/products").json()
        if coupon:
            assert products[0]["inventory"] == 97
            assert report["coupons"]["redeemed"] == 1
        else:
            assert products[-1]["inventory"] == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)
        queue.close()
