"""Opt-in network tests. Provider tests require actual data, and never inject responses."""

import json
import os
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

from travel.agent import plan
from travel.catalog import Providers
from travel.models import FX, Flight, PlacesQuery, Query, Route, Stay
from travel.request import Submission
from travel.scenarios import SCENARIOS

pytestmark = pytest.mark.skipif(
    os.getenv("TRAVEL_LIVE_TESTS") != "1", reason="opt-in live calls"
)
POINT = {"latitude": 38.7223, "longitude": -9.1393}
NEXT = {"latitude": 38.7139, "longitude": -9.1335}


@pytest.mark.parametrize(
    "name,args,key",
    [
        ("resolve_location", Query(query="Lisbon"), None),
        (
            "search_places",
            PlacesQuery(
                query="Kiyomizu-dera Kyoto",
                center={"latitude": 35.01, "longitude": 135.77},
            ),
            None,
        ),
        ("get_route", Route(origin=POINT, destination=NEXT, mode="WALK"), None),
        (
            "search_places",
            PlacesQuery(
                query="museums in Lisbon", center=POINT, osm_tag="tourism:museum"
            ),
            None,
        ),
        ("currency_rate", FX(base="EUR", target="USD"), None),
        ("get_route", Route(origin=POINT, destination=NEXT, mode="DRIVE"), None),
        (
            "search_places",
            PlacesQuery(
                query="golf courses near Kyoto Japan",
                center={"latitude": 35.01, "longitude": 135.77},
            ),
            "GOOGLE_MAPS_API_KEY",
        ),
        (
            "get_route",
            Route(origin=POINT, destination=NEXT, mode="WALK"),
            "GOOGLE_MAPS_API_KEY",
        ),
        (
            "search_flights",
            Flight(
                slices=[
                    {
                        "origin": "JFK",
                        "destination": "LIS",
                        "departure_date": date.today() + timedelta(days=60),
                    }
                ],
                adults=1,
            ),
            "DUFFEL_ACCESS_TOKEN",
        ),
        (
            "search_hotels",
            Stay(
                center=POINT,
                check_in=date.today() + timedelta(days=60),
                check_out=date.today() + timedelta(days=62),
                adults=1,
            ),
            "DUFFEL_ACCESS_TOKEN",
        ),
        (
            "search_published_hotels",
            Stay(
                center=POINT,
                check_in=date.today() + timedelta(days=60),
                check_out=date.today() + timedelta(days=62),
                adults=1,
            ),
            "SERPAPI_API_KEY",
        ),
    ],
)
def test_provider(name, args, key, monkeypatch):
    if key and not os.getenv(key):
        pytest.skip("Missing " + key)
    if key == "DUFFEL_ACCESS_TOKEN":
        monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    with httpx.Client(timeout=25) as http:
        rows = getattr(Providers(http), name)(args)
        assert rows and all(row.retrieved for row in rows)


def test_serpapi_round_trip_without_duffel(monkeypatch):
    if not os.getenv("SERPAPI_API_KEY"):
        pytest.skip("Missing SERPAPI_API_KEY")
    monkeypatch.delenv("DUFFEL_ACCESS_TOKEN", raising=False)
    start = date.today() + timedelta(days=60)
    flight = Flight(
        adults=2,
        slices=[
            {"origin": "JFK", "destination": "MIA", "departure_date": start},
            {
                "origin": "MIA",
                "destination": "JFK",
                "departure_date": start + timedelta(days=5),
            },
        ],
    )
    with httpx.Client(timeout=25) as http:
        providers = Providers(http)
        assert providers.search_airports(Query(query="New York City"))
        rows = providers.search_flights(flight)
        assert rows and rows[0].price_status == "published"
        assert len(rows[0].details["slices"]) == 2


@pytest.mark.parametrize(
    "scenario,model",
    [("A", "gpt-4.1-mini"), ("B", "gpt-4o-mini"), ("C", "gpt-4.1-mini")],
)
def test_journey(scenario, model):
    report = plan(Submission(prompt=SCENARIOS[scenario], model=model))
    if folder := os.getenv("TRAVEL_LIVE_REPORT_DIR"):
        path = Path(folder)
        path.mkdir(parents=True, exist_ok=True)
        (path / f"scenario-{scenario}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8"
        )
    trace = report["trace"]
    print(
        scenario,
        model,
        report["api_mode"],
        [
            (e["tool"], e.get("count", e["state"]))
            for e in trace
            if e["state"] != "started"
        ],
        flush=True,
    )
    print(
        "outcome",
        ascii(
            {
                "trip": report.get("trip"),
                "items": [
                    i["name"] for i in report.get("selected", {}).get("items", [])
                ],
                "gaps": report.get("selected", {}).get("gaps", [])[:8],
                "errors": [
                    e.get("message") for e in trace if e["state"] == "unavailable"
                ][-2:],
            }
        ),
        flush=True,
    )
    assert "selected" in report, report.get("question") or report.get("incomplete")
    assert any(e["state"] == "returned" and e["count"] for e in trace)
    assert report["selected"]["items"]
    assert report["evidence"] and report["selected"]["total_status"] == "partial"
    assert all(
        i["kind"] == "break" or i["evidence"] in report["evidence"]
        for i in report["selected"]["items"]
    )


def test_text_orchestration_and_missing_dates(monkeypatch):
    monkeypatch.setenv("OPENAI_API_MODE", "chat-json")
    report = plan(
        Submission(prompt="Plan my trip from Boston to Lisbon.", model="gpt-4o-mini")
    )
    assert "question" in report and report["api_mode"] == "chat-json"


def test_text_model_calls_geocoder(monkeypatch):
    from travel.llm import Dialogue, client

    monkeypatch.setenv("OPENAI_API_MODE", "chat-json")
    schema = {
        "name": "resolve_location",
        "description": "Search a city by name",
        "parameters": Query.model_json_schema(),
    }
    with client() as ai, httpx.Client(timeout=20) as http:
        dialogue = Dialogue(
            ai,
            "gpt-4o-mini",
            "Use resolve_location for the requested city.",
            {"request": "Find Porto, Portugal."},
            [schema],
        )
        calls = dialogue.step()
        if not calls:
            calls = dialogue.step()
        assert calls and calls[0][1] == "resolve_location"
        query = Query.model_validate_json(calls[0][2])
        rows = Providers(http).resolve_location(query)
        assert any(r.details.get("country") == "Portugal" for r in rows)
        dialogue.accept(calls[0], [r.model_dump(mode="json") for r in rows])
