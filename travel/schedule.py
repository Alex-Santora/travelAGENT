import itertools
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


def airport_time(segment, field, airport):
    stamp = datetime.fromisoformat(segment[field])
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=ZoneInfo(segment[airport]["time_zone"]))
    return stamp


def inside_hours(hours, start, end):
    periods = hours.get("periods")
    if periods is None:
        return None
    for period in periods:
        first, last = period["open"], period.get("close")
        if last is None:
            return (
                True
                if first.get("day") == 0
                and not first.get("hour")
                and not first.get("minute")
                else None
            )
        # Google numbers Sunday as zero. Include a period starting the previous week.
        sunday = start.date() - timedelta(days=(start.weekday() + 1) % 7)
        for offset in (0, -7):

            def boundary(point, sunday=sunday, offset=offset):
                day = sunday + timedelta(days=offset + point["day"])
                return datetime.combine(day, datetime.min.time(), start.tzinfo).replace(
                    hour=point.get("hour", 0), minute=point.get("minute", 0)
                )

            opening, closing = boundary(first), boundary(last)
            if closing <= opening:
                closing += timedelta(days=7)
            if opening <= start and end <= closing:
                return True
    return False


def checked_times(slot, row):
    start, end = slot.start, slot.end
    details = row.details
    issues = []
    if row.kind == "hotel":
        hours = details.get("check_in_information") or {}
        after = hours.get("check_in_after_time")
        before = hours.get("check_in_before_time")
        if after and start.time() < time.fromisoformat(after):
            issues.append("Arrival before hotel check-in opens")
        if (
            before
            and (not after or before > after)
            and start.time() > time.fromisoformat(before)
        ):
            issues.append("Arrival after hotel check-in closes")
        if hours.get("check_out_before_time") and end.time() > time.fromisoformat(
            hours["check_out_before_time"]
        ):
            issues.append("Departure after hotel check-out deadline")
    if row.kind == "flight":
        segments = details["slices"][slot.slice_index]["segments"]
        start = airport_time(segments[0], "departing_at", "origin")
        end = airport_time(segments[-1], "arriving_at", "destination")
        for prev, nxt in itertools.pairwise(segments):
            gap = (
                airport_time(nxt, "departing_at", "origin").timestamp()
                - airport_time(prev, "arriving_at", "destination").timestamp()
            )
            if (
                gap < 1800
                or prev["destination"]["iata_code"] != nxt["origin"]["iata_code"]
            ):
                issues.append("Flight connection needs manual transfer review")
    if row.kind == "place":
        tz = details.get("timeZone") or {}
        if tz.get("id"):
            zone = ZoneInfo(tz["id"])
            if start.utcoffset() != start.astimezone(zone).utcoffset():
                issues.append("Proposed time zone differs from venue time zone")
            start, end = start.astimezone(zone), end.astimezone(zone)
        if details.get("businessStatus", "OPERATIONAL") != "OPERATIONAL":
            issues.append("Venue marked closed")
        if inside_hours(details.get("regularOpeningHours", {}), start, end) is False:
            issues.append("Visit is outside published opening hours")
    if (
        row.kind == "route"
        and end.timestamp() - start.timestamp() + 1 < details["seconds"]
    ):
        issues.append("Time allotted is shorter than the provider route")
    if end.timestamp() <= start.timestamp():
        issues.append("End must follow start")
    return start, end, issues


def fill_visit_days(finish, evidence, distance):
    """Propose visits only from researched venues in the requested destinations."""
    from .models import Slot

    trip = finish.trip
    span = (trip.end - trip.start).days
    if (
        not (trip.daily_visits or trip.open_destination)
        or span < 2
        or span > 60
        or not trip.destinations
    ):
        return
    cities = []
    for name in trip.destinations:
        matches = [
            r
            for r in evidence.values()
            if r.kind == "location"
            and r.name.casefold() == name.split(",")[0].strip().casefold()
            and r.details.get("timezone")
        ]
        if not matches:
            return
        # Ambiguous names need support from nearby researched venues.
        options = []
        for city in matches:
            nearby = []
            for ref, place in evidence.items():
                d = place.details
                point = d.get("location")
                if (
                    place.kind != "place"
                    or not point
                    or point.get("latitude") is None
                    or point.get("longitude") is None
                ):
                    continue
                category = d.get("osm_value") or d.get("primaryType")
                if category not in {
                    "golf_course",
                    "museum",
                    "art_gallery",
                    "gallery",
                    "tourist_attraction",
                    "attraction",
                    "castle",
                    "park",
                    "garden",
                    "botanical_garden",
                    "restaurant",
                    "aquarium",
                    "temple",
                    "shrine",
                    "place_of_worship",
                    "historical_landmark",
                    "monument",
                    "art_museum",
                    "market",
                    "viewpoint",
                    "theme_park",
                    "ski_resort",
                }:
                    continue
                if distance(city.details, point) <= 40:
                    nearby.append((ref, place, category))
            options.append((city, nearby))
        city, nearby = max(options, key=lambda match: len(match[1]))
        cities.append((city, nearby))
    added = False
    for candidate in finish.candidates:
        used = {s.evidence for s in candidate.items}
        names = {evidence[i].name.casefold() for i in used if i in evidence}
        for day in range(1, span):
            date = trip.start + timedelta(days=day)
            if any(
                s.start.date() <= date <= s.end.date()
                for s in candidate.items
                if evidence.get(s.evidence) and evidence[s.evidence].kind != "hotel"
            ):
                continue
            index = min(len(cities) - 1, (day - 1) * len(cities) // max(1, span - 1))
            city, venues = cities[index]
            hotels = [
                evidence[s.evidence]
                for s in candidate.items
                if s.evidence in evidence
                and evidence[s.evidence].kind == "hotel"
                and s.start.date() <= date < s.end.date()
            ]
            if hotels and not any(
                distance(city.details, h.details["center"]) <= 40 for h in hotels
            ):
                continue
            zone = ZoneInfo(city.details["timezone"])

            def priority(entry, used=used):
                words = entry[2].replace("_", " ").split()
                uses = sum(
                    evidence[k].details.get(
                        "osm_value", evidence[k].details.get("primaryType")
                    )
                    == entry[2]
                    for k in used
                    if k in evidence
                )
                return (
                    uses,
                    not any(
                        w in req.casefold() for req in trip.requirements for w in words
                    ),
                    entry[2] == "restaurant",
                )

            for hour in (10, 15):
                for ref, venue, category in sorted(venues, key=priority):
                    if category == "golf_course" and any(
                        evidence[k].details.get(
                            "osm_value", evidence[k].details.get("primaryType")
                        )
                        == "golf_course"
                        for k in used
                        if k in evidence
                    ):
                        continue
                    if ref in used or venue.name.casefold() in names:
                        continue
                    start = datetime.combine(date, time(hour), zone)
                    end = start + timedelta(
                        hours=4 if category == "golf_course" else 1.5
                    )
                    if (
                        inside_hours(
                            venue.details.get("regularOpeningHours", {}), start, end
                        )
                        is False
                    ):
                        continue
                    if any(p.start < end and p.end > start for p in candidate.breaks):
                        continue
                    candidate.items.append(Slot(evidence=ref, start=start, end=end))
                    used.add(ref)
                    names.add(venue.name.casefold())
                    added = True
                    break
    if added:
        trip.assumptions.append(
            "Unscheduled interior days use researched nearby venues, split across the requested city order. Visits allow 90 minutes, or four hours for golf. Times and reservations require confirmation."
        )
