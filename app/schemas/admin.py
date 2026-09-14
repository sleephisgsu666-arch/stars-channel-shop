from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ChannelInput(StrictModel):
    telegram_chat_id: int = Field(lt=0, ge=-(2**63))
    title: str = Field(min_length=1, max_length=128)
    is_active: bool = True


class ProductInput(StrictModel):
    channel_id: UUID
    title: str = Field(min_length=1, max_length=128)
    short_description: str = Field(default="", max_length=500)
    description: str = Field(default="", max_length=3000)
    cover_file_id: str | None = Field(default=None, max_length=512)
    is_active: bool = False
    sort_order: int = Field(default=0, ge=-100000, le=100000)


class PlanInput(StrictModel):
    product_id: UUID
    name: str = Field(min_length=1, max_length=128)
    billing_type: Literal["free", "fixed", "lifetime"]
    price_stars: int = Field(ge=0, le=1000000)
    duration_days: int | None = Field(default=None, gt=0, le=36500)
    is_active: bool = True
    sort_order: int = Field(default=0, ge=-100000, le=100000)

    @model_validator(mode="after")
    def valid_plan(self):
        if self.billing_type == "free":
            valid = self.price_stars == 0
        elif self.billing_type == "fixed":
            valid = self.price_stars > 0 and self.duration_days is not None
        elif self.billing_type == "lifetime":
            valid = self.price_stars > 0 and self.duration_days is None
        else:
            valid = 0 < self.price_stars <= 10000 and self.duration_days == 30
        if not valid:
            raise ValueError("Invalid plan")
        return self


class AccessInput(StrictModel):
    telegram_user_id: int = Field(gt=0, le=2**63 - 1)
    product_id: UUID
    days: int | None = Field(default=None, gt=0, le=36500)
    revoke: bool = False
