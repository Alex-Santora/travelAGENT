from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


Mode = Literal["bus", "rail", "air"]
City = Literal["New York City", "Boston", "Philadelphia"]
Point = tuple[int, int]


class Trip(Model):
    origin: City
    destination: City
    departure: date
    returning: date
    budget_cents: int = Field(gt=0, le=100_000_000, strict=True)
    travelers: int = Field(default=1, ge=1, le=8, strict=True)
    allowed_modes: list[Mode] = Field(
        default_factory=lambda: ["air", "bus", "rail"], min_length=1, max_length=3
    )
    preferred_mode: Mode | None = None
    lodging: Literal["central", "value"] | None = None
    interests: list[Literal["history", "art", "outdoors", "science"]] = Field(
        default_factory=list
    )
    priority: Literal["balanced", "cost", "time"] = "balanced"

    @model_validator(mode="after")
    def validate_route(self):
        if self.origin != "New York City" or self.destination == self.origin:
            raise ValueError("Choose New York City to Boston or Philadelphia.")
        if not 0 <= self.nights <= 13:
            raise ValueError("Return must be within 0 to 13 nights after departure.")
        return self

    @property
    def nights(self):
        return (self.returning - self.departure).days


class Source(Model):
    source: str = "Simulated corridor catalog v1"
    reliability: Literal["simulated"] = "simulated"


class Transport(Source):
    mode: Mode
    provider: str
    origin: City
    destination: City
    terminal: str
    location: Point
    outbound: tuple[datetime, datetime]
    inbound: tuple[datetime, datetime]
    fare_each_way_cents: int
    party_roundtrip_cents: int
    access_minutes: int
    access_cents_pp: int
    checkin_minutes: int
    restrictions: str

    @computed_field
    @property
    def roundtrip_ride_minutes(self) -> int:
        return sum(
            (end - start).seconds // 60 for start, end in (self.outbound, self.inbound)
        )


class Hotel(Source):
    name: str
    style: Literal["central", "value"]
    location: Point
    district: str
    nightly_cents: int
    rooms: int
    nights: int
    room_total_cents: int
    tax_cents: int


class Activity(Source):
    name: str
    district: str
    location: Point
    interest: str
    duration: int
    admission_cents: int
    opens: int
    closes: int


class Event(Model):
    start: datetime
    end: datetime
    title: str
    category: str
    cents: int = 0


class Costs(Model):
    primary: int
    accommodation: int
    transfers: int
    local: int
    activities: int
    meals: int
    fees: int

    @property
    def total(self):
        return sum(self.model_dump().values())


class Candidate(Model):
    transport: Transport
    hotel: Hotel
    costs: Costs
    total_cents: int
    remaining_cents: int
    travel_minutes: int
    destination_minutes: int
    visits: int
    interest_matches: int
    score: int
    feasible: bool
    events: list[Event]


class Comparison(Model):
    mode: Mode
    provider: str
    hotel: str
    costs: Costs
    total_cents: int
    remaining_cents: int
    travel_minutes: int
    destination_minutes: int
    visits: int
    interest_matches: int
    score: int
    feasible: bool


class Execution(Model):
    tool: str
    source: str
    inputs: dict
    output: dict | list | None = None
    error: str | None = None


class Report(Model):
    request: Trip
    assumptions: list[str]
    trace: list[Execution]
    selected: Candidate
    alternatives: list[Comparison]
    explanation: str
