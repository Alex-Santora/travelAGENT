"""Translate Google Flights search results into the planner's flight evidence."""

import json
from datetime import UTC, datetime
from functools import cache
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import airportsdata

from .models import Evidence

PROVIDER = "Google Flights via SerpApi"


@cache
def airport_data():
    return airportsdata.load("IATA")


def airport_details(code):
    row = airport_data().get(code)
    if not row or not row["tz"]:
        return {"iata_code": code}
    return {
        "iata_code": code,
        "iata_country_code": row["country"],
        "time_zone": row["tz"],
        "latitude": row["lat"],
        "longitude": row["lon"],
    }


def airport_suggestions(search, query):
    data = search("google_flights_autocomplete", q=query, exclude_regions="true")
    rows = {}
    for city in data.get("suggestions", []):
        for airport in city.get("airports", []):
            code = airport["id"]
            rows.setdefault(
                code,
                Evidence(
                    kind="airport",
                    name=airport["name"],
                    provider=PROVIDER,
                    source="https://www.google.com/travel/flights",
                    retrieved=datetime.now(UTC),
                    details=airport_details(code)
                    | {
                        "city_name": airport.get("city") or city["name"],
                        "type": "airport",
                    },
                ),
            )
    return list(rows.values())[:8]


def flight_parameters(request):
    first, *remaining = request.slices
    params = {"adults": request.adults, "currency": "USD", "hl": "en"}
    if remaining and (
        len(remaining) > 1
        or first.origin != remaining[0].destination
        or first.destination != remaining[0].origin
    ):
        return params | {
            "type": 3,
            "multi_city_json": json.dumps(
                [
                    {
                        "departure_id": leg.origin,
                        "arrival_id": leg.destination,
                        "date": str(leg.departure_date),
                    }
                    for leg in request.slices
                ]
            ),
        }
    params.update(
        type=1 if remaining else 2,
        departure_id=first.origin,
        arrival_id=first.destination,
        outbound_date=str(first.departure_date),
    )
    if remaining:
        params["return_date"] = str(remaining[0].departure_date)
    return params


def flight_options(data):
    return (data.get("best_flights") or []) + (data.get("other_flights") or [])


def normalize_leg(option):
    segments = []
    for flight in option["flights"]:
        departure, arrival = flight["departure_airport"], flight["arrival_airport"]
        origin = airport_details(departure["id"])
        destination = airport_details(arrival["id"])
        start = datetime.fromisoformat(departure["time"]).replace(
            tzinfo=ZoneInfo(origin["time_zone"])
        )
        end = datetime.fromisoformat(arrival["time"]).replace(
            tzinfo=ZoneInfo(destination["time_zone"])
        )
        if end.timestamp() <= start.timestamp():
            raise ValueError("Flight times are inconsistent.")
        segments.append(
            {
                "origin": origin,
                "destination": destination,
                "departing_at": start.isoformat(),
                "arriving_at": end.isoformat(),
                "airline": flight["airline"],
                "marketing_carrier_flight_number": flight.get("flight_number", ""),
                "duration_minutes": flight.get("duration"),
            }
        )
    if not segments:
        raise ValueError("No flight segments.")
    return {"segments": segments, "duration_minutes": option.get("total_duration")}


def flight_offers(search, request):
    params = flight_parameters(request)
    data = search("google_flights", **params)
    candidates = [(data, [])]
    legs = len(request.slices)
    for _ in range(legs - 1):
        next_candidates = []
        for response, previous in candidates:
            # Compare two outbound choices for a round trip. Bound multi-city to one path.
            tokens = {}
            limit = 2 if params["type"] == 1 else 1
            for option in flight_options(response):
                if token := option.get("departure_token"):
                    tokens.setdefault(token, option)
            for token, option in list(tokens.items())[:limit]:
                try:
                    following = search(
                        "google_flights", **params, departure_token=token
                    )
                except ValueError:
                    if not any(flight_options(row) for row, _ in next_candidates):
                        raise
                    break
                next_candidates.append((following, [*previous, option]))
        candidates = next_candidates

    offers = []
    seen = set()
    for response, previous in candidates:
        metadata = response.get("search_metadata", {})
        url = metadata.get("google_flights_url") or data.get("search_metadata", {}).get(
            "google_flights_url"
        )
        if url and not (
            urlsplit(url).scheme == "https"
            and urlsplit(url).hostname == "www.google.com"
            and urlsplit(url).path.startswith("/travel/flights")
        ):
            url = None
        for option in flight_options(response):
            try:
                slices = [normalize_leg(leg) for leg in [*previous, option]]
            except (KeyError, ValueError, TypeError, ZoneInfoNotFoundError):
                continue
            signature = tuple(
                (
                    segment["marketing_carrier_flight_number"],
                    segment["origin"]["iata_code"],
                    segment["destination"]["iata_code"],
                    segment["departing_at"],
                    segment["arriving_at"],
                )
                for leg in slices
                for segment in leg["segments"]
            )
            price = option.get("price")
            if (signature, price) in seen:
                continue
            seen.add((signature, price))
            endpoints = " / ".join(
                leg["segments"][0]["origin"]["iata_code"]
                + "–"
                + leg["segments"][-1]["destination"]["iata_code"]
                for leg in slices
            )
            offers.append(
                Evidence(
                    kind="flight",
                    name=slices[0]["segments"][0]["airline"] + " · " + endpoints,
                    provider=PROVIDER,
                    source=url,
                    retrieved=datetime.now(UTC),
                    details={
                        "slices": slices,
                        "adults": request.adults,
                        "search_created_at": metadata.get("created_at"),
                    },
                    amount=price,
                    currency="USD" if price is not None else None,
                    basis="party total",
                    price_status="published" if price is not None else "unavailable",
                    availability="Availability and final price require confirmation with the seller",
                )
            )
    return sorted(offers, key=lambda row: (row.amount is None, row.amount))[:6]
