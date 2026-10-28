"""Validated input shapes. Everything from the browser or the model is parsed through these."""
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_QTY = 20
MAX_LINES = 30
PHONE_RE = re.compile(r"^\+?[0-9][0-9 \-]{6,18}[0-9]$")


class MenuItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=300)
    category: str = Field(min_length=1, max_length=40)
    price_minor: int = Field(ge=0, le=10_000_000)
    available: bool = True
    options: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("name", "category", "description")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("options")
    @classmethod
    def _options(cls, v: list[str]) -> list[str]:
        cleaned = [o.strip() for o in v if o.strip()]
        if any(len(o) > 40 for o in cleaned):
            raise ValueError("each option must be 40 characters or fewer")
        return cleaned


class AvailabilityIn(BaseModel):
    available: bool


class OrderLineIn(BaseModel):
    item_id: str = Field(min_length=1, max_length=64)
    quantity: int = Field(ge=1, le=MAX_QTY)
    customizations: list[str] = Field(default_factory=list, max_length=5)


class OrderItemsIn(BaseModel):
    items: list[OrderLineIn] = Field(min_length=1, max_length=MAX_LINES)


class CustomerIn(BaseModel):
    customer_name: str = Field(min_length=1, max_length=80)
    phone: str
    fulfillment: Literal["pickup", "delivery"]
    delivery_address: str | None = Field(default=None, max_length=300)

    @field_validator("customer_name")
    @classmethod
    def _name(cls, v: str) -> str:
        return v.strip()

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        v = v.strip()
        if not PHONE_RE.match(v):
            raise ValueError("phone must be 8-20 digits, optionally starting with +")
        return v

    @field_validator("delivery_address")
    @classmethod
    def _addr(cls, v: str | None) -> str | None:
        return v.strip() or None if v else None


class LoginIn(BaseModel):
    restaurant_id: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


RESTAURANT_ID_RE = r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$"


class RestaurantCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=RESTAURANT_ID_RE)
    name: str = Field(min_length=1, max_length=80)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    delivery_fee_minor: int = Field(default=0, ge=0, le=10_000_000)
    accepts_pickup: bool = True
    accepts_delivery: bool = True
    owner_password: str = Field(min_length=8, max_length=200)

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v


class RestaurantUpdateIn(BaseModel):
    """Only these fields are editable. Anything else (_id, owner_password_hash, status, currency...) is a 422.
    Currency is fixed after creation: changing it would silently re-label every stored price."""
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=80)
    delivery_fee_minor: int | None = Field(default=None, ge=0, le=10_000_000)
    accepts_pickup: bool | None = None
    accepts_delivery: bool | None = None

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v


class RestaurantDeleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm_id: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


class AdminLoginIn(BaseModel):
    password: str = Field(min_length=1, max_length=200)


class CallSessionIn(BaseModel):
    sdp: str = Field(min_length=10, max_length=65536)
    client_call_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    restaurant_id: str | None = Field(default=None, max_length=64)

    @field_validator("sdp")
    @classmethod
    def _sdp(cls, v: str) -> str:
        if not v.lstrip().startswith("v=0"):
            raise ValueError("not an SDP offer")
        return v


class CallRefIn(BaseModel):
    client_call_id: str = Field(pattern=r"^[0-9a-f]{32}$")
