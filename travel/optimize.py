from .models import Candidate, Costs
from .schedule import generate_schedule, minutes

MEAL_CENTS = 3500


def estimate_trip_costs(t, transports, hotels, activities):
    if not transports or not hotels:
        raise ValueError("No transportation or lodging inventory for this request.")
    results = []
    for ride in transports:
        for hotel in hotels:
            events, usable, visits, matches = generate_schedule(
                t, ride, hotel, activities
            )
            amounts = {
                name: sum(e.cents for e in events if e.category == name)
                for name in ("primary", "transfers", "local", "activities")
            }
            costs = Costs(
                **amounts,
                accommodation=hotel.room_total_cents,
                fees=hotel.tax_cents,
                meals=MEAL_CENTS * t.travelers * (t.nights + 1),
            )
            travel = sum(
                (e.end - e.start) // minutes(1)
                for e in events
                if e.category in ("primary", "transfers", "local")
                or e.title == "Boarding buffer"
            )
            # Generalized cost in cents. Time and preference weights apply per traveler.
            value = {"cost": 0, "balanced": 10, "time": 50}[t.priority]
            penalties = (
                4000 if t.preferred_mode and ride.mode != t.preferred_mode else 0
            )
            if t.nights and t.lodging and hotel.style != t.lodging:
                penalties += 3500
            quality = (
                value * (travel - usable // 4)
                + penalties
                - 500 * matches
                - value * 10 * visits
            )
            score = (
                costs.total
                if t.priority == "cost"
                else costs.total + quality * t.travelers
            )
            results.append(
                Candidate(
                    transport=ride,
                    hotel=hotel,
                    costs=costs,
                    total_cents=costs.total,
                    remaining_cents=t.budget_cents - costs.total,
                    feasible=costs.total <= t.budget_cents,
                    score=score,
                    travel_minutes=travel,
                    destination_minutes=usable,
                    events=events,
                    visits=visits,
                    interest_matches=matches,
                )
            )
    return results


def compare_trip_options(candidates):
    return sorted(
        candidates,
        key=lambda c: (
            not c.feasible,
            max(0, -c.remaining_cents),
            c.score,
            c.total_cents,
            c.transport.mode,
            c.hotel.name,
        ),
    )


def summary(c):
    row = c.model_dump(exclude={"events", "transport", "hotel"})
    return dict(
        row, mode=c.transport.mode, provider=c.transport.provider, hotel=c.hotel.name
    )


def generate_itinerary(ranked):
    best = ranked[0]
    runner = ranked[1] if len(ranked) > 1 else None
    if best.feasible:
        why = f"Lowest weighted score among {sum(c.feasible for c in ranked)} within-budget combinations."
    else:
        why = f"All {len(ranked)} combinations exceed the budget. This one has the smallest shortfall."
    if runner:
        gap = (runner.total_cents - best.total_cents) / 100
        delta = runner.travel_minutes - best.travel_minutes
        why += (
            f" The next choice ({runner.transport.mode}, {runner.hotel.name}) differs by "
            f"{gap:+.2f} USD and {delta:+d} travel minutes relative to this plan."
        )
    if best.feasible:
        why += (
            f" The selected trip includes {best.visits} visits, {best.travel_minutes} minutes "
            f"in transit or boarding, and ${best.costs.transfers / 100:.2f} in terminal transfers."
        )
    return best, why
