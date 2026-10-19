import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from checkout.db import Database
from checkout.errors import DomainError
from checkout.models import (
    AddItem,
    Cart,
    CheckoutRequest,
    Coupon,
    ErrorResponse,
    Order,
    Product,
    Report,
    UpdateItem,
)
from checkout.service import Store

logger = logging.getLogger("checkout")


def create_app(db_path=None, reward_every=None, discount_percent=None):
    @asynccontextmanager
    async def lifespan(app):
        database = Database(
            db_path if db_path is not None else os.getenv("DATABASE_PATH", "data/store.sqlite3"),
            reward_every if reward_every is not None else int(os.getenv("REWARD_EVERY", "5")),
            discount_percent
            if discount_percent is not None
            else int(os.getenv("DISCOUNT_PERCENT", "10")),
        )
        app.state.store = Store(database)
        yield

    app = FastAPI(
        title="Checkout and Rewards API",
        version="1.0.0",
        lifespan=lifespan,
        description=(
            "All money is integer USD cents. Admin routes have no authentication in this demo."
        ),
        responses={
            404: {"model": ErrorResponse, "description": "Resource does not exist"},
            409: {"model": ErrorResponse, "description": "Conflict with current domain state"},
            422: {"model": ErrorResponse, "description": "Invalid request or coupon"},
            503: {"model": ErrorResponse, "description": "Database busy; retry later"},
            500: {"model": ErrorResponse, "description": "Unexpected internal error"},
        },
    )

    @app.middleware("http")
    async def request_log(request: Request, call_next):
        started = time.perf_counter()
        request_id = str(uuid.uuid4())
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("Unhandled error request_id=%s", request_id)
            response = JSONResponse(
                status_code=500,
                content={"error": {"code": "internal_error", "message": "Internal server error"}},
            )
        response.headers["X-Request-ID"] = request_id
        route = request.scope.get("route")
        logger.info(
            "request_id=%s method=%s route=%s status=%s duration_ms=%.2f",
            request_id,
            request.method,
            getattr(route, "path", "unmatched"),
            response.status_code,
            (time.perf_counter() - started) * 1000,
        )
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse(
            status_code=exc.status,
            content={"error": {"code": exc.code, "message": exc.message}},
            headers={"Retry-After": "1"} if exc.status == 503 else None,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "; ".join(
                        ".".join(str(part) for part in err["loc"]) + ": " + err["msg"]
                        for err in exc.errors()
                    ),
                }
            },
        )

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": "not_found" if exc.status_code == 404 else "http_error",
                    "message": str(exc.detail),
                }
            },
            headers=exc.headers,
        )

    @app.get("/health", tags=["health"])
    def health(request: Request):
        with request.app.state.store.db.transaction() as conn:
            conn.execute("SELECT 1").fetchone()
        return {"status": "ok"}

    @app.get("/products", response_model=list[Product], tags=["products"])
    def products(request: Request):
        return request.app.state.store.products()

    @app.post("/carts", response_model=Cart, status_code=201, tags=["carts"])
    def create_cart(request: Request):
        return request.app.state.store.create_cart()

    @app.get("/carts/{cart_id}", response_model=Cart, tags=["carts"])
    def cart(cart_id: str, request: Request):
        return request.app.state.store.cart(cart_id)

    @app.post("/carts/{cart_id}/items", response_model=Cart, tags=["carts"])
    def add_item(cart_id: str, body: AddItem, request: Request):
        """Add quantity to an existing line, or create a new line. Does not reserve stock."""
        return request.app.state.store.set_item(cart_id, body.product_id, body.quantity, add=True)

    @app.put("/carts/{cart_id}/items/{product_id}", response_model=Cart, tags=["carts"])
    def update_item(cart_id: str, product_id: str, body: UpdateItem, request: Request):
        """Replace quantity on an existing cart line. Use DELETE to remove it."""
        return request.app.state.store.set_item(cart_id, product_id, body.quantity)

    @app.delete("/carts/{cart_id}/items/{product_id}", response_model=Cart, tags=["carts"])
    def remove_item(cart_id: str, product_id: str, request: Request):
        return request.app.state.store.remove_item(cart_id, product_id)

    @app.post(
        "/carts/{cart_id}/checkout",
        response_model=Order,
        status_code=201,
        tags=["checkout"],
        responses={200: {"model": Order, "description": "Replay of a successful checkout"}},
    )
    def checkout(
        cart_id: str,
        body: CheckoutRequest,
        request: Request,
        response: Response,
        idempotency_key: Annotated[str, Header(pattern=r"^[A-Za-z0-9._:-]{1,128}$")],
    ):
        """Atomically place an order. Same key/cart/coupon replays the saved order.

        A key is globally scoped and retained with its order. Only successful operations bind it.
        Different keys cannot check out the same cart twice. Prices are resolved at checkout.
        """
        if len(request.headers.getlist("idempotency-key")) != 1:
            raise DomainError(422, "validation_error", "Supply exactly one Idempotency-Key header")
        order, replayed = request.app.state.store.checkout(
            cart_id, idempotency_key, body.coupon_code
        )
        response.status_code = 200 if replayed else 201
        response.headers["Idempotency-Replayed"] = str(replayed).lower()
        response.headers["Location"] = "/orders/" + order["id"]
        return order

    @app.get("/orders/{order_id}", response_model=Order, tags=["orders"])
    def order(order_id: str, request: Request):
        return request.app.state.store.order(order_id)

    @app.post("/admin/coupons", response_model=Coupon, status_code=201, tags=["admin"])
    def generate_coupon(request: Request):
        """ADMIN: generate one coupon for the oldest eligible unrewarded milestone."""
        return request.app.state.store.generate_coupon()

    @app.get("/admin/coupons", response_model=list[Coupon], tags=["admin"])
    def coupons(request: Request):
        return request.app.state.store.coupons()

    @app.get("/admin/orders", response_model=list[Order], tags=["admin"])
    def orders(request: Request):
        return request.app.state.store.orders()

    @app.get("/admin/report", response_model=Report, tags=["admin"])
    def report(request: Request):
        """ADMIN: read a consistent snapshot of successful orders and coupons."""
        return request.app.state.store.report()

    return app
