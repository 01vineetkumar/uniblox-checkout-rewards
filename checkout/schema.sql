CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    reward_every INTEGER NOT NULL CHECK (reward_every > 0),
    discount_percent INTEGER NOT NULL CHECK (discount_percent BETWEEN 1 AND 100)
);
CREATE TABLE IF NOT EXISTS products (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents >= 0),
    inventory INTEGER NOT NULL CHECK (inventory >= 0)
);
CREATE TABLE IF NOT EXISTS carts (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'checked_out')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS cart_items (
    cart_id TEXT NOT NULL REFERENCES carts(id),
    product_id TEXT NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL CHECK (quantity BETWEEN 1 AND 1000000),
    PRIMARY KEY (cart_id, product_id)
);
CREATE TABLE IF NOT EXISTS coupons (
    code TEXT PRIMARY KEY,
    milestone INTEGER NOT NULL UNIQUE CHECK (milestone > 0),
    discount_percent INTEGER NOT NULL CHECK (discount_percent BETWEEN 1 AND 100),
    redeemed_order_id TEXT UNIQUE REFERENCES orders(id),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    cart_id TEXT NOT NULL UNIQUE REFERENCES carts(id),
    idempotency_key TEXT NOT NULL UNIQUE,
    request_fingerprint TEXT NOT NULL,
    currency TEXT NOT NULL CHECK (currency = 'USD'),
    gross_cents INTEGER NOT NULL CHECK (gross_cents >= 0),
    discount_cents INTEGER NOT NULL CHECK (discount_cents BETWEEN 0 AND gross_cents),
    net_cents INTEGER NOT NULL CHECK (net_cents = gross_cents - discount_cents),
    coupon_code TEXT UNIQUE REFERENCES coupons(code),
    discount_percent INTEGER NOT NULL CHECK (discount_percent BETWEEN 0 AND 100),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS order_items (
    order_id TEXT NOT NULL REFERENCES orders(id),
    product_id TEXT NOT NULL REFERENCES products(id),
    product_name TEXT NOT NULL,
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents >= 0),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    line_total_cents INTEGER NOT NULL CHECK (line_total_cents = unit_price_cents * quantity),
    PRIMARY KEY (order_id, product_id)
);
