import sqlite3
from unittest.mock import patch

from conftest import cart_with, checkout

from checkout.db import Database
from checkout.errors import DomainError


def test_lock_timeout_returns_retryable_error_and_preserves_cart(client):
    cart_id = cart_with(client)
    database = client.app.state.store.db
    original_connect = Database.connect

    def short_timeout(self):
        conn = original_connect(self)
        conn.execute("PRAGMA busy_timeout=10")
        return conn

    with database.transaction(write=True):
        with patch.object(Database, "connect", short_timeout):
            response = checkout(client, cart_id, "locked")
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.json()["error"]["code"] == "database_busy"
    assert checkout(client, cart_id, "locked").status_code == 201
    assert client.get("/admin/report").json()["successful_orders"] == 1


def test_unexpected_storage_error_returns_safe_error_and_can_retry(client):
    cart_id = cart_with(client)
    database = client.app.state.store.db
    with database.transaction(write=True) as conn:
        conn.execute("""CREATE TRIGGER fail_insert BEFORE INSERT ON order_items
                        BEGIN SELECT RAISE(ABORT, 'private database detail'); END""")
    response = checkout(client, cart_id, "internal-error")
    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": "Internal server error",
        }
    }
    assert "X-Request-ID" in response.headers
    assert client.get("/admin/report").json()["successful_orders"] == 0
    assert client.get("/products").json()[0]["inventory"] == 100
    with database.transaction(write=True) as conn:
        conn.execute("DROP TRIGGER fail_insert")
    assert checkout(client, cart_id, "internal-error").status_code == 201


def test_constraints_reject_negative_inventory(client):
    database = client.app.state.store.db
    try:
        with database.transaction(write=True) as conn:
            conn.execute("UPDATE products SET inventory = -1 WHERE id = 'p1'")
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("negative inventory should be rejected by the database")
    assert client.get("/products").json()[0]["inventory"] == 100


def test_unknown_operational_error_is_not_misreported_as_busy(client):
    try:
        with client.app.state.store.db.transaction() as conn:
            conn.execute("SELECT * FROM nonexistent_table")
    except DomainError:
        raise AssertionError(
            "non-lock failures must not be converted to retryable lock failures"
        ) from None
    except sqlite3.OperationalError:
        pass
    else:
        raise AssertionError("expected an operational failure")
