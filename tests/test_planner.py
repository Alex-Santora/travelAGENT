import asyncio
import itertools
import json

import httpx
import pytest
from openai import OpenAI

from travel import agent, catalog
from travel.api import app
from travel.request import Submission
from travel.scenarios import SCENARIOS

BASE = {
    "origin": "New York City",
    "destination": "Boston",
    "travelers": 1,
    "departure": "2027-04-12",
    "days": 4,
    "budget_usd": 900,
}
EXTRACTED = {
    SCENARIOS["A"]: BASE,
    SCENARIOS["B"]: dict(
        BASE,
        destination="Philadelphia",
        days=3,
        budget_usd=850,
        priority="cost",
        interests=["history"],
    ),
    SCENARIOS["C"]: dict(BASE, travelers=2, budget_usd=120),
}


def completion(calls):
    return httpx.Response(
        200,
        json={
            "id": "fixture",
            "created": 0,
            "model": "fixture",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": ident,
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(args),
                                },
                            }
                            for ident, name, args in calls
                        ],
                    },
                }
            ],
        },
    )


def scripted_client(fields, prompt, *, bad_call=False):
    counter = itertools.count()

    def handler(request):
        payload = json.loads(request.content)
        assert json.loads(payload["messages"][1]["content"])["prompt"] == prompt
        # Every prior call must have exactly one correlated tool message.
        requested = [
            c["id"] for m in payload["messages"] for c in m.get("tool_calls", [])
        ]
        replies = [m for m in payload["messages"] if m["role"] == "tool"]
        assert requested == [m["tool_call_id"] for m in replies]
        names = [t["function"]["name"] for t in payload["tools"]]
        if "set_trip" in names:
            names = ["clarify" if replies else "set_trip"]
        else:
            names = sorted(names, reverse=True)
        calls = []
        for name in names:
            args = {}
            if name == "set_trip":
                args = fields
            elif name == "clarify":
                args = {"question": json.loads(replies[-1]["content"])["error"]}
            elif name == "recommend":
                result = json.loads(replies[-1]["content"])["data"]
                assert result["schedule"] and "total_cents" in result["choice"]
                args = {"emphasis": "transfers"}
            elif name == "search_hotels" and bad_call and len(replies) == 1:
                args = {"invent_price": 1}
            calls.append((str(next(counter)), name, args))
        return completion(calls)

    return fake_client(handler)


def run(prompt=SCENARIOS["A"], *, extracted=None, **fields):
    values = EXTRACTED[prompt] if extracted is None else extracted
    with scripted_client(values, prompt) as client:
        return agent.plan(Submission(prompt=prompt, overrides=fields), client)


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, "Please provide"),
        (dict(BASE, origin="Chicago"), "origin"),
        (dict(BASE, destination="Paris"), "destination"),
        (dict(BASE, origin="Boston"), "New York City"),
        (dict(BASE, budget_usd=-50), "budget_usd"),
        (dict(BASE, budget_usd=0), "budget_usd"),
        (dict(BASE, budget_usd=1000001), "budget_usd"),
        (dict(BASE, days=0), "days"),
        (dict(BASE, travelers=9), "travelers"),
        (dict(BASE, departure="2027-02-29"), "departure"),
        (dict(BASE, returning="2027-04-01"), "conflicts"),
        ({k: v for k, v in BASE.items() if k != "departure"}, "departure"),
    ],
)
def test_bad_requests(fields, expected):
    with pytest.raises(agent.PlanningError, match=expected) as error:
        run("Please plan a trip", extracted=fields)
    assert error.value.trace[-1].tool == "clarify"


def test_override_and_defaults():
    r = run(departure="2027-08-21", travelers=3, budget_usd="800.21")
    assert (r.request.returning - r.request.departure).days == 3
    assert r.request.budget_cents == 80021
    assert r.selected.hotel.rooms == 2
    assert r.selected.hotel.nights == 3
    assert r.selected.costs.accommodation == r.selected.hotel.nightly_cents * 2 * 3
    assert r.selected.costs.fees == (r.selected.costs.accommodation * 12 + 50) // 100


@pytest.mark.parametrize("key", list(SCENARIOS))
def test_pipeline(key):
    r = run(SCENARIOS[key])
    best = r.selected
    assert {e.tool for e in r.trace} == set(agent.TOOLS) - {"clarify"}
    assert r.trace[-1].output["explanation"] == r.explanation
    assert f"${best.costs.transfers / 100:,.2f}" in r.explanation
    assert all(e.output and not e.error for e in r.trace)
    assert best.costs.total == best.total_cents
    assert best.remaining_cents == r.request.budget_cents - best.total_cents
    assert (
        best.costs.primary
        == 2 * best.transport.fare_each_way_cents * r.request.travelers
    )
    assert best.costs.meals == 3500 * r.request.travelers * (r.request.nights + 1)
    assert best.feasible == (key != "C")
    assert best.model_dump() == run(SCENARIOS[key]).selected.model_dump()
    if key == "C":
        assert best.total_cents == min(
            [best.total_cents] + [r.total_cents for r in r.alternatives]
        )
    for category in ("transfers", "primary", "local", "activities"):
        assert sum(e.cents for e in best.events if e.category == category) == getattr(
            best.costs, category
        )
    events = best.events
    assert all(e.start < e.end for e in events)
    assert all(a.end <= b.start for a, b in itertools.pairwise(events))
    activities = {a.name: a for a in catalog.search_activities(r.request)}
    for e in events:
        if e.category == "activities":
            assert (
                best.transport.outbound[1]
                <= e.start
                < e.end
                <= best.transport.inbound[0]
            )
            a = activities[e.title]
            assert (
                e.start.hour >= a.opens
                and e.end.hour * 60 + e.end.minute <= a.closes * 60
            )


def test_cheapest_ticket_loses_when_transfers_count():
    r = run(SCENARIOS["B"])
    best = r.selected
    air = next(
        c for c in r.alternatives if c.mode == "air" and c.hotel == best.hotel.name
    )
    assert air.costs.primary < best.costs.primary
    assert air.costs.transfers > best.costs.transfers
    assert air.total_cents > best.total_cents
    assert best.total_cents == min(c.total_cents for c in [best, *r.alternatives])


def test_priority_changes_transport_and_budget_is_hard():
    fast = run(priority="time")
    cheap = run(priority="cost")
    assert fast.selected.travel_minutes < cheap.selected.travel_minutes
    constrained = run(priority="time", budget_usd=cheap.selected.total_cents / 100)
    assert constrained.selected.feasible and constrained.selected.remaining_cents == 0


def test_same_day_has_no_lodging_or_transfer_to_hotel():
    r = run(days=1, budget_usd=400)
    assert (
        r.selected.hotel.rooms
        == r.selected.hotel.nights
        == r.selected.costs.accommodation
        == 0
    )
    assert not any(r.selected.hotel.name in e.title for e in r.selected.events)


@pytest.mark.parametrize("missing", ["search_hotels", "both_services"])
def test_missing_inventory(missing, monkeypatch):
    names = (
        ["search_hotels"]
        if missing == "search_hotels"
        else ["search_flights", "search_ground_transport"]
    )
    for name in names:
        monkeypatch.setitem(agent.SEARCH, name, lambda trip: [])
    with pytest.raises(
        agent.PlanningError, match="No transportation or lodging"
    ) as exc:
        run()
    assert exc.value.trace[-1].error


def test_api(monkeypatch):
    def backend(submission):
        with scripted_client(
            EXTRACTED.get(submission.prompt, {}), submission.prompt
        ) as client:
            return agent.plan(submission, client)

    monkeypatch.setattr("travel.api.plan", backend)

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/health")).json() == {"status": "ok"}
            assert (await client.post("/plan", json={"prompt": SCENARIOS["B"]})).json()[
                "selected"
            ]["feasible"]
            assert (
                await client.post("/plan", json={"prompt": "to Paris"})
            ).status_code == 422
            assert (
                await client.post(
                    "/plan", json={"prompt": "", "overrides": {"travelers": 0}}
                )
            ).status_code == 422

    asyncio.run(exercise())


def fake_client(handler):
    return OpenAI(
        api_key="offline-test",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_sdk_tool_loop_rejects_bad_args_and_accepts_model_search_order():
    prompt = SCENARIOS["A"]
    with scripted_client(BASE, prompt, bad_call=True) as client:
        result = agent.plan(Submission(prompt=prompt), client)
    calls = [e for e in result.trace if e.tool == "search_hotels"]
    assert len(calls) == 2 and calls[0].error and calls[1].output
    assert result.selected.total_cents == run().selected.total_cents


def test_provider_timeout_redacted():
    def timeout(request):
        raise httpx.ReadTimeout("sensitive text", request=request)

    with pytest.raises(agent.PlanningError, match="timed out") as exc:
        agent.plan(Submission(prompt="Boston"), fake_client(timeout))
    assert "sensitive" not in str(exc.value)


@pytest.mark.parametrize("city", ["Boston", "Philadelphia"])
@pytest.mark.parametrize("duration", [1, 2, 4, 14])
def test_all_candidate_schedules(city, duration):
    r = run(days=duration, destination=city, travelers=3, budget_usd=4000)
    trip = r.request
    candidates = agent.estimate_trip_costs(
        trip,
        catalog.services(trip, ["rail", "bus", "air"]),
        catalog.search_hotels(trip),
        catalog.search_activities(trip),
    )
    for candidate in candidates:
        events = candidate.events
        assert all(a.end <= b.start for a, b in itertools.pairwise(events))
        assert {e.start.date() for e in events} == {
            trip.departure.fromordinal(trip.departure.toordinal() + i)
            for i in range(duration)
        }
        assert candidate.total_cents == (
            sum(e.cents for e in events)
            + candidate.costs.accommodation
            + candidate.costs.fees
            + candidate.costs.meals
        )


def test_no_key(monkeypatch):
    monkeypatch.setenv("TRAVEL_LLM_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(agent.PlanningError, match="OPENAI_API_KEY"):
        agent.plan(Submission(prompt="to Boston"))


def test_ground_only_workflow():
    r = run(extracted=dict(BASE, allowed_modes=["rail"]))
    assert "search_flights" not in [e.tool for e in r.trace]
    assert r.selected.transport.mode == "rail"
    assert all(a.mode == "rail" for a in r.alternatives)


def test_reject_old_mode():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Submission(prompt="Boston", mode="demo")


@pytest.mark.parametrize(
    "status, message",
    [(401, "authentication"), (400, "tool-calling"), (429, "rate limited")],
)
def test_provider_errors(status, message):
    with (
        fake_client(
            lambda _: httpx.Response(status, json={"error": {"message": "private"}})
        ) as client,
        pytest.raises(agent.PlanningError, match=message) as error,
    ):
        agent.plan(Submission(prompt=SCENARIOS["A"]), client)
    assert not error.value.trace and "private" not in str(error.value)


def test_unknown_tool_stops_at_limit(monkeypatch):
    monkeypatch.setenv("TRAVEL_MAX_ROUNDS", "2")
    with (
        fake_client(lambda _: completion([("bad", "invent_prices", {})])) as client,
        pytest.raises(agent.PlanningError, match="round limit") as error,
    ):
        agent.plan(Submission(prompt=SCENARIOS["A"]), client)
    assert len(error.value.trace) == 2 and all(e.error for e in error.value.trace)


def test_live_provider():
    import os

    if os.getenv("TRAVEL_RUN_LIVE") != "1":
        pytest.skip("Set TRAVEL_RUN_LIVE=1 to call the configured provider")
    if os.getenv("TRAVEL_LLM_PROVIDER", "openai") == "openai" and not os.getenv(
        "OPENAI_API_KEY"
    ):
        pytest.skip("OPENAI_API_KEY unavailable")
    report = agent.plan(Submission(prompt=SCENARIOS["A"]))
    assert report.selected.feasible
    assert any(e.tool in agent.SEARCH and e.output for e in report.trace)


def test_streamlit_result_and_clarification(monkeypatch):
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    def post(url, *, json, timeout):
        submission = Submission.model_validate(json)
        with scripted_client(
            EXTRACTED.get(submission.prompt, {}), submission.prompt
        ) as client:
            try:
                report = agent.plan(submission, client)
            except agent.PlanningError as exc:
                return httpx.Response(
                    exc.status,
                    json={
                        "detail": {
                            "message": str(exc),
                            "trace": [e.model_dump() for e in exc.trace],
                        }
                    },
                )
            return httpx.Response(200, json=report.model_dump(mode="json"))

    monkeypatch.setattr(httpx, "post", post)
    ui = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run()
    assert not ui.radio
    ui.button[0].click().run()
    assert not ui.exception and ui.metric[0].value == "$579.68"
    assert "Agent execution" in [t.label for t in ui.tabs]
    ui.text_area[0].set_value("Plan a trip")
    ui.button[0].click().run()
    assert not ui.exception and "departure" in ui.error[0].value and not ui.metric
