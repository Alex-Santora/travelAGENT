from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


Text = Annotated[str, Field(min_length=1, max_length=350)]
Currency = Annotated[str, Field(pattern="^[A-Z]{3}$")]
Count = Annotated[int, Field(ge=1, le=9)]


class Query(Model):
    query: Text


class FX(Model):
    base: Currency
    target: Currency


class Point(Model):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class PlacesQuery(Query):
    osm_tag: str | None = Field(
        None,
        pattern="^[a-z_]+:[a-z_]+$",
        description="For category discovery use amenity:restaurant, tourism:museum, leisure:golf_course or another appropriate OSM tag. Omit for an exact named venue.",
    )
    center: Point = Field(description="Resolved city or search-area coordinates.")
    radius_km: float = Field(40, gt=0, le=100)


class Route(Model):
    origin: Point
    destination: Point
    mode: Literal["DRIVE", "WALK", "TRANSIT"]
    departure: AwareDatetime | None = None


class Slice(Model):
    origin: Annotated[str, Field(pattern="^[A-Z]{3}$")]
    destination: Annotated[str, Field(pattern="^[A-Z]{3}$")]
    departure_date: date


class Flight(Model):
    slices: list[Slice] = Field(min_length=1, max_length=6)
    adults: Count


class Stay(Model):
    center: Point
    check_in: date
    check_out: date
    adults: Count
    rooms: Count = 1

    @model_validator(mode="after")
    def dates(self):
        if self.check_in >= self.check_out or self.rooms > self.adults:
            raise ValueError("Check the stay dates and room occupancy")
        return self


class Trip(Model):
    origin: Text
    destinations: list[Text] = Field(
        max_length=20,
        description="Only destinations to visit on THIS itinerary, in visit order. Exclude alternative destinations considered.",
    )
    open_destination: bool = Field(
        False, description="User delegated the choice of destination."
    )
    duration_days: int | None = Field(
        None,
        ge=1,
        description="Inclusive trip length if the traveler specified a number of days.",
    )
    flexible_dates: bool = Field(
        False,
        description="Exact travel dates are proposed within the traveler's flexible window.",
    )
    start: date
    end: date
    adults: Count
    currency: Currency = "USD"
    budget: Decimal | None = Field(None, gt=0)
    daily_visits: bool = Field(
        False,
        description="Traveler wants a day-by-day sightseeing or activities itinerary.",
    )
    requirements: list[Text] = Field(default_factory=list, max_length=40)
    assumptions: list[Text] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def dates(self):
        if (
            self.duration_days
            and (self.end - self.start).days + 1 != self.duration_days
        ):
            if not self.flexible_dates:
                raise ValueError("Date range must match requested inclusive duration")
            try:
                self.end = self.start + timedelta(days=self.duration_days - 1)
            except OverflowError as exc:
                raise ValueError("Duration exceeds the supported calendar") from exc
        if self.end < self.start:
            raise ValueError("Return precedes departure")
        return self


class Interpretation(Trip):
    travel_window_quote: str | None = Field(
        None,
        description="Exact verbatim words from the traveler specifying when to travel (date, month, relative window). Null if missing. Never supply your own date as evidence.",
    )
    research_queries: list[Text] = Field(
        default_factory=list,
        max_length=3,
        description="Up to three targeted web searches to execute immediately. Include named-venue discovery for mandatory activities, official intercity timetables, or climate/destination discovery for open requests. Use [] if none are needed.",
    )


class Evidence(Model):
    kind: Literal[
        "location", "airport", "flight", "hotel", "route", "place", "web", "fx"
    ]
    name: str
    provider: str
    source: str | None = None
    retrieved: AwareDatetime
    details: dict = Field(default_factory=dict)
    amount: Decimal | None = Field(None, ge=0, allow_inf_nan=False)
    currency: Currency | None = None
    basis: Literal["party total", "per traveler", "unknown"] = "unknown"
    price_status: Literal["live quote", "published", "estimate", "unavailable"] = (
        "unavailable"
    )
    availability: str = "unverified"


class Slot(Model):
    web_activity: Text | None = Field(
        None,
        description="For a web-only visit, give the specific venue or service name verbatim from the cited result. General research articles stay in the source list.",
    )
    evidence: Text
    start: AwareDatetime
    end: AwareDatetime
    slice_index: int = Field(0, ge=0, le=5)


class Break(Model):
    kind: Literal["break", "free time"] = "break"
    start: AwareDatetime
    end: AwareDatetime


class Candidate(Model):
    breaks: list[Break] = Field(default_factory=list, max_length=100)
    items: list[Slot] = Field(max_length=350)
    gaps: list[Text] = Field(default_factory=list, max_length=60)


class Finish(Model):
    trip: Trip
    candidates: list[Candidate] = Field(min_length=1, max_length=4)
    priority: Literal["cost", "time", "balanced"] = "balanced"


class Question(Model):
    missing: Literal[
        "origin", "travel window", "conflicting constraints", "ambiguous place", "party"
    ]
    question: Text
