from datetime import datetime, time, timedelta

from .models import Event


def minutes(n):
    return timedelta(minutes=n)


def link(a, b, travelers):
    distance = sum(abs(x - y) for x, y in zip(a, b))
    if distance <= 18:
        return "Walk", distance, 0
    if distance <= 48:
        return "Local transit", 10 + distance // 3, 300 * travelers
    return "Regional shuttle", 15 + distance // 2, 2600 * travelers


def generate_schedule(t, ride, hotel, activities):
    events = []

    def add(start, duration, title, category="buffer", cents=0):
        end = start + minutes(duration)
        if duration:
            events.append(
                Event(start=start, end=end, title=title, category=category, cents=cents)
            )
        return end

    depart, arrive = ride.outbound
    back, home = ride.inbound
    access = ride.access_minutes
    check = ride.checkin_minutes
    access_cost = ride.access_cents_pp * t.travelers
    fare = ride.fare_each_way_cents * t.travelers
    add(
        depart - minutes(access + check),
        access,
        "City center to origin terminal",
        "transfers",
        access_cost,
    )
    add(depart - minutes(check), check, "Boarding buffer")
    add(
        depart,
        (arrive - depart) // minutes(1),
        f"{ride.provider} to {ride.terminal}",
        "primary",
        fare,
    )
    base = hotel.location if t.nights else ride.location
    base_name = hotel.name if t.nights else ride.terminal
    method, duration, cost = link(ride.location, base, t.travelers)
    add(arrive, duration, f"{method} to {base_name}", "transfers", cost)
    ready = arrive + minutes(duration)
    deadline = back - minutes(check + duration)
    available = list(activities)
    matches = visits = usable = 0
    for offset in range(t.nights + 1):
        day = t.departure + timedelta(days=offset)
        cursor = ready if not offset else datetime.combine(day, time(9))
        end = deadline if day == t.returning else datetime.combine(day, time(19))
        if cursor > end:
            raise ValueError("Terminal connections cannot fit this schedule.")
        usable += (end - cursor) // minutes(1)
        point, place = base, base_name
        # Short anchor visits leave space for meals, optional exploration and rest.
        for _ in range(2):
            choices = []
            for a in available:
                leg = link(point, a.location, t.travelers)
                start = max(
                    cursor + minutes(leg[1]), datetime.combine(day, time(a.opens))
                )
                finish = start + minutes(a.duration)
                home_leg = link(a.location, base, t.travelers)
                if (
                    finish > datetime.combine(day, time(a.closes))
                    or finish + minutes(home_leg[1] + 15) > end
                ):
                    continue
                choices.append(
                    (
                        (
                            a.interest not in t.interests,
                            (start - cursor) // minutes(1),
                            a.name,
                        ),
                        a,
                        leg,
                        start,
                        finish,
                    )
                )
            if not choices:
                break
            _, activity, (method, duration_leg, cost_leg), start, finish = min(
                choices, key=lambda x: x[0]
            )
            cursor = add(
                cursor,
                duration_leg,
                f"{method} from {place} to {activity.name}",
                "local",
                cost_leg,
            )
            add(cursor, (start - cursor) // minutes(1), "Free time before opening")
            add(
                start,
                activity.duration,
                activity.name,
                "activities",
                activity.admission_cents * t.travelers,
            )
            cursor = add(finish, 15, "Break")
            point, place = activity.location, activity.name
            available.remove(activity)
            visits += 1
            matches += activity.interest in t.interests
        method_local, dur_local, price_local = link(point, base, t.travelers)
        cursor = add(
            cursor,
            dur_local,
            f"{method_local} back to {base_name}",
            "local",
            price_local,
        )
        add(cursor, (end - cursor) // minutes(1), "Free time and meals")
    add(deadline, duration, f"{method} to {ride.terminal}", "transfers", cost)
    add(back - minutes(check), check, "Boarding buffer")
    add(
        back,
        (home - back) // minutes(1),
        f"{ride.provider} to New York City",
        "primary",
        fare,
    )
    add(home, access, "Origin terminal to city center", "transfers", access_cost)
    return events, usable, visits, matches
