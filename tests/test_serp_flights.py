import json

import httpx
import pytest

from travel.catalog import Providers, Unavailable
from travel.models import Candidate, Flight, Query, Stay, Trip
from travel.optimize import evaluate


def segment(origin, destination, departure, arrival, number):
    return {
        "departure_airport": {"id": origin, "time": departure},
        "arrival_airport": {"id": destination, "time": arrival},
        "airline": "Test Air",
        "flight_number": number,
    }


@pytest.fixture
def serp_credentials(monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", "fixture-secret")
    monkeypatch.delenv("DUFFEL_ACCESS_TOKEN", raising=False)


def test_serpapi_only_round_trip_and_hotel_budget(serp_credentials):
    engines = []
    outbound = segment("JFK", "LHR", "2027-03-18 22:00", "2027-03-19 09:00", "TA 123")
    inbound = segment("LHR", "JFK", "2027-03-22 12:00", "2027-03-22 15:30", "TA 321")
    search_url = "https://www.google.com/travel/flights?tfs=selected-outbound"

    def handle(request):
        assert request.url.host == "serpapi.com"
        params = request.url.params
        assert params["api_key"] == "fixture-secret"
        engines.append(params["engine"])
        if params["engine"] == "google_flights_autocomplete":
            assert params["q"] == "New York"
            return httpx.Response(
                200,
                json={
                    "suggestions": [
                        {
                            "name": "New York",
                            "airports": [
                                {
                                    "id": "JFK",
                                    "name": "John F. Kennedy International Airport",
                                }
                            ],
                        }
                    ]
                },
            )
        if params["engine"] == "google_hotels":
            assert params["check_in_date"] == "2027-03-19"
            return httpx.Response(
                200,
                json={
                    "properties": [
                        {
                            "name": "London hotel",
                            "link": "https://example.com/london",
                            "total_rate": {"extracted_lowest": 425},
                        }
                    ]
                },
            )
        assert params["type"] == "1"
        assert params["departure_id"] == "JFK" and params["arrival_id"] == "LHR"
        assert params["outbound_date"] == "2027-03-18"
        assert params["return_date"] == "2027-03-22"
        assert params["adults"] == "2"
        if "departure_token" not in params:
            return httpx.Response(
                200,
                json={
                    "best_flights": [
                        {
                            "flights": [outbound],
                            "price": 500,
                            "departure_token": "outbound-choice",
                        }
                    ]
                },
            )
        assert params["departure_token"] == "outbound-choice"
        return httpx.Response(
            200,
            json={
                "search_metadata": {"google_flights_url": search_url},
                "other_flights": [{"flights": [inbound], "price": 620}],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        service = Providers(http)
        airport = service.search_airports(Query(query="New York"))[0]
        assert airport.details["iata_code"] == "JFK"
        flights = service.search_flights(
            Flight(
                adults=2,
                slices=[
                    {
                        "origin": "JFK",
                        "destination": "LHR",
                        "departure_date": "2027-03-18",
                    },
                    {
                        "origin": "LHR",
                        "destination": "JFK",
                        "departure_date": "2027-03-22",
                    },
                ],
            )
        )
        hotels = service.search_hotels(
            Stay(
                center={"latitude": 51.5, "longitude": -0.12},
                check_in="2027-03-19",
                check_out="2027-03-22",
                adults=2,
            )
        )

    (flight,) = flights
    (hotel,) = hotels
    assert engines == [
        "google_flights_autocomplete",
        "google_flights",
        "google_flights",
        "google_hotels",
    ]
    assert flight.amount == 620 and flight.source == search_url
    assert flight.price_status == hotel.price_status == "published"
    assert "fixture-secret" not in flight.model_dump_json()
    candidate = Candidate(
        items=[
            {
                "evidence": "flight",
                "slice_index": 0,
                "start": "2027-03-18T22:00-04:00",
                "end": "2027-03-19T09:00+00:00",
            },
            {
                "evidence": "hotel",
                "start": "2027-03-19T15:00+00:00",
                "end": "2027-03-22T09:00+00:00",
            },
            {
                "evidence": "flight",
                "slice_index": 1,
                "start": "2027-03-22T12:00+00:00",
                "end": "2027-03-22T15:30-04:00",
            },
        ]
    )
    trip = Trip(
        origin="New York",
        destinations=["London"],
        start="2027-03-18",
        end="2027-03-22",
        adults=2,
    )
    report = evaluate(candidate, trip, {"flight": flight, "hotel": hotel})
    assert report["known_total"] == "1045.00"
    assert len(report["costs"]) == 2
    assert report["items"][-1]["end"] == "2027-03-22T15:30:00-04:00"
    assert not report["issues"]


def test_serpapi_open_jaw_uses_multi_city_tokens(serp_credentials):
    calls = []

    def handle(request):
        params = request.url.params
        assert params["type"] == "3" and "return_date" not in params
        legs = json.loads(params["multi_city_json"])
        assert [leg["departure_id"] for leg in legs] == ["JFK", "CDG"]
        calls.append(params.get("departure_token"))
        if "departure_token" not in params:
            row = segment(
                "JFK", "LHR", "2027-03-18 22:00", "2027-03-19 09:00", "TA 123"
            )
            return httpx.Response(
                200,
                json={
                    "best_flights": [{"flights": [row], "departure_token": "to-london"}]
                },
            )
        row = segment("CDG", "JFK", "2027-03-22 12:00", "2027-03-22 15:30", "TA 321")
        return httpx.Response(
            200, json={"best_flights": [{"flights": [row], "price": 580}]}
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        offers = Providers(http).search_flights(
            Flight(
                adults=1,
                slices=[
                    {
                        "origin": "JFK",
                        "destination": "LHR",
                        "departure_date": "2027-03-18",
                    },
                    {
                        "origin": "CDG",
                        "destination": "JFK",
                        "departure_date": "2027-03-22",
                    },
                ],
            )
        )
    assert calls == [None, "to-london"]
    assert len(offers[0].details["slices"]) == 2 and offers[0].amount == 580


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(
            200,
            json={
                "error": "Google Flights hasn't returned any results for this query."
            },
        ),
        httpx.Response(
            429, headers={"Retry-After": "60"}, json={"error": "quota: fixture-secret"}
        ),
    ],
)
def test_serpapi_empty_search_and_quota(serp_credentials, response):
    def handle(request):
        assert request.url.params["type"] == "2"
        return response

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        service = Providers(http)
        trip = Flight(
            adults=1,
            slices=[
                {"origin": "JFK", "destination": "MIA", "departure_date": "2027-03-18"},
            ],
        )
        if response.is_success:
            assert service.search_flights(trip) == []
        else:
            with pytest.raises(Unavailable, match="HTTP 429") as error:
                service.search_flights(trip)
            assert "fixture-secret" not in str(error.value)
