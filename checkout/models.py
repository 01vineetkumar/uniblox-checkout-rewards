from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Quantity = Annotated[int, Field(strict=True, ge=1, le=1_000_000)]


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AddItem(RequestModel):
    product_id: Annotated[str, Field(strict=True, min_length=1, max_length=100)]
    quantity: Quantity


class UpdateItem(RequestModel):
    quantity: Quantity


class CheckoutRequest(RequestModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{}]})
    coupon_code: Annotated[str, Field(strict=True, pattern=r"^REWARD-[A-F0-9]{32}$")] | None = None


class Product(BaseModel):
    id: str
    name: str
    unit_price_cents: int
    inventory: int


class LineItem(BaseModel):
    product_id: str
    product_name: str
    unit_price_cents: int
    quantity: int
    line_total_cents: int


class CartLineItem(LineItem):
    inventory: int


class Cart(BaseModel):
    id: str
    status: Literal["open", "checked_out"]
    created_at: str
    currency: Literal["USD"]
    items: list[CartLineItem]
    subtotal_cents: int
    order_id: str | None


class Order(BaseModel):
    id: str
    cart_id: str
    currency: Literal["USD"]
    gross_cents: int
    discount_cents: int
    net_cents: int
    coupon_code: str | None
    discount_percent: int
    created_at: str
    items: list[LineItem]


class Coupon(BaseModel):
    code: str
    milestone: int
    discount_percent: int
    redeemed_order_id: str | None
    created_at: str


class CouponCounts(BaseModel):
    generated: int
    available: int
    redeemed: int


class ProductQuantity(BaseModel):
    product_id: str
    quantity: int


class Report(BaseModel):
    currency: Literal["USD"]
    successful_orders: int
    gross_cents: int
    discount_cents: int
    net_cents: int
    coupons: CouponCounts
    products: list[ProductQuantity]


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail
