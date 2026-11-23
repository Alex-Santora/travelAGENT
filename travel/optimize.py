import itertools
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal

from .geography import distance_km
from .schedule import checked_times


def endpoints(row, slot):
    d = row.details
    if row.kind == "flight":
        segs = d["slices"][slot.slice_index]["segments"]
        return segs[0]["origin"], segs[-1]["destination"]
    if row.kind == "route":
        return d.get("origin"), d.get("destination")
    loc = d.get("location") or d.get("gps_coordinates") or {}
    point = loc.get("geographic_coordinates", loc)
    return point, point


def sourced_web_visit(row, slot):
    excerpt = (
        row.name
        + " "
        + str(row.details.get("snippet") or row.details.get("research_summary") or "")
    )
    return bool(
        slot.web_activity
        and slot.web_activity.casefold() in excerpt.casefold()
        and slot.end.timestamp() - slot.start.timestamp() <= 12 * 3600
    )


def evaluate(candidate, trip, evidence):
    issues, gaps = [], list(candidate.gaps)
    events, intervals, stays, prices = [], [], [], []
    seen_flights, billed_hotels = set(), set()
    rates = {
        (r.details["base"], r.details["target"]): r
        for r in evidence.values()
        if r.kind == "fx"
    }
    total = Decimal(0)
    required = {r.casefold(): False for r in trip.requirements}
    kinds = Counter()
    minutes = 0
    positions = []
    transfers = 0
    for slot in candidate.items:
        row = evidence.get(slot.evidence)
        if not row or row.kind in ("fx", "location", "airport"):
            raise ValueError("Use schedulable evidence IDs from this conversation.")
        if (
            row.kind == "place"
            and not row.details.get("timeZone")
            and row.details.get("location")
        ):
            point = row.details["location"]
            zones = [
                (distance_km(point, r.details), r.details["timezone"])
                for r in evidence.values()
                if r.kind == "location"
                and r.details.get("timezone")
                and r.details.get("latitude") is not None
            ]
            if zones and min(zones)[0] <= 10:
                row = row.model_copy(
                    update={
                        "details": row.details | {"timeZone": {"id": min(zones)[1]}}
                    }
                )
        if row.kind == "web":
            if not sourced_web_visit(row, slot):
                gaps.append(
                    "Research source awaiting a scheduled venue or service: " + row.name
                )
                continue
            row = row.model_copy(update={"name": slot.web_activity})
        start, end, problems = checked_times(slot, row)
        issues.extend(f"{row.name}: {p}" for p in problems)
        if start.date() < trip.start or end.date() > trip.end:
            issues.append(f"{row.name} falls outside the trip dates")
        kinds[row.kind] += 1
        data = row.details
        first, last = endpoints(row, slot)
        if row.kind == "hotel":
            positions.extend(
                [
                    (start.timestamp(), start.timestamp(), row.name, first, last),
                    (end.timestamp(), end.timestamp(), row.name, first, last),
                ]
            )
        else:
            positions.append(
                (start.timestamp(), end.timestamp(), row.name, first, last)
            )
        if row.kind == "flight":
            transfers += len(data["slices"][slot.slice_index]["segments"]) - 1
        if row.kind == "route":
            transfers += max(0, len(data.get("transit", [])) - 1)
        if row.kind == "place" and not data.get("regularOpeningHours"):
            gaps.append("Operating hours unverified: " + row.name)
        if row.kind in ("hotel", "flight") and data.get("adults") != trip.adults:
            issues.append(f"{row.name} has different occupancy")
        if row.kind == "hotel":
            if (str(start.date()), str(end.date())) != (
                data["check_in"],
                data["check_out"],
            ):
                issues.append(f"{row.name} does not match searched stay dates")
            if slot.evidence in billed_hotels:
                issues.append("Duplicate hotel stay")
            billed_hotels.add(slot.evidence)
            stays.append((start.date(), end.date()))
        else:
            intervals.append((start.timestamp(), end.timestamp(), row.kind, row.name))
        if row.kind in ("flight", "route"):
            minutes += (end.timestamp() - start.timestamp()) / 60
        event = {
            "evidence": slot.evidence,
            "name": row.name,
            "kind": row.kind,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "timing": "provider" if row.kind == "flight" else "proposed",
        }
        events.append(event)
        for term in required:
            if (
                term in row.name.casefold()
                or term in str(data.get("displayName", "")).casefold()
            ):
                required[term] = True
        if row.kind == "flight":
            if (slot.evidence, slot.slice_index) in seen_flights:
                issues.append("Duplicate flight slice")
            billed = any(i == slot.evidence for i, _ in seen_flights)
            seen_flights.add((slot.evidence, slot.slice_index))
            if billed:
                continue
        if row.amount is None:
            gaps.append(f"Unpriced: {row.name}")
            continue
        amount = row.amount * (trip.adults if row.basis == "per traveler" else 1)
        conversion = None
        if row.currency != trip.currency:
            conversion = rates.get((row.currency, trip.currency))
            if not conversion:
                gaps.append(
                    f"Missing {row.currency}/{trip.currency} rate for {row.name}"
                )
                continue
            amount *= Decimal(str(conversion.details["rate"]))
        total += amount
        prices.append(
            {
                "evidence": slot.evidence,
                "item": row.name,
                "amount": str(row.amount),
                "currency": row.currency,
                "basis": row.basis,
                "status": row.price_status,
                "converted_party_cost": str(amount),
                "exchange_source": conversion.source if conversion else None,
            }
        )
    for ref, _ in seen_flights:
        if sum(e == ref for e, _ in seen_flights) != len(
            evidence[ref].details["slices"]
        ):
            issues.append("Missing leg of selected flight offer")
    for pause in candidate.breaks:
        if (
            pause.end <= pause.start
            or pause.start.date() < trip.start
            or pause.end.date() > trip.end
        ):
            issues.append("Break falls outside valid trip hours")
        intervals.append(
            (pause.start.timestamp(), pause.end.timestamp(), "break", pause.kind)
        )
        events.append(
            {
                "evidence": None,
                "name": pause.kind.title(),
                "kind": "break",
                "start": pause.start.isoformat(),
                "end": pause.end.isoformat(),
                "timing": "proposed",
            }
        )
    positions.sort(key=lambda p: p[0])
    for left, right in itertools.pairwise(positions):
        a, b = left[4], right[3]
        if (
            a
            and b
            and all(
                point.get(k) is not None
                for point in (a, b)
                for k in ("latitude", "longitude")
            )
        ) and distance_km(a, b) > 0.5:
            gaps.append(f"Missing connecting leg between {left[2]} and {right[2]}")
    intervals.sort()
    for last, nxt in itertools.pairwise(intervals):
        if nxt[0] < last[1]:
            issues.append(f"Overlap: {last[3]} and {nxt[3]}")
        elif "flight" in (last[2], nxt[2]) and nxt[0] - last[1] < 3600:
            issues.append(f"Less than one hour for airport procedures near {nxt[3]}")
        elif (
            all(k not in ("flight", "route", "break") for k in (last[2], nxt[2]))
            and last[3] != nxt[3]
        ):
            gaps.append(f"Travel time unverified between {last[3]} and {nxt[3]}")
    stays.sort()
    if any(nxt[0] < last[1] for last, nxt in itertools.pairwise(stays)):
        issues.append("Overlapping hotel reservations")
    for term, matched in required.items():
        if not matched:
            gaps.append("Requires confirmation: " + term)
    covered_days = {
        event["start"][:10]
        for event in events
        if event["kind"] not in ("hotel", "break")
    }
    missing_days = (trip.end - trip.start).days + 1 - len(covered_days)
    if missing_days > 0:
        gaps.append(f"{missing_days} trip day(s) have no sourced visit or journey")
    covered_nights = set()
    for arrival, departure in stays:
        for i in range(min((departure - arrival).days, 366)):
            covered_nights.add(arrival + timedelta(days=i))
    if stays and len(covered_nights) < (trip.end - trip.start).days:
        gaps.append(
            "Some nights have no confirmed accommodation or overnight transport"
        )
    if trip.end > trip.start and not stays:
        gaps.append("No dated accommodation selected")
    if not kinds["route"]:
        gaps.append("Local transfers and travel times unverified")
    if len(trip.destinations) > 1 and not kinds["route"] and not kinds["flight"]:
        gaps.append("Intercity transportation missing")
    anchors = {}
    for city in trip.destinations:
        matches = [
            r
            for r in evidence.values()
            if r.kind == "location"
            and r.name.casefold() == city.split(",")[0].strip().casefold()
        ]
        visited = [
            p
            for leg in positions
            for p in leg[3:5]
            if p and p.get("latitude") is not None and p.get("longitude") is not None
        ]
        known = [
            r for r in matches if any(distance_km(r.details, p) < 60 for p in visited)
        ]
        if not known:
            gaps.append("No geographically verified itinerary stop in " + city)
        else:
            anchors[city] = known[0].details
    for src, dst in itertools.pairwise(trip.destinations):
        if src not in anchors or dst not in anchors:
            gaps.append(f"Transport unverified between {src} and {dst}")
            continue
        linked = False
        for slot in candidate.items:
            row = evidence[slot.evidence]
            if row.kind not in ("flight", "route"):
                continue
            a, b = endpoints(row, slot)
            if all(
                p and p.get("latitude") is not None and p.get("longitude") is not None
                for p in (a, b)
            ) and (
                distance_km(a, anchors[src]) < 60 and distance_km(b, anchors[dst]) < 60
            ):
                linked = True
                break
        if not linked:
            gaps.append(f"No routed or quoted journey from {src} to {dst}")
    if not events:
        gaps.append("No sourced schedule items")
    if trip.budget and total > trip.budget:
        issues.append("Known costs exceed the trip budget")
    return {
        "items": sorted(
            events, key=lambda e: datetime.fromisoformat(e["start"]).timestamp()
        ),
        "costs": prices,
        "known_total": str(total.quantize(Decimal(".01"))) if prices else None,
        "currency": trip.currency,
        "total_status": "partial",
        "travel_minutes": round(minutes),
        "transfers": transfers,
        "geographically_supported_destinations": list(anchors),
        "issues": list(dict.fromkeys(issues)),
        "gaps": list(dict.fromkeys(gaps)),
    }


def optimize(finish, evidence):
    choices = [evaluate(c, finish.trip, evidence) for c in finish.candidates]

    totals = [
        Decimal(c["known_total"]) for c in choices if c["known_total"] is not None
    ]
    times = [c["travel_minutes"] for c in choices]

    def normalized(value, values):
        if not values or max(values) == min(values):
            return Decimal(0)
        return Decimal(value - min(values)) / Decimal(max(values) - min(values))

    def score(c):
        cost = (
            Decimal(c["known_total"])
            if c["known_total"] is not None
            else Decimal("Infinity")
        )
        if finish.priority == "balanced":
            combined = (
                normalized(cost, totals) if cost.is_finite() else Decimal(1)
            ) + normalized(c["travel_minutes"], times)
            effort = (combined, c["transfers"], cost)
        elif finish.priority == "time":
            effort = (c["travel_minutes"], c["transfers"], cost)
        else:
            effort = (cost, c["travel_minutes"], c["transfers"])
        return (len(c["issues"]), len(c["gaps"]), *effort)

    choices.sort(key=score)
    trip = finish.trip.model_dump(mode="json")
    if (
        finish.trip.open_destination
        and choices[0]["geographically_supported_destinations"]
    ):
        trip["destinations"] = choices[0]["geographically_supported_destinations"]
    return {
        "trip": trip,
        "selected": choices[0],
        "alternatives": choices[1:],
    }
