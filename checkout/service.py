import hashlib
import json
import uuid

from checkout.db import Database
from checkout.errors import DomainError


class Store:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _cart(conn, cart_id, require_open=False):
        cart = conn.execute("SELECT * FROM carts WHERE id = ?", (cart_id,)).fetchone()
        if cart is None:
            raise DomainError(404, "cart_not_found", "Cart does not exist")
        if require_open and cart["status"] != "open":
            raise DomainError(409, "cart_checked_out", "Cart has already been checked out")
        return cart

    @staticmethod
    def _cart_items(conn, cart_id):
        return [
            dict(row)
            for row in conn.execute(
                """SELECT p.id AS product_id, p.name AS product_name, p.unit_price_cents,
                      p.inventory, ci.quantity, p.unit_price_cents * ci.quantity AS line_total_cents
               FROM cart_items ci JOIN products p ON p.id = ci.product_id
               WHERE ci.cart_id = ? ORDER BY p.id""",
                (cart_id,),
            )
        ]

    def _cart_view(self, conn, cart_id):
        cart = dict(self._cart(conn, cart_id))
        items = self._cart_items(conn, cart_id)
        order = conn.execute("SELECT id FROM orders WHERE cart_id = ?", (cart_id,)).fetchone()
        return {
            **cart,
            "currency": "USD",
            "items": items,
            "subtotal_cents": sum(item["line_total_cents"] for item in items),
            "order_id": order["id"] if order else None,
        }

    def products(self):
        with self.db.transaction() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM products ORDER BY id")]

    def create_cart(self):
        with self.db.transaction(write=True) as conn:
            cart_id = str(uuid.uuid4())
            conn.execute("INSERT INTO carts (id) VALUES (?)", (cart_id,))
            return self._cart_view(conn, cart_id)

    def cart(self, cart_id):
        with self.db.transaction() as conn:
            return self._cart_view(conn, cart_id)

    def set_item(self, cart_id, product_id, quantity, *, add=False):
        if type(quantity) is not int or not 1 <= quantity <= 1_000_000:
            raise DomainError(
                422, "invalid_quantity", "Quantity must be an integer from 1 to 1000000"
            )
        with self.db.transaction(write=True) as conn:
            self._cart(conn, cart_id, require_open=True)
            product = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
            if product is None:
                raise DomainError(404, "product_not_found", "Product does not exist")
            existing = conn.execute(
                "SELECT quantity FROM cart_items WHERE cart_id = ? AND product_id = ?",
                (cart_id, product_id),
            ).fetchone()
            if not add and existing is None:
                raise DomainError(404, "item_not_found", "Product is not in this cart")
            target = quantity + (existing["quantity"] if add and existing else 0)
            if target > 1_000_000:
                raise DomainError(422, "invalid_quantity", "Combined quantity exceeds 1000000")
            if target > product["inventory"]:
                raise DomainError(
                    409, "insufficient_inventory", "Requested quantity is unavailable"
                )
            conn.execute(
                """INSERT INTO cart_items VALUES (?, ?, ?)
                   ON CONFLICT(cart_id, product_id) DO UPDATE SET quantity = excluded.quantity""",
                (cart_id, product_id, target),
            )
            return self._cart_view(conn, cart_id)

    def remove_item(self, cart_id, product_id):
        with self.db.transaction(write=True) as conn:
            self._cart(conn, cart_id, require_open=True)
            result = conn.execute(
                "DELETE FROM cart_items WHERE cart_id = ? AND product_id = ?", (cart_id, product_id)
            )
            if result.rowcount == 0:
                raise DomainError(404, "item_not_found", "Product is not in this cart")
            return self._cart_view(conn, cart_id)

    @staticmethod
    def _order_view(conn, order_id):
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if row is None:
            raise DomainError(404, "order_not_found", "Order does not exist")
        order = dict(row)
        del order["idempotency_key"]
        del order["request_fingerprint"]
        order["items"] = [
            dict(item)
            for item in conn.execute(
                """SELECT product_id, product_name, unit_price_cents, quantity, line_total_cents
               FROM order_items WHERE order_id = ? ORDER BY product_id""",
                (order_id,),
            )
        ]
        return order

    def order(self, order_id):
        with self.db.transaction() as conn:
            return self._order_view(conn, order_id)

    def checkout(self, cart_id, key, coupon_code=None):
        fingerprint = hashlib.sha256(
            json.dumps([cart_id, coupon_code], separators=(",", ":")).encode()
        ).hexdigest()
        with self.db.transaction(write=True) as conn:
            previous = conn.execute(
                "SELECT id, request_fingerprint FROM orders WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if previous:
                if previous["request_fingerprint"] != fingerprint:
                    raise DomainError(
                        409, "idempotency_conflict", "Key was used for another request"
                    )
                return self._order_view(conn, previous["id"]), True
            self._cart(conn, cart_id, require_open=True)
            items = self._cart_items(conn, cart_id)
            if not items:
                raise DomainError(409, "empty_cart", "Cannot check out an empty cart")
            percent = 0
            if coupon_code is not None:
                coupon = conn.execute(
                    "SELECT * FROM coupons WHERE code = ?", (coupon_code,)
                ).fetchone()
                if coupon is None:
                    raise DomainError(422, "invalid_coupon", "Coupon does not exist")
                if coupon["redeemed_order_id"] is not None:
                    raise DomainError(409, "coupon_redeemed", "Coupon has already been redeemed")
                percent = coupon["discount_percent"]
            for item in items:
                result = conn.execute(
                    """UPDATE products SET inventory = inventory - ?
                       WHERE id = ? AND inventory >= ?""",
                    (item["quantity"], item["product_id"], item["quantity"]),
                )
                if result.rowcount != 1:
                    raise DomainError(409, "insufficient_inventory", "Cart quantity is unavailable")
            gross = sum(item["line_total_cents"] for item in items)
            discount = (gross * percent + 50) // 100  # nearest cent, ties up, once per order
            order_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO orders
                   (id, cart_id, idempotency_key, request_fingerprint, currency, gross_cents,
                    discount_cents, net_cents, coupon_code, discount_percent)
                   VALUES (?, ?, ?, ?, 'USD', ?, ?, ?, ?, ?)""",
                (
                    order_id,
                    cart_id,
                    key,
                    fingerprint,
                    gross,
                    discount,
                    gross - discount,
                    coupon_code,
                    percent,
                ),
            )
            conn.executemany(
                "INSERT INTO order_items VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        order_id,
                        item["product_id"],
                        item["product_name"],
                        item["unit_price_cents"],
                        item["quantity"],
                        item["line_total_cents"],
                    )
                    for item in items
                ],
            )
            if coupon_code is not None:
                conn.execute(
                    "UPDATE coupons SET redeemed_order_id = ? WHERE code = ?",
                    (order_id, coupon_code),
                )
            conn.execute("UPDATE carts SET status = 'checked_out' WHERE id = ?", (cart_id,))
            # Returning from this block commits before the caller can send a response.
            return self._order_view(conn, order_id), False

    def generate_coupon(self):
        with self.db.transaction(write=True) as conn:
            settings = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
            count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            latest = conn.execute("SELECT COALESCE(MAX(milestone), 0) FROM coupons").fetchone()[0]
            milestone = latest + settings["reward_every"]
            if milestone > count:
                raise DomainError(
                    409, "no_eligible_milestone", "No unrewarded milestone is eligible"
                )
            code = "REWARD-" + uuid.uuid4().hex.upper()
            conn.execute(
                "INSERT INTO coupons (code, milestone, discount_percent) VALUES (?, ?, ?)",
                (code, milestone, settings["discount_percent"]),
            )
            return dict(conn.execute("SELECT * FROM coupons WHERE code = ?", (code,)).fetchone())

    def coupons(self):
        with self.db.transaction() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM coupons ORDER BY milestone")]

    def orders(self):
        with self.db.transaction() as conn:
            return [
                self._order_view(conn, row["id"])
                for row in conn.execute("SELECT id FROM orders ORDER BY created_at, id").fetchall()
            ]

    def report(self):
        with self.db.transaction() as conn:
            totals = dict(
                conn.execute(
                    """SELECT COUNT(*) AS successful_orders,
                          COALESCE(SUM(gross_cents), 0) AS gross_cents,
                          COALESCE(SUM(discount_cents), 0) AS discount_cents,
                          COALESCE(SUM(net_cents), 0) AS net_cents FROM orders"""
                ).fetchone()
            )
            coupons = dict(
                conn.execute(
                    """SELECT COUNT(*) AS generated,
                          COUNT(*) - COUNT(redeemed_order_id) AS available,
                          COUNT(redeemed_order_id) AS redeemed FROM coupons"""
                ).fetchone()
            )
            quantities = [
                dict(row)
                for row in conn.execute(
                    """SELECT p.id AS product_id, COALESCE(SUM(oi.quantity), 0) AS quantity
                   FROM products p LEFT JOIN order_items oi ON oi.product_id = p.id
                   GROUP BY p.id ORDER BY p.id"""
                )
            ]
            return {"currency": "USD", **totals, "coupons": coupons, "products": quantities}
