import json

import httpx
import pytest
from fastapi.testclient import TestClient
from openai import OpenAI
from streamlit.testing.v1 import AppTest

from travel import agent, llm
from travel.api import app
from travel.catalog import Providers, Unavailable, record
from travel.models import Candidate, Flight, Query, Stay, Trip
from travel.optimize import evaluate

TRIP = {
    "origin": "Boston",
    "destinations": ["Lisbon"],
    "start": "2027-04-12",
    "end": "2027-04-14",
    "adults": 2,
    "budget": "100",
    "currency": "USD",
}
POINT = {"latitude": 38.7, "longitude": -9.1}


def providers(handler):
    return Providers(httpx.Client(transport=httpx.MockTransport(handler)))


def test_no_keys_and_test_environment(monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.delenv("DUFFEL_ACCESS_TOKEN", raising=False)
    service = providers(lambda req: pytest.fail("Unexpected network call"))
    args = Flight(
        slices=[
            {"origin": "BOS", "destination": "LIS", "departure_date": TRIP["start"]}
        ],
        adults=2,
    )
    with pytest.raises(Unavailable, match="DUFFEL_ACCESS_TOKEN"):
        service.search_flights(args)
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_test_example")
    with pytest.raises(Unavailable, match="live Duffel"):
        service.search_flights(args)


def test_timeouts_rate_limit_and_empty(monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_live_fixture")
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.host == "api.duffel.com":
            assert (
                json.loads(request.content)["data"]["passengers"]
                == [{"type": "adult"}] * 2
            )
            return httpx.Response(201, json={"data": {"live_mode": True, "offers": []}})
        raise httpx.ReadTimeout("timeout")

    p = providers(handler)
    assert (
        p.search_flights(
            Flight(
                slices=[
                    {
                        "origin": "BOS",
                        "destination": "LIS",
                        "departure_date": TRIP["start"],
                    }
                ],
                adults=2,
            )
        )
        == []
    )
    with pytest.raises(Unavailable, match="ReadTimeout"):
        p.resolve_location(Query(query="Lisbon"))
    assert len(calls) == 3


def test_stay_access_and_rates(monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_live_fixture")
    args = Stay(center=POINT, check_in=TRIP["start"], check_out=TRIP["end"], adults=2)
    p = providers(lambda req: httpx.Response(403, json={}))
    with pytest.raises(Unavailable, match="403"):
        p.search_hotels(args)

    def handler(req):
        if req.url.path.endswith("fetch_all_rates"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "accommodation": {
                            "rooms": [
                                {"name": "Queen", "rates": [{"total_amount": "125"}]}
                            ]
                        }
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "results": [
                        {
                            "id": "srr_fixture",
                            "accommodation": {"name": "Test property"},
                            "cheapest_rate_total_amount": "125",
                            "cheapest_rate_currency": "EUR",
                        }
                    ]
                }
            },
        )

    row = providers(handler).search_hotels(args)[0]
    assert row.details["rooms"][0]["name"] == "Queen"
    assert (
        row.price_status == "live quote"
        and row.currency == "EUR"
        and row.details["adults"] == 2
    )


def test_calendar_budget_and_missing_fares():
    route = record(
        "route",
        "Tram",
        "fixture",
        details={"seconds": 1800},
        amount="4.60",
        currency="EUR",
        basis="per traveler",
        price_status="published",
    )
    fx = record(
        "fx",
        "EUR/USD",
        "fixture",
        details={"base": "EUR", "target": "USD", "rate": "1.25"},
    )
    venue = record(
        "place",
        "Golf club",
        "fixture",
        details={
            "timeZone": {"id": "Europe/Lisbon"},
            "regularOpeningHours": {
                "periods": [
                    {"open": {"day": 1, "hour": 9}, "close": {"day": 1, "hour": 16}}
                ]
            },
        },
    )
    visit = {
        "evidence": "club",
        "start": "2027-04-12T17:00:00+01:00",
        "end": "2027-04-12T18:00:00+01:00",
    }
    ride = {
        "evidence": "tram",
        "start": "2027-04-12T16:55:00+01:00",
        "end": "2027-04-12T17:05:00+01:00",
    }
    result = evaluate(
        Candidate(items=[visit, ride]),
        Trip(**TRIP),
        {"club": venue, "tram": route, "rate": fx},
    )
    assert result["known_total"] == "11.50" and result["total_status"] == "partial"
    assert any("outside published" in x for x in result["issues"])
    assert any("shorter" in x for x in result["issues"])
    assert any("Overlap" in x for x in result["issues"])
    assert any("Unpriced" in x for x in result["gaps"])
    with pytest.raises(ValueError, match="evidence IDs"):
        evaluate(Candidate(items=[visit]), Trip(**TRIP), {})


def chat_reply(content):
    return httpx.Response(
        200,
        json={
            "id": "test",
            "object": "chat.completion",
            "created": 1,
            "model": "test",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
        },
    )


def test_non_native_fallback_repairs_and_executes_real_dispatch():
    requests = []

    def handler(req):
        data = json.loads(req.content)
        requests.append(req.url.path)
        if "tools" in data or req.url.path.endswith("responses"):
            return httpx.Response(
                400,
                json={
                    "error": {"message": "unsupported", "type": "invalid_request_error"}
                },
            )
        transcript = data["messages"][0]["content"]
        if "Invalid JSON" not in transcript:
            return chat_reply("invalid")
        if (
            "executed"
            not in transcript.split("Conversation and executed tool results:")[-1]
        ):
            return chat_reply(
                json.dumps(
                    {
                        "actions": [
                            {
                                "name": "resolve_location",
                                "arguments": {"query": "Lisbon"},
                            }
                        ]
                    }
                )
            )
        return chat_reply(
            json.dumps(
                {
                    "actions": [
                        {
                            "name": "clarify",
                            "arguments": {
                                "question": "Which travel dates?",
                                "missing": "travel window",
                            },
                        }
                    ]
                }
            )
        )

    ai = OpenAI(
        api_key="fixture",
        base_url="https://model.invalid/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    dialogue = llm.Dialogue(ai, "test", "Use tools", {"request": "travel"}, [])
    assert dialogue.step() == []
    call = dialogue.step()[0]
    geocoder = providers(
        lambda req: httpx.Response(
            200, json={"results": [{"name": "Lisbon", "country": "Portugal"}]}
        )
    )
    result = geocoder.resolve_location(Query.model_validate_json(call[2]))[
        0
    ].model_dump(mode="json")
    dialogue.accept(call, result)
    assert dialogue.step()[0][1] == "clarify"
    assert dialogue.mode == "chat-json"
    assert len(requests) == 6


def test_native_ids_and_context():
    requests = []

    def handler(req):
        body = json.loads(req.content)
        requests.append(body)
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "object": "response",
                "created_at": 1,
                "model": "test",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_abc",
                        "name": "search_web",
                        "arguments": "{}",
                    }
                ],
            },
        )

    ai = OpenAI(
        api_key="fixture",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    dialogue = llm.Dialogue(ai, "test", "system", {}, [])
    call = dialogue.step()[0]
    dialogue.accept(call, {"error": "Unavailable"})
    dialogue.step()
    assert requests[1]["previous_response_id"] == "resp_1"
    assert requests[1]["input"][0]["call_id"] == "call_abc"


def test_clarification_followup_and_stream(monkeypatch):
    class FakeDialogue:
        mode = "responses"

        def __init__(self, ai, model, system, context, schemas):
            self.context = context

        def feedback(self, *args):
            pass

        def step(self):
            if self.context["previous_turns"]:
                assert (
                    self.context["previous_turns"][0]["outcome"]["question"]
                    == "When do you want to travel?"
                )
            return [
                (
                    "1",
                    "clarify",
                    json.dumps(
                        {
                            "question": "When do you want to travel?",
                            "missing": "travel window",
                        }
                    ),
                )
            ]

    monkeypatch.setattr(agent.llm, "Dialogue", FakeDialogue)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture")
    client = TestClient(app)
    original = client.post("/plan", json={"prompt": "Take me to Lisbon"}).json()
    assert original["question"] and original["conversation"]
    lines = client.post(
        "/plan/stream",
        json={
            "prompt": "Tell me what you need",
            "conversation": original["conversation"],
        },
    ).text.splitlines()
    assert json.loads(lines[0])["event"] == "progress"
    assert json.loads(lines[-1])["data"]["conversation"] == original["conversation"]
    assert client.post("/plan", json={"prompt": " "}).status_code == 422
    assert not AppTest.from_file("../app.py").run().exception


def test_price_expiry():
    session = agent.Session()
    session.evidence["old"] = record(
        "flight", "Expired", "fixture", details={"expires_at": "2000-01-01T00:00:00Z"}
    )
    session.cache["flight"] = ["old"]
    session.prune()
    assert not session.evidence and not session.cache


def test_return_flight_uses_airport_zones_and_bills_once(monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_live_fixture")
    bos = {"iata_code": "BOS", "time_zone": "America/New_York"}
    lis = {"iata_code": "LIS", "time_zone": "Europe/Lisbon"}

    def segment(origin, destination, depart, arrive):
        return {
            "origin": origin,
            "destination": destination,
            "departing_at": depart,
            "arriving_at": arrive,
            "marketing_carrier": {"name": "Fixture Air"},
            "marketing_carrier_flight_number": "123",
        }

    slices = [
        {"segments": [segment(bos, lis, "2027-04-12T21:00:00", "2027-04-13T08:00:00")]},
        {"segments": [segment(lis, bos, "2027-04-14T12:00:00", "2027-04-14T14:00:00")]},
    ]
    data = {
        "live_mode": True,
        "offers": [
            {
                "id": "off_fixture",
                "owner": {"name": "Fixture Air"},
                "slices": slices,
                "total_currency": "USD",
                "total_amount": "500.26",
            }
        ],
    }
    p = providers(lambda req: httpx.Response(201, json={"data": data}))
    row = p.search_flights(
        Flight(
            slices=[
                {
                    "origin": "BOS",
                    "destination": "LIS",
                    "departure_date": TRIP["start"],
                },
                {"origin": "LIS", "destination": "BOS", "departure_date": TRIP["end"]},
            ],
            adults=2,
        )
    )[0]
    slots = [
        {
            "evidence": "f",
            "slice_index": i,
            "start": "2027-04-12T00:00:00Z",
            "end": "2027-04-12T01:00:00Z",
        }
        for i in (0, 1)
    ]
    result = evaluate(Candidate(items=slots), Trip(**TRIP), {"f": row})
    assert result["known_total"] == "500.26" and len(result["costs"]) == 1
    assert result["travel_minutes"] == 13 * 60
    assert result["items"][0]["end"] == "2027-04-13T08:00:00+01:00"
    assert "Known costs exceed the trip budget" in result["issues"]


def test_filling_empty_day_uses_checked_venues_and_measured_routes():
    from travel.models import Finish
    from travel.optimize import distance_km
    from travel.schedule import fill_visit_days

    data = agent.Session()
    data.evidence = {
        "city": record(
            "location",
            "Lisbon",
            "fixture",
            details=POINT | {"timezone": "Europe/Lisbon"},
        ),
        "p1": record(
            "place",
            "Museum",
            "fixture",
            details={"location": POINT, "osm_value": "museum"},
        ),
        "p2": record(
            "place",
            "Club",
            "fixture",
            details={
                "location": {"latitude": 38.708, "longitude": -9.1},
                "osm_value": "golf_course",
            },
        ),
    }
    finish = Finish(
        trip=TRIP | {"daily_visits": True, "requirements": ["golf"]},
        candidates=[{"items": []}],
    )
    fill_visit_days(finish, data.evidence, distance_km)
    assert [s.evidence for s in finish.candidates[0].items] == ["p2", "p1"]
    service = providers(
        lambda req: httpx.Response(
            200, json={"routes": [{"duration": 600, "distance": 900}]}
        )
    )
    import os
    from unittest.mock import patch

    with patch.dict(os.environ, {"GOOGLE_MAPS_API_KEY": ""}):
        assert (
            agent.connect_visits(finish, data, service, lambda *args, **kwargs: None, 2)
            == 1
        )
    assert finish.candidates[0].items[-1].end.minute == 10


def test_duration_and_hotel_deadlines():
    with pytest.raises(ValueError, match="inclusive duration"):
        Trip(**TRIP, duration_days=2)
    row = record(
        "hotel",
        "Fixture Inn",
        "fixture",
        details={
            "check_in": TRIP["start"],
            "check_out": TRIP["end"],
            "check_in_information": {
                "check_in_after_time": "15:00",
                "check_out_before_time": "11:00",
            },
            "adults": 2,
        },
    )
    c = Candidate(
        items=[
            {
                "evidence": "h",
                "start": "2027-04-12T10:00:00+01:00",
                "end": "2027-04-14T12:00:00+01:00",
            }
        ]
    )
    result = evaluate(c, Trip(**TRIP), {"h": row})
    assert any("check-in opens" in i for i in result["issues"])
    assert any("check-out deadline" in i for i in result["issues"])


def test_calendar_evidence_and_qualified_geocoding():
    from travel.request import has_window
    from travel.scenarios import SCENARIOS

    assert all(has_window(text) for text in SCENARIOS.values())
    assert not has_window("Plan a trip from Boston to Lisbon")

    def handler(req):
        if "," in req.url.params["name"]:
            return httpx.Response(200, json={})
        return httpx.Response(
            200,
            json={
                "results": [
                    {"name": "Porto", "country": "Portugal"},
                    {"name": "Porto", "country": "France"},
                ]
            },
        )

    rows = providers(handler).resolve_location(Query(query="Porto, Portugal"))
    assert [r.details["country"] for r in rows] == ["Portugal"]


def test_flexible_length_and_provider_backoff():
    assert str(Trip(**TRIP, flexible_dates=True, duration_days=2).end) == "2027-04-13"
    requests = []

    def handler(req):
        requests.append(req)
        return httpx.Response(429, headers={"Retry-After": "300"})

    with pytest.raises(Unavailable, match="429"):
        providers(handler).resolve_location(Query(query="Porto"))
    assert len(requests) == 1


def test_model_dispatches_live_rate_adapter_and_reuses_on_revision(monkeypatch):
    from travel.request import Submission

    requests = []

    def handler(req):
        requests.append(req.url.path)
        if req.url.path.endswith("fetch_all_rates"):
            return httpx.Response(200, json={"data": {"accommodation": {"rooms": []}}})
        assert req.url.path == "/stays/search"
        return httpx.Response(
            201,
            json={
                "data": {
                    "results": [
                        {
                            "id": "srr_fixture",
                            "accommodation": {"name": "Test Inn"},
                            "cheapest_rate_total_amount": "95.00",
                            "cheapest_rate_currency": "USD",
                        }
                    ]
                }
            },
        )

    adapter = providers(handler)

    class Script:
        mode = "chat-json"

        def __init__(self, ai, model, system, context, schemas):
            self.index = 0

        def feedback(self, msg):
            pass

        def accept(self, call, output):
            if call[1] == "search_hotels":
                self.ref = next(iter(output))

        def step(self):
            self.index += 1
            if self.index == 1:
                name, args = (
                    "interpret_trip",
                    TRIP | {"travel_window_quote": "April 12-14"},
                )
            elif self.index == 2:
                name, args = (
                    "search_hotels",
                    {
                        "center": POINT,
                        "adults": 2,
                        "check_in": TRIP["start"],
                        "check_out": TRIP["end"],
                    },
                )
            else:
                name, args = (
                    "finish",
                    {
                        "trip": TRIP,
                        "candidates": [
                            {
                                "items": [
                                    {
                                        "evidence": self.ref,
                                        "start": "2027-04-12T15:00:00+01:00",
                                        "end": "2027-04-14T10:00:00+01:00",
                                    }
                                ]
                            }
                        ],
                    },
                )
            return [(str(self.index), name, json.dumps(args))]

    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.setenv("DUFFEL_ACCESS_TOKEN", "duffel_live_fixture")
    monkeypatch.setattr(agent.llm, "Dialogue", Script)
    result = agent.plan(
        Submission(prompt="Find a hotel in Lisbon for two, April 12-14."),
        ai=object(),
        providers=adapter,
    )
    revised = agent.plan(
        Submission(prompt="Keep that hotel", conversation=result["conversation"]),
        ai=object(),
        providers=adapter,
    )
    assert revised["selected"]["known_total"] == "95.00" and len(requests) == 2
    assert any(e["state"] == "reused" for e in revised["trace"])


def test_map_listing_cannot_supply_an_overnight_stay():
    from travel.models import Finish

    session = agent.Session()
    session.evidence["hotel"] = record(
        "place", "Unquoted inn", "fixture", details={"osm_value": "hotel"}
    )
    slot = {
        "evidence": "hotel",
        "start": "2027-04-12T14:00:00+01:00",
        "end": "2027-04-14T10:00:00+01:00",
    }
    proposal = Finish(trip=Trip(**TRIP), candidates=[Candidate(items=[slot])])
    selected = agent.finalize_schedule(
        proposal, session, None, [], lambda *a, **kw: None
    )["selected"]
    assert not selected["items"] and selected["known_total"] is None
    assert any("requires dated availability" in g for g in selected["gaps"])


def test_missing_quotes_do_not_pause_a_sourced_itinerary(monkeypatch):
    from travel.request import Submission

    calls = [
        ("interpret_trip", TRIP | {"duration_days": 3}),
        (
            "search_hotels",
            {
                "center": POINT,
                "adults": 2,
                "check_in": TRIP["start"],
                "check_out": TRIP["end"],
            },
        ),
        (
            "search_flights",
            {
                "adults": 2,
                "slices": [
                    {
                        "origin": "BOS",
                        "destination": "LIS",
                        "departure_date": TRIP["start"],
                    }
                ],
            },
        ),
        ("search_web", {"query": "Museum in Lisbon"}),
        (
            "clarify",
            {"missing": "party", "question": "Should I continue without live quotes?"},
        ),
        (
            "clarify",
            {
                "missing": "ambiguous place",
                "question": "Should I wait for bookable inventory?",
            },
        ),
        (
            "finish",
            {
                "trip": TRIP | {"end": "2027-04-15", "duration_days": 3},
                "candidates": [
                    {
                        "items": [
                            {
                                "evidence": "E1",
                                "web_activity": "Lisbon Museum",
                                "start": "2027-04-13T10:00:00+01:00",
                                "end": "2027-04-13T12:00:00+01:00",
                            }
                        ]
                    }
                ],
            },
        ),
    ]

    class Script:
        mode = "responses"

        def __init__(self, *args):
            self.steps = iter(calls)

        def feedback(self, message):
            pass

        def step(self):
            name, args = next(self.steps)
            if name == "clarify":
                assert all(tool["name"] != "clarify" for tool in self.schemas)
            return [(name, name, json.dumps(args))]

        def accept(self, call, output):
            if call[1] == "clarify":
                assert "Continue in this turn" in output["error"]

    class InventoryUnavailable:
        def search_hotels(self, args):
            raise Unavailable("Configure DUFFEL_ACCESS_TOKEN on the backend.")

        def search_flights(self, args):
            return []

        def search_web(self, args):
            return [
                record("web", "Lisbon Museum", "fixture", "https://example.com/museum")
            ]

    monkeypatch.setenv("TRAVEL_MAX_ROUNDS", "12")
    monkeypatch.setattr(agent.llm, "Dialogue", Script)
    report = agent.plan(
        Submission(
            prompt="Plan Boston to Lisbon for two for 3 days, departing April 12, 2027 and returning April 15, 2027."
        ),
        ai=object(),
        providers=InventoryUnavailable(),
    )
    assert "question" not in report
    assert report["trip"]["end"] == "2027-04-15"
    assert (
        "The specified dates span 3 nights and 4 calendar days."
        in report["trip"]["assumptions"]
    )
    assert report["selected"]["items"][0]["name"] == "Lisbon Museum"
    assert report["selected"]["known_total"] is None
    assert (
        "No verified live flight quote in this itinerary." in report["selected"]["gaps"]
    )
    assert (
        "No verified live hotel quote in this itinerary." in report["selected"]["gaps"]
    )
