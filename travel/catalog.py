"""Fictional service patterns. Grid units represent approximate walking minutes."""

from datetime import datetime, time, timedelta

from .models import Activity, Hotel, Transport, Trip

CITIES = {
    "Boston": {
        "rail": ("South Station", (6, 3), 245, 4600),
        "bus": ("South Station coach hall", (7, 4), 285, 3200),
        "air": ("Outer Boston regional terminal", (47, 42), 75, 2400),
        "hotels": [
            ("Harbor House", "Waterfront", (4, 3), 11900),
            ("Fenway Guesthouse", "Fenway", (23, 12), 8800),
        ],
        "activities": [
            ("Freedom Trail walk", "Downtown", (5, 4), "history", 90, 0, 9, 17),
            ("Harborwalk", "Waterfront", (3, 2), "outdoors", 60, 0, 8, 18),
            ("Museum of Fine Arts", "Fenway", (22, 11), "art", 100, 2500, 10, 17),
            ("Science galleries", "West End", (12, 9), "science", 90, 2200, 10, 17),
            ("Public Garden", "Back Bay", (11, 6), "outdoors", 60, 0, 8, 18),
            ("Old State House", "Downtown", (6, 5), "history", 60, 1200, 10, 17),
        ],
    },
    "Philadelphia": {
        "rail": ("30th Street Station", (11, 7), 95, 3600),
        "bus": ("Center City coach stop", (6, 6), 145, 2300),
        "air": ("Regional air terminal", (37, 30), 65, 2100),
        "hotels": [
            ("Market Street Inn", "Old City", (4, 5), 10800),
            ("University Lodge", "University City", (19, 11), 7900),
        ],
        "activities": [
            ("Independence district walk", "Old City", (4, 4), "history", 90, 0, 9, 17),
            ("River trail", "Schuylkill Banks", (14, 8), "outdoors", 60, 0, 8, 18),
            ("Art museum galleries", "Fairmount", (16, 12), "art", 100, 2300, 10, 17),
            (
                "Franklin science exhibits",
                "Logan Square",
                (12, 10),
                "science",
                90,
                2400,
                10,
                17,
            ),
            (
                "Reading Terminal Market walk",
                "Center City",
                (7, 6),
                "history",
                60,
                0,
                9,
                17,
            ),
        ],
    },
}


def services(t: Trip, modes):
    rows = []
    for mode in modes:
        terminal, point, duration, fare = CITIES[t.destination][mode]
        leave = datetime.combine(t.departure, time(8))
        back = datetime.combine(t.returning, time(18))
        ride = timedelta(minutes=duration)
        air = mode == "air"
        rows.append(
            Transport(
                mode=mode,
                provider={
                    "air": "Corridor Air",
                    "bus": "Atlantic Coach",
                    "rail": "Northeast Rail",
                }[mode],
                origin=t.origin,
                destination=t.destination,
                terminal=terminal,
                location=point,
                outbound=(leave, leave + ride),
                inbound=(back, back + ride),
                fare_each_way_cents=fare,
                party_roundtrip_cents=2 * fare * t.travelers,
                access_minutes=45 if air else 15,
                access_cents_pp=1200 if air else 300,
                checkin_minutes=60 if air else 15,
                restrictions="Personal item only" if air else "One carry-on included",
            )
        )
    return rows


def search_flights(t):
    return services(t, ["air"])


def search_ground_transport(t):
    return services(t, ["rail", "bus"])


def search_hotels(t):
    rows = []
    for style, (name, district, point, rate) in zip(
        ("central", "value"), CITIES[t.destination]["hotels"]
    ):
        rooms = (t.travelers + 1) // 2 if t.nights else 0
        subtotal = rooms * t.nights * rate
        rows.append(
            Hotel(
                name=name,
                style=style,
                district=district,
                location=point,
                nightly_cents=rate,
                rooms=rooms,
                nights=t.nights,
                room_total_cents=subtotal,
                tax_cents=(subtotal * 12 + 50) // 100,
            )
        )
        if not t.nights:
            break
    return rows


def search_activities(t):
    return [
        Activity(
            **dict(
                zip(
                    (
                        "name",
                        "district",
                        "location",
                        "interest",
                        "duration",
                        "admission_cents",
                        "opens",
                        "closes",
                    ),
                    row,
                )
            )
        )
        for row in CITIES[t.destination]["activities"]
    ]
