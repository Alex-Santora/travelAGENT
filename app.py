import base64
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import streamlit as st

assets = Path(__file__).resolve().parent / "assets"
mark = str(assets / "travelAGENT.png")
st.set_page_config(
    page_title="travelAGENT",
    page_icon=mark,
    layout="wide",
    menu_items={
        "About": "travelAGENT researches and compares sourced travel itineraries."
    },
)
api = os.getenv("TRAVEL_API_URL", "http://127.0.0.1:8000").rstrip("/")


def details(row):
    st.caption(
        f"{row['provider']} · retrieved {row['retrieved']} · {row['availability']}"
    )
    price_label = (
        "Published price snapshot"
        if row["price_status"] == "published" and row["kind"] in {"flight", "hotel"}
        else row["price_status"]
    )
    st.write(
        f"{price_label} · "
        + (
            f"{row['currency']} {row['amount']} ({row['basis']})"
            if row["amount"] is not None
            else "Price unknown"
        )
    )
    if row["source"] and urlsplit(row["source"]).scheme in {"https", "http"}:
        label = (
            "View Google Flights search"
            if row["provider"] == "Google Flights via SerpApi"
            and row["kind"] == "flight"
            else "View source"
        )
        st.markdown(
            f"[{label}](<"
            + row["source"].replace(">", "%3E").replace("<", "%3C")
            + ">)"
        )
    data = row["details"]
    if fetched := data.get("search_created_at") or data.get("created_at"):
        st.caption(f"Provider search time · {fetched}")
    if row["kind"] == "flight":
        for flight_slice in data["slices"]:
            for seg in flight_slice["segments"]:
                st.write(
                    f"{seg['airline']} {seg.get('marketing_carrier_flight_number', '')} · {seg['origin']['iata_code']} {seg['departing_at']} → {seg['destination']['iata_code']} {seg['arriving_at']}"
                )
    elif row["kind"] == "hotel":
        st.write(
            f"{data['check_in']} to {data['check_out']} · {data['adults']} adult(s)"
        )
    elif row["kind"] == "route":
        st.write(f"{data['seconds'] / 60:.0f} minutes · {data['meters'] / 1000:.1f} km")
    elif row["kind"] == "web":
        st.text(data.get("research_summary") or data.get("snippet", ""))
    st.json(data, expanded=False)


def results(report):
    st.caption("travelAGENT itinerary" if "selected" in report else "travelAGENT")
    if "question" in report:
        st.write(report["question"])
    if "incomplete" in report:
        st.warning(report["incomplete"])
    evidence = report["evidence"]
    if "selected" in report:
        trip, selected = report["trip"], report["selected"]
        st.subheader(" → ".join([trip["origin"], *trip["destinations"]]))
        st.caption(f"{trip['start']} to {trip['end']} · {trip['adults']} adult(s)")
        for assumption in trip["assumptions"]:
            st.caption("Assumption · " + assumption)
        st.metric(
            "Partial total for the party",
            f"{selected['currency']} {selected['known_total']}"
            if selected["known_total"] is not None
            else "No verified prices",
        )
        for problem in selected["issues"]:
            st.error(problem)
        last_day = None
        for item in selected["items"]:
            day = item["start"][:10]
            if day != last_day:
                st.markdown("#### " + day)
                last_day = day
            with st.expander(item["start"][11:16] + " · " + item["name"]):
                st.write(f"{item['start']} → {item['end']} · {item['timing']} times")
                if item["evidence"]:
                    details(evidence[item["evidence"]])
        if selected["gaps"]:
            with st.expander("Unresolved trip details", expanded=True):
                for gap in selected["gaps"]:
                    st.write("• " + gap)
        if selected["costs"]:
            with st.expander("Itemized costs"):
                st.dataframe(selected["costs"], hide_index=True)
        if report["alternatives"]:
            with st.expander("Alternative schedules"):
                st.json(report["alternatives"], expanded=False)
    with st.expander(f"Sources and search alternatives ({len(evidence)})"):
        for row in evidence.values():
            st.write(row["name"])
            details(row)
    with st.expander("travelAGENT research activity"):
        st.caption(report["model"] + " · " + str(report["api_mode"]))
        st.json(report["trace"], expanded=False)


# This local template contains only fixed branding and document metadata.
header = (assets / "header.html").read_text(encoding="utf-8")
icon_data = base64.b64encode(Path(mark).read_bytes()).decode("ascii")
st.html(header.replace("{{mark}}", icon_data), unsafe_allow_javascript=True)
with st.sidebar:
    st.markdown("### travelAGENT")
    if st.button("New trip"):
        st.session_state.pop("conversation", None)
        st.session_state["turns"] = []
        st.rerun()
    if st.button("Load available models"):
        try:
            response = httpx.get(api + "/models", timeout=30)
            response.raise_for_status()
            st.session_state["models"] = response.json()["models"]
        except httpx.HTTPError:
            st.warning(
                "travelAGENT couldn't load the model list. Enter a model ID or use the server default."
            )
    model = st.selectbox(
        "Planning model",
        st.session_state.get("models", []),
        index=None,
        accept_new_options=True,
        placeholder="Server default",
    )
for prompt, report in st.session_state.setdefault("turns", []):
    st.chat_message("user").write(prompt)
    with st.chat_message("travelAGENT", avatar=mark):
        results(report)
if prompt := st.chat_input("Where would you like to go?"):
    st.chat_message("user").write(prompt)
    with st.chat_message("travelAGENT", avatar=mark):
        try:
            body = {
                "prompt": prompt,
                "conversation": st.session_state.get("conversation"),
                "model": model,
            }
            with st.status(
                "travelAGENT is researching your trip", expanded=True
            ) as progress:
                with httpx.stream(
                    "POST", api + "/plan/stream", json=body, timeout=150
                ) as response:
                    if response.status_code == 404:
                        raise ValueError(
                            "The connected backend is missing the trip streaming endpoint. "
                            "Restart the travelAGENT API from this project, then retry your request."
                        )
                    response.raise_for_status()
                    report = None
                    for line in response.iter_lines():
                        event = json.loads(line)
                        if event["event"] == "progress":
                            e = event["data"]
                            progress.write(
                                e["tool"].replace("_", " ")
                                + " · "
                                + e["state"]
                                + " "
                                + str(e.get("message", ""))
                            )
                        elif event["event"] == "error":
                            raise ValueError(event["message"])
                        elif event["event"] == "result":
                            report = event["data"]
                    if report is None:
                        raise ValueError(
                            "Research stream ended before a report arrived."
                        )
                progress.update(
                    label="travelAGENT needs a trip detail"
                    if "question" in report
                    else "travelAGENT research complete",
                    state="complete",
                    expanded=False,
                )
            st.session_state.conversation = report["conversation"]
            st.session_state.turns.append((prompt, report))
            results(report)
        except (httpx.HTTPError, ValueError) as exc:
            st.error(f"travelAGENT couldn't finish this request. {exc}")
