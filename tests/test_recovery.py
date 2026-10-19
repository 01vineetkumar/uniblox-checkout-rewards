"""Exercise actual process death on either side of the checkout commit."""

import multiprocessing
import os

import pytest
from conftest import cart_with, checkout, earn_coupon

from checkout.db import Database
from checkout.service import Store


def crash_checkout(path, cart_id, coupon, after_commit):
    store = Store(Database(path, 2, 10))
    if not after_commit:
        original_view = store._order_view

        def exit_before_commit(conn, order_id):
            # Ensure the complete order and coupon writes exist inside the transaction.
            original_view(conn, order_id)
            os._exit(17)

        store._order_view = exit_before_commit
    store.checkout(cart_id, "crash-key", coupon)
    # The transaction committed, but no caller has received its result.
    os._exit(18)


@pytest.mark.parametrize("after_commit", [False, True])
def test_process_death_before_or_after_commit_is_recoverable(client, after_commit):
    coupon = earn_coupon(client)
    cart_id = cart_with(client, quantity=2)
    before = client.get("/admin/report").json()
    inventory = client.get("/products").json()
    path = client.app.state.store.db.path
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=crash_checkout, args=(path, cart_id, coupon, after_commit))
    try:
        process.start()
        process.join(timeout=20)
        assert process.exitcode == (18 if after_commit else 17)
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)

    # Reopen independently and let SQLite recover any uncommitted WAL work.
    reopened = Database(path, 2, 10)
    with reopened.transaction() as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    if not after_commit:
        assert client.get("/admin/report").json() == before
        assert client.get("/products").json() == inventory
        assert client.get(f"/carts/{cart_id}").json()["status"] == "open"
        assert client.get("/admin/coupons").json()[0]["redeemed_order_id"] is None
    response = checkout(client, cart_id, "crash-key", coupon)
    assert response.status_code == (200 if after_commit else 201)
    assert response.headers["Idempotency-Replayed"] == str(after_commit).lower()
    saved = response.json()
    assert checkout(client, cart_id, "crash-key", coupon).json() == saved
    assert client.get("/products").json()[0]["inventory"] == inventory[0]["inventory"] - 2
    assert (
        client.get("/admin/report").json()["successful_orders"] == before["successful_orders"] + 1
    )
    assert client.get("/admin/coupons").json()[0]["redeemed_order_id"] == saved["id"]
