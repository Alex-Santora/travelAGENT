import os
from datetime import datetime, timedelta

import httpx
import streamlit as st
from dotenv import load_dotenv

from travel.scenarios import SCENARIOS

load_dotenv()
st.set_page_config(page_title="Wayledger · Trip planner", layout="wide")


def usd(cents):
    return f"${cents / 100:,.2f}"


def results(report):
    trip, best = report["request"], report["selected"]
    hotel, ride = best["hotel"], best["transport"]
    st.divider()
    st.header(f"{trip['origin']} → {trip['destination']}")
    days = hotel["nights"] + 1
    st.caption(
        f"{trip['departure']} through {trip['returning']} · {days} days · "
        f"{trip['travelers']} traveler(s) · Simulated estimates in USD"
    )
    if not best["feasible"]:
        st.warning("No complete trip fits your budget. Showing the smallest shortfall.")
    a, b, c = st.columns(3)
    a.metric("Whole trip", usd(best["total_cents"]))
    b.metric(
        "Budget remaining" if best["feasible"] else "Over budget",
        usd(abs(best["remaining_cents"])),
    )
    c.metric("Round-trip service", ride["mode"].title())
    st.write(
        f"**{ride['provider']}** via {ride['terminal']} · "
        + (
            f"{hotel['nights']} nights at **{hotel['name']}**, {hotel['district']}"
            if hotel["nights"]
            else "Day trip"
        )
    )
    st.write(report["explanation"])
    with st.expander("Interpreted constraints"):
        st.json(trip)
    route, budget, choices, trace = st.tabs(
        ["Itinerary", "Cost ledger", "Alternatives", "Agent execution"]
    )
    with route:
        events = best["events"]
        for i in range(days):
            day = datetime.fromisoformat(trip["departure"]) + timedelta(days=i)
            items = [e for e in events if e["start"][:10] == day.date().isoformat()]
            expenses = sum(e["cents"] for e in items) + best["costs"]["meals"] // days
            if i < hotel["nights"]:
                expenses += (
                    best["costs"]["accommodation"] + best["costs"]["fees"]
                ) // hotel["nights"]
            with st.expander(f"{day:%a, %b %d}   ·   {usd(expenses)}", expanded=i == 0):
                st.table(
                    [
                        {
                            "Time": e["start"][11:16] + " – " + e["end"][11:16],
                            "Stop or connection": e["title"],
                            "Duration": str(
                                (
                                    datetime.fromisoformat(e["end"])
                                    - datetime.fromisoformat(e["start"])
                                ).seconds
                                // 60
                            )
                            + " min",
                            "Party cost": usd(e["cents"]),
                        }
                        for e in items
                    ]
                )
                st.caption(
                    "Daily totals include the meal allowance and any overnight room and tax."
                )
    with budget:
        labels = [
            "Round-trip tickets",
            "Rooms",
            "Terminal transfers",
            "Local journeys",
            "Admissions",
            "Meals",
            "Lodging tax",
        ]
        keys = [
            "primary",
            "accommodation",
            "transfers",
            "local",
            "activities",
            "meals",
            "fees",
        ]
        st.table(
            [
                {"Expense": label, "Party total": usd(best["costs"][key])}
                for label, key in zip(labels, keys)
            ]
        )
        st.caption(
            f"{hotel['rooms']} room(s) × {hotel['nights']} nights × {usd(hotel['nightly_cents'])} per room."
        )
        for note in report["assumptions"]:
            st.write("• " + note)
    with choices:
        st.caption(
            "Lower scores rank first within budget. When all trips exceed budget, smallest shortfall ranks first."
        )
        rows = [
            dict(best, mode=ride["mode"], hotel=hotel["name"]),
            *report["alternatives"],
        ]
        st.dataframe(
            [
                {
                    "Travel": r["mode"],
                    "Lodging": r["hotel"] if hotel["nights"] else "Day trip",
                    "Total": usd(r["total_cents"]),
                    "Cost difference": usd(r["total_cents"] - best["total_cents"]),
                    "Travel (min)": r["travel_minutes"],
                    "Difference (min)": r["travel_minutes"] - best["travel_minutes"],
                    "Visits": r["visits"],
                    "Within budget": r["feasible"],
                    "Score": r["score"],
                }
                for r in rows
            ],
            hide_index=True,
        )
    with trace:
        st.caption(
            f"AI planner · {len(report['trace'])} tool calls · simulated catalog"
        )
        for i, call in enumerate(report["trace"], 1):
            with st.expander(f"{i:02d}   {call['tool']}"):
                st.json(call)


st.caption("WAYLEDGER  /  NORTHEAST TRIP PLANNER")
st.title("Plan the whole trip")
st.write(
    "Compare round-trip fares, rooms and the journeys in between. Depart from New York City for Boston or Philadelphia."
)
with st.form("request"):
    prompt = st.text_area("Describe your trip", SCENARIOS["A"], height=100)
    with st.expander("Trip settings"):
        st.caption("These fields override details in your request.")
        a, b = st.columns(2)
        destination = a.selectbox("Destination", ["Boston", "Philadelphia"], index=None)
        travelers = b.number_input("Travelers", min_value=1, max_value=8, value=None)
        departure = a.date_input("Depart", value=None)
        returning = b.date_input("Return", value=None)
        amount = a.number_input(
            "Total budget (USD)", min_value=0.01, max_value=1000000.0, value=None
        )
        priority = b.selectbox(
            "Ranking preference", ["balanced", "cost", "time"], index=None
        )
    st.caption(
        "The AI planner compares simulated fares and schedules. Include dates, party size and total budget."
    )
    submitted = st.form_submit_button("Compare trips", type="primary")
if submitted:
    st.session_state.pop("report", None)
    overrides = {
        "destination": destination,
        "travelers": travelers,
        "departure": departure,
        "returning": returning,
        "budget_usd": amount,
        "priority": priority,
    }
    overrides = {
        k: (v.isoformat() if hasattr(v, "isoformat") else v)
        for k, v in overrides.items()
        if v is not None
    }
    try:
        with st.spinner("AI is checking constraints and comparing complete journeys…"):
            response = httpx.post(
                os.getenv("TRAVEL_API_URL", "http://127.0.0.1:8000") + "/plan",
                json={"prompt": prompt, "overrides": overrides},
                timeout=660,
            )
            if response.is_success:
                st.session_state.report = response.json()
            else:
                detail = response.json().get("detail")
                st.error(
                    detail.get("message")
                    if isinstance(detail, dict)
                    else "Check the trip fields and try again."
                )
                if isinstance(detail, dict) and detail.get("trace"):
                    with st.expander("Execution trace"):
                        st.json(detail["trace"])
    except (httpx.HTTPError, ValueError):
        st.error(
            "Could not read the planner response. Check that the FastAPI server is running and try again."
        )
if "report" in st.session_state:
    results(st.session_state.report)
