"""Start an isolated real server and verify concurrent requests over TCP.

Run: python -m scripts.smoke
Uses a temporary database and stops its server when finished.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import httpx


def run_smoke():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as directory:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = {
            **os.environ,
            "DATABASE_PATH": str(Path(directory) / "smoke.sqlite3"),
            "REWARD_EVERY": "2",
            "DISCOUNT_PERCENT": "10",
        }
        with open(Path(directory) / "server.log", "w+", encoding="utf-8") as log:
            server = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "scripts.smoke_server",
                    str(port),
                ],
                cwd=root,
                env=env,
                stdout=log,
                stderr=log,
                stdin=subprocess.PIPE,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            try:
                with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=20) as client:
                    deadline = time.monotonic() + 20
                    while True:
                        if server.poll() is not None:
                            log.seek(0)
                            raise RuntimeError("Server exited: " + log.read())
                        try:
                            if client.get("/health").status_code == 200:
                                break
                        except httpx.ConnectError:
                            pass
                        if time.monotonic() > deadline:
                            raise RuntimeError("Server did not become ready")
                        time.sleep(0.05)

                    def cart(product="p1"):
                        result = client.post("/carts")
                        assert result.status_code == 201
                        cart_id = result.json()["id"]
                        result = client.post(
                            f"/carts/{cart_id}/items",
                            json={
                                "product_id": product,
                                "quantity": 1,
                            },
                        )
                        assert result.status_code == 200
                        return cart_id

                    def place(cart_id, key, coupon=None):
                        return client.post(
                            f"/carts/{cart_id}/checkout",
                            json={
                                "coupon_code": coupon,
                            },
                            headers={"Idempotency-Key": key},
                        )

                    def compete(actions):
                        barrier = Barrier(len(actions))

                        def run(action):
                            barrier.wait(timeout=15)
                            return action()

                        with ThreadPoolExecutor(max_workers=len(actions)) as pool:
                            return list(pool.map(run, actions))

                    assert place(cart(), "seed-1").status_code == 201
                    assert place(cart(), "seed-2").status_code == 201
                    coupon_response = client.post("/admin/coupons")
                    assert coupon_response.status_code == 201
                    coupon = coupon_response.json()["code"]
                    carts = [cart() for _ in range(12)]
                    started = time.perf_counter()
                    responses = compete([lambda cid=cid: place(cid, cid, coupon) for cid in carts])
                    elapsed = time.perf_counter() - started
                    assert sorted(r.status_code for r in responses) == [201] + [409] * 11
                    winner_index = next(i for i, r in enumerate(responses) if r.status_code == 201)
                    winner = responses[winner_index].json()
                    winner_cart = carts[winner_index]
                    replays = compete(
                        [lambda: place(winner_cart, winner_cart, coupon) for _ in range(12)]
                    )
                    assert all(r.status_code == 200 and r.json() == winner for r in replays)
                    stock_carts = [cart("p5") for _ in range(12)]
                    stock_results = compete(
                        [lambda cid=cid: place(cid, cid) for cid in stock_carts]
                    )
                    assert sorted(r.status_code for r in stock_results) == [201] + [409] * 11
                    report = client.get("/admin/report").json()
                    orders = client.get("/admin/orders").json()
                    assert len(orders) == report["successful_orders"] == 4
                    for field in ("gross_cents", "discount_cents", "net_cents"):
                        assert report[field] == sum(order[field] for order in orders)
                    assert report["coupons"] == {"generated": 1, "available": 0, "redeemed": 1}
                    assert client.get("/admin/report").json() == report
                    assert client.get("/products").json()[-1]["inventory"] == 0
                    assert client.get("/docs").status_code == 200
                    schema = client.get("/openapi.json").json()
                    assert (
                        "201" in schema["paths"]["/carts/{cart_id}/checkout"]["post"]["responses"]
                    )
                    log.seek(0)
                    runtime_log = log.read()
                    assert "Application startup complete." in runtime_log
                    assert "method=POST route=/carts/{cart_id}/checkout status=201" in runtime_log
                    assert "method=POST route=/carts/{cart_id}/checkout status=200" in runtime_log
                    assert "Unhandled error" not in runtime_log
                    return {
                        "status": "passed",
                        "transport": "real HTTP/TCP",
                        "coupon_contenders": 12,
                        "coupon_redemptions": 1,
                        "successful_replays": 12,
                        "inventory_contenders": 12,
                        "limited_product_orders": 1,
                        "orders": 4,
                        "api_docs_and_request_logs": "verified",
                        "coupon_race_duration_ms": round(elapsed * 1000, 2),
                    }
            finally:
                try:
                    server.communicate(input="stop\n", timeout=10)
                except subprocess.TimeoutExpired:
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/PID", str(server.pid), "/T", "/F"],
                            capture_output=True,
                            check=False,
                        )
                    else:
                        server.kill()
                    server.wait(timeout=10)


if __name__ == "__main__":
    print(json.dumps(run_smoke(), indent=2))
