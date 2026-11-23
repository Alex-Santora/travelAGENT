"""Read-only travel providers. API keys and upstream error bodies never enter reports."""

import os
import time
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urlencode

import httpx
from openai import APIError

from .geography import distance_km
from .models import FX, Evidence, Flight, PlacesQuery, Query, Route, Stay
from .serp_flights import airport_suggestions, flight_offers


class Unavailable(ValueError):
    pass


def key(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise Unavailable(f"Configure {name} on the backend.")
    return value


def record(kind, name, provider, source=None, details=None, **fields):
    return Evidence(
        kind=kind,
        name=name,
        provider=provider,
        source=source,
        details=details or {},
        retrieved=datetime.now(UTC),
        **fields,
    )


def pick(data, *names):
    return {name: data[name] for name in names if data.get(name) is not None}


def within_search_area(point, args):
    return distance_km(args.center.model_dump(), point) <= args.radius_km


class Providers:
    def __init__(self, http, ai=None):
        self.http = http
        self.ai = ai

    def request(self, method, url, **kwargs):
        kwargs.setdefault("headers", {}).setdefault("User-Agent", "travelAGENT/2.0")
        for attempt in range(2):
            try:
                response = self.http.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                if method == "GET" and attempt == 0:
                    continue
                raise Unavailable(
                    f"Provider connection failed ({type(exc).__name__})."
                ) from None
            if response.status_code in (429, 503) and attempt == 0 and method == "GET":
                retry = response.headers.get("Retry-After", "1")
                if retry.isdigit() and int(retry) <= 3:
                    time.sleep(int(retry))
                    continue
                raise Unavailable(
                    f"Provider returned HTTP {response.status_code}. Retry later."
                )
            if not response.is_success:
                raise Unavailable(
                    f"Provider returned HTTP {response.status_code}. Check access, coverage and quota."
                )
            return response.json()

    def resolve_location(self, a):
        params = {"name": a.query, "count": 5, "language": "en"}
        url = "https://geocoding-api.open-meteo.com/v1/search"
        data = self.request("GET", url, params=params)
        results = data.get("results", [])
        if not results and "," in a.query:
            city, *qualifiers = [word.strip().casefold() for word in a.query.split(",")]
            params["name"] = city
            params["count"] = 20
            data = self.request("GET", url, params=params)
            results = [
                r
                for r in data.get("results", [])
                if all(
                    q
                    in {
                        str(r.get(k, "")).casefold()
                        for k in ("country", "country_code", "admin1", "admin2")
                    }
                    for q in qualifiers
                )
            ]
        return [
            record(
                "location",
                r["name"],
                "Open-Meteo / GeoNames",
                url + "?" + urlencode(params),
                pick(
                    r,
                    "latitude",
                    "longitude",
                    "country",
                    "country_code",
                    "admin1",
                    "timezone",
                    "id",
                ),
            )
            for r in results[:5]
        ]

    def duffel(self, method, path, **kwargs):
        token = key("DUFFEL_ACCESS_TOKEN")
        if not token.startswith("duffel_live_"):
            raise Unavailable(
                "Production inventory requires a live Duffel token. Test tokens are disabled."
            )
        return self.request(
            method,
            "https://api.duffel.com" + path,
            headers={
                "Authorization": "Bearer " + token,
                "Duffel-Version": "v2",
                "Accept": "application/json",
            },
            **kwargs,
        )["data"]

    def serpapi(self, engine, **params):
        data = self.request(
            "GET",
            "https://serpapi.com/search.json",
            params={"engine": engine, "api_key": key("SERPAPI_API_KEY"), **params},
        )
        if data.get("error"):
            if (
                data["error"]
                .casefold()
                .endswith("hasn't returned any results for this query.")
            ):
                return data
            raise Unavailable("SerpApi search failed. Check provider access and quota.")
        return data

    def search_airports(self, a):
        if os.getenv("SERPAPI_API_KEY", "").strip():
            return airport_suggestions(self.serpapi, a.query)
        rows = self.duffel("GET", "/places/suggestions", params={"query": a.query})
        return [
            record(
                "airport",
                p["name"],
                "Duffel Places",
                details=pick(
                    p,
                    "iata_code",
                    "iata_country_code",
                    "latitude",
                    "longitude",
                    "time_zone",
                    "city_name",
                    "type",
                )
                | {"airports": [x.get("iata_code") for x in p.get("airports") or []]},
            )
            for p in rows[:8]
        ]

    def search_flights(self, a):
        if os.getenv("SERPAPI_API_KEY", "").strip():
            return flight_offers(self.serpapi, a)
        body = {
            "slices": [s.model_dump(mode="json") for s in a.slices],
            "passengers": [{"type": "adult"} for _ in range(a.adults)],
        }
        data = self.duffel(
            "POST",
            "/air/offer_requests",
            params={"return_offers": "true", "supplier_timeout": 15000},
            json={"data": body},
        )
        if data.get("live_mode") is not True:
            raise Unavailable("Provider did not confirm live flight inventory.")
        rows = []
        for offer in data["offers"]:
            if offer.get("live_mode") is False:
                continue
            slices = []
            for s in offer["slices"]:
                segments = []
                for seg in s["segments"]:
                    segments.append(
                        pick(
                            seg,
                            "departing_at",
                            "arriving_at",
                            "duration",
                            "marketing_carrier_flight_number",
                        )
                        | {
                            "origin": pick(
                                seg["origin"],
                                "iata_code",
                                "time_zone",
                                "latitude",
                                "longitude",
                            ),
                            "destination": pick(
                                seg["destination"],
                                "iata_code",
                                "time_zone",
                                "latitude",
                                "longitude",
                            ),
                            "airline": seg["marketing_carrier"]["name"],
                            "baggage": [
                                p.get("baggages") for p in seg.get("passengers", [])
                            ],
                        }
                    )
                slices.append({"duration": s.get("duration"), "segments": segments})
            rows.append(
                record(
                    "flight",
                    offer["owner"]["name"]
                    + " · "
                    + " / ".join(s.origin + "–" + s.destination for s in a.slices),
                    "Duffel Flights",
                    details=pick(offer, "id", "expires_at", "conditions")
                    | {"slices": slices, "adults": a.adults},
                    amount=offer["total_amount"],
                    currency=offer["total_currency"],
                    basis="party total",
                    price_status="live quote",
                    availability="offered at retrieval",
                )
            )
        cheapest = sorted(rows, key=lambda r: (r.currency, r.amount))
        fewest = sorted(
            cheapest, key=lambda r: sum(len(s["segments"]) for s in r.details["slices"])
        )
        options = {r.details["id"]: r for r in cheapest[:4] + fewest[:2]}
        return list(options.values())

    def search_hotels(self, a):
        if os.getenv("SERPAPI_API_KEY", "").strip():
            return self.search_published_hotels(a)
        body = {
            "rooms": a.rooms,
            "guests": [{"type": "adult"} for _ in range(a.adults)],
            "check_in_date": str(a.check_in),
            "check_out_date": str(a.check_out),
            "location": {"radius": 8, "geographic_coordinates": a.center.model_dump()},
        }
        data = self.duffel("POST", "/stays/search", json={"data": body})
        rows = []
        for result in data["results"]:
            hotel = result["accommodation"]
            if result.get("live_mode") is False or data.get("live_mode") is False:
                raise Unavailable("Test stay results rejected.")
            rows.append(
                record(
                    "hotel",
                    hotel["name"],
                    "Duffel Stays",
                    hotel.get("website"),
                    a.model_dump(mode="json")
                    | pick(hotel, "location", "check_in_information", "rooms")
                    | pick(
                        result,
                        "expires_at",
                        "cheapest_rate_due_at_accommodation_amount",
                        "cheapest_rate_due_at_accommodation_currency",
                    )
                    | {
                        "search_result_id": result["id"],
                        "room_count": a.rooms,
                        "rooms": hotel.get("rooms") or [],
                    },
                    amount=result["cheapest_rate_total_amount"],
                    currency=result["cheapest_rate_currency"],
                    basis="party total",
                    price_status="live quote",
                    availability="available at search, cheapest rate",
                )
            )
        choices = sorted(rows, key=lambda r: (r.currency, r.amount))[:5]
        if choices:
            best = choices[0]
            ref = best.details["search_result_id"]
            try:
                rates = self.duffel(
                    "POST", f"/stays/search_results/{ref}/actions/fetch_all_rates"
                )
                best.details["rooms"] = rates["accommodation"].get("rooms", [])
            except Unavailable as exc:
                best.details["rate_details_unavailable"] = str(exc)
        return choices

    def search_published_hotels(self, a):
        # Google Hotels search is an alternative for users without Duffel Stays approval.
        if a.rooms != 1:
            raise Unavailable("Published hotel comparison supports a single room.")
        data = self.serpapi(
            "google_hotels",
            q=f"Hotels near {a.center.latitude}, {a.center.longitude}",
            check_in_date=str(a.check_in),
            check_out_date=str(a.check_out),
            adults=a.adults,
            currency="USD",
        )
        rows = []
        for p in data.get("properties", [])[:5]:
            price = (p.get("total_rate") or {}).get("extracted_lowest")
            rows.append(
                record(
                    "hotel",
                    p["name"],
                    "Google Hotels via SerpApi",
                    p.get("link"),
                    a.model_dump(mode="json")
                    | pick(
                        p,
                        "gps_coordinates",
                        "total_rate",
                        "check_in_time",
                        "check_out_time",
                    )
                    | pick(data.get("search_metadata", {}), "created_at"),
                    amount=price,
                    currency="USD" if price is not None else None,
                    basis="party total",
                    price_status="published" if price is not None else "unavailable",
                    availability="Availability and final price require confirmation with the seller",
                )
            )
        return rows

    def search_places(self, a):
        if not os.getenv("GOOGLE_MAPS_API_KEY"):
            params = {
                "lat": a.center.latitude,
                "lon": a.center.longitude,
                "limit": 6,
                "lang": "en",
            }
            if a.osm_tag:
                params.update(osm_tag=a.osm_tag, radius=min(50, a.radius_km))
                url = "https://photon.komoot.io/reverse"
            else:
                params["q"] = a.query
                url = "https://photon.komoot.io/api/"
            data = self.request("GET", url, params=params)
            rows = []
            for f in data.get("features", []):
                props = f["properties"]
                osm_type = {"N": "node", "W": "way", "R": "relation"}.get(
                    props.get("osm_type")
                )
                if (
                    not props.get("name")
                    or not osm_type
                    or not props.get("osm_id")
                    or props.get("osm_key") in ("place", "boundary")
                ):
                    continue
                lon, lat = f["geometry"]["coordinates"]
                if not within_search_area({"latitude": lat, "longitude": lon}, a):
                    continue
                rows.append(
                    record(
                        "place",
                        props["name"],
                        "Photon / OpenStreetMap",
                        f"https://www.openstreetmap.org/{osm_type}/{props['osm_id']}",
                        pick(
                            props,
                            "country",
                            "state",
                            "city",
                            "street",
                            "osm_key",
                            "osm_value",
                        )
                        | {"location": {"latitude": lat, "longitude": lon}},
                    )
                )
            return rows
        url = "https://places.googleapis.com/v1/places:searchText"
        mask = "id,displayName,primaryType,formattedAddress,location,websiteUri,googleMapsUri,regularOpeningHours,timeZone,businessStatus,reservable"
        data = self.request(
            "POST",
            url,
            headers={
                "X-Goog-Api-Key": key("GOOGLE_MAPS_API_KEY"),
                "X-Goog-FieldMask": ",".join("places." + v for v in mask.split(",")),
            },
            json={
                "textQuery": a.query,
                "pageSize": 5,
                "locationBias": {
                    "circle": {
                        "center": a.center.model_dump(),
                        "radius": min(50000, a.radius_km * 1000),
                    }
                },
            },
        )
        return [
            record(
                "place",
                p["displayName"]["text"],
                "Google Maps",
                p.get("websiteUri") or p.get("googleMapsUri"),
                pick(p, *mask.split(",")),
            )
            for p in data.get("places", [])
            if p.get("location") and within_search_area(p["location"], a)
        ]

    def get_route(self, a):
        if not os.getenv("GOOGLE_MAPS_API_KEY") and a.mode in ("DRIVE", "WALK"):
            coords = ";".join(
                f"{p.longitude},{p.latitude}" for p in [a.origin, a.destination]
            )
            base = (
                "https://router.project-osrm.org"
                if a.mode == "DRIVE"
                else "https://routing.openstreetmap.de/routed-foot"
            )
            url = base + "/route/v1/driving/" + coords
            data = self.request("GET", url, params={"overview": "false"})
            return [
                record(
                    "route",
                    a.mode.title() + " route",
                    "OSRM / OpenStreetMap",
                    url,
                    a.model_dump(mode="json")
                    | {"seconds": r["duration"], "meters": r["distance"]},
                )
                for r in data.get("routes", [])[:1]
            ]
        body = {
            "origin": {"location": {"latLng": a.origin.model_dump()}},
            "destination": {"location": {"latLng": a.destination.model_dump()}},
            "travelMode": a.mode,
        }
        if a.departure and a.mode != "WALK":
            body["departureTime"] = a.departure.isoformat()
        url = "https://routes.googleapis.com/directions/v2:computeRoutes"
        data = self.request(
            "POST",
            url,
            json=body,
            headers={
                "X-Goog-Api-Key": key("GOOGLE_MAPS_API_KEY"),
                "X-Goog-FieldMask": "routes.duration,routes.distanceMeters,routes.travelAdvisory.transitFare,routes.legs.steps.transitDetails",
            },
        )
        rows = []
        for r in data.get("routes", [])[:3]:
            fare = (r.get("travelAdvisory") or {}).get("transitFare")
            amount = (
                Decimal(fare.get("units", 0)) + Decimal(fare.get("nanos", 0)) / 10**9
                if fare
                else None
            )
            details = a.model_dump(mode="json") | {
                "seconds": float(r["duration"][:-1]),
                "meters": r["distanceMeters"],
                "transit": [
                    step["transitDetails"]
                    for leg in r.get("legs", [])
                    for step in leg.get("steps", [])
                    if "transitDetails" in step
                ],
            }
            rows.append(
                record(
                    "route",
                    a.mode.title() + " route",
                    "Google Maps Routes",
                    details=details,
                    amount=amount,
                    currency=fare["currencyCode"] if fare else None,
                    basis="per traveler" if fare else "unknown",
                    price_status="published" if fare else "unavailable",
                )
            )
        return rows

    def search_web(self, a):
        if os.getenv("SERPAPI_API_KEY"):
            data = self.request(
                "GET",
                "https://serpapi.com/search.json",
                params={
                    "engine": "google",
                    "q": a.query,
                    "api_key": key("SERPAPI_API_KEY"),
                },
            )
            if data.get("error"):
                raise Unavailable("Web search provider reported an error.")
            return [
                record(
                    "web",
                    p["title"],
                    "Google via SerpApi",
                    p["link"],
                    pick(p, "snippet", "date") | {"query": a.query},
                )
                for p in data.get("organic_results", [])[:5]
            ]
        if self.ai is None:
            raise Unavailable(
                "Configure SERPAPI_API_KEY or OPENAI_API_KEY for web research."
            )
        try:
            result = self.ai.responses.create(
                model=os.getenv("OPENAI_SEARCH_MODEL", "gpt-4.1-mini"),
                tools=[{"type": "web_search"}],
                tool_choice="required",
                input="Find official sources for this travel query. Give concise cited findings. "
                + a.query,
            )
        except APIError as exc:
            raise Unavailable(
                f"OpenAI web search failed ({getattr(exc, 'status_code', None) or 'connection'})."
            ) from None
        rows = []
        if not any(x.type == "web_search_call" for x in result.output):
            raise Unavailable("No web search was executed.")
        for msg in result.output:
            for part in getattr(msg, "content", []) or []:
                for cite in getattr(part, "annotations", []) or []:
                    if cite.type == "url_citation" and all(
                        r.source != cite.url for r in rows
                    ):
                        rows.append(
                            record(
                                "web",
                                cite.title or cite.url,
                                "OpenAI web search",
                                cite.url,
                                {
                                    "research_summary": part.text[
                                        max(
                                            0,
                                            part.text.rfind("\n", 0, cite.start_index)
                                            + 1,
                                        ) : cite.start_index
                                    ][-1200:]
                                    or part.text[:400],
                                    "publication_date": "unverified",
                                    "query": a.query,
                                },
                            )
                        )
        return rows[:5]

    def currency_rate(self, a):
        url = "https://api.frankfurter.dev/v1/latest"
        params = {"base": a.base, "symbols": a.target}
        data = self.request("GET", url, params=params)
        return [
            record(
                "fx",
                a.base + "/" + a.target,
                "Frankfurter",
                url + "?" + urlencode(params),
                {
                    "base": a.base,
                    "target": a.target,
                    "rate": data["rates"][a.target],
                    "date": data["date"],
                },
            )
        ]


TOOLS = {
    "resolve_location": (
        Query,
        "Find matching cities, countries and geographic coordinates. Disambiguate by region.",
    ),
    "search_airports": (Query, "Find airports and metropolitan IATA codes for a city."),
    "search_flights": (
        Flight,
        "Search date-specific adult fares. Include all slices in one search for a round trip or multi-city trip. SerpApi returns published prices.",
    ),
    "search_hotels": (
        Stay,
        "Search hotels for dates, coordinates and occupancy. SerpApi returns published prices for one room.",
    ),
    "search_published_hotels": (
        Stay,
        "Compare dated published hotel prices via SerpApi when live inventory is unavailable.",
    ),
    "search_places": (
        PlacesQuery,
        "Find named attractions, restaurants or requested activities with location and usual hours.",
    ),
    "get_route": (
        Route,
        "Look up actual travel duration and available transit details. Supply verified endpoint coordinates.",
    ),
    "search_web": (
        Query,
        "Research official operator timetables, venue websites, events, and open-ended destinations. Include dates and location.",
    ),
    "currency_rate": (
        FX,
        "Get a published exchange rate to convert party costs into the budget currency.",
    ),
}
