from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import Field

from .models import City, Mode, Model, Trip


class Fields(Model):
    origin: City | None = None
    destination: City | None = None
    departure: date | None = None
    returning: date | None = None
    days: int | None = Field(default=None, ge=1, le=14)
    budget_usd: Decimal | None = Field(
        default=None, gt=0, le=1_000_000, decimal_places=2
    )
    travelers: int | None = Field(default=None, ge=1, le=8, strict=True)
    allowed_modes: list[Mode] | None = Field(default=None, min_length=1, max_length=3)
    preferred_mode: Mode | None = None
    lodging: Literal["central", "value"] | None = None
    interests: list[Literal["art", "science", "outdoors", "history"]] | None = None
    priority: Literal["balanced", "cost", "time"] | None = None


class Submission(Model):
    prompt: str = Field(min_length=1, max_length=3000)
    overrides: Fields = Field(default_factory=Fields)


def resolve(fields, overrides):
    data = fields.model_dump(exclude_none=True) | overrides.model_dump(
        exclude_none=True
    )
    notes = [
        "Simulated daily service, prices and attraction hours. All times are local Eastern time.",
        "Meals budgeted at $35 per traveler per day. Rooms sleep two. Simulated lodging tax is 12%.",
        "Transfers start and end in central New York City. Hotel luggage storage assumed on travel days.",
    ]
    missing = [
        key
        for key in ("origin", "destination", "departure", "travelers", "budget_usd")
        if key not in data
    ]
    days = data.pop("days", None)
    if "returning" not in data and days is None:
        missing.append("return date or duration")
    if missing:
        raise ValueError(
            "Please provide " + ", ".join(missing).replace("_usd", " in USD") + "."
        )
    if "returning" not in data:
        try:
            data["returning"] = data["departure"] + timedelta(days=days - 1)
        except OverflowError as exc:
            raise ValueError("The return date exceeds the supported calendar.") from exc
    elif days and (data["returning"] - data["departure"]).days + 1 != days:
        raise ValueError("Requested duration conflicts with the travel dates.")
    data["budget_cents"] = int(Decimal(data.pop("budget_usd")) * 100)
    return Trip(**data), notes
