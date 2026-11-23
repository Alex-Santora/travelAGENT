# travelAGENT implementation notes

## Request and data flow

Streamlit sends a natural-language request to FastAPI and renders progress from the running backend. The model interprets travel constraints or asks for essential missing details. It may request targeted web research during interpretation. It selects further tools from a Pydantic registry and receives structured results with evidence IDs. Independent lookups run in a bounded pool. Credentials, upstream error bodies and authentication headers stay outside the model conversation.

The former corridor catalog, city enum, simulated services, flat meal allowances and Manhattan-distance schedule have been removed. Sample prompts in `travel/scenarios.py` are CLI and integration test inputs. The production planner has no scenario selection or destination list.

## Components

| File | Responsibility |
|---|---|
| `travel/request.py`, `travel/models.py` | Submission, constraints, tool arguments, evidence, slots and candidate schemas |
| `travel/llm.py` | Model endpoint negotiation, function-call correlation, validated text actions and repair |
| `travel/catalog.py` | Read-only provider adapters, provenance, inventory, route and web data |
| `travel/agent.py` | Tool dispatch, attempt and concurrency limits, short-lived evidence, chat revisions and progress events |
| `travel/schedule.py`, `travel/optimize.py` | Provider flight times, time-zone conversion, operating hours, transfers, candidate ranking and monetary arithmetic |
| `travel/api.py`, `app.py` | Local API, streaming transport, model settings and conversation UI |

## Trust and feasibility

Only server-issued evidence IDs can become externally sourced items. Rest and free time are traveler schedule proposals. Web results retain the provider's cited URL, retrieval date and a research excerpt. The schedule marks proposed times separately from provider flight times. The ledger reads money exclusively from provider records, scales per-traveler charges, and uses Decimal arithmetic and dated exchange rates. All trip totals are conservative partial totals. Unknown costs remain visible.

Candidate ranking prioritizes fewer validation issues and unresolved gaps. Among comparable proposals it compares known party costs, travel minutes and transfers, respecting the selected priority. For a requested daily itinerary, sparse days can be filled with geographically matched venues. Bounded route lookups connect successive nearby visits. The engine checks dates, party size, overlaps, opening periods, hotel timing, flight connections and the duration of routed legs. A one-hour airport planning buffer and 30-minute connection review threshold are screening heuristics. They do not certify airline-specific minimum connection times. An itinerary with an outstanding issue is displayed for review. Search covers a bounded set of proposals, so global optimality is unverified.

## Operational limits

A turn permits up to 36 actions and five web searches with three concurrent lookups. `TRAVEL_MAX_ROUNDS` defaults to 16. Flight and stay search data are reused for at most five minutes and expire earlier when the supplier specifies an expiry. Other research can be reused for an hour. An expired offer needs a new search. Follow-ups keep up to five recent turns in this single process.

Missing credentials, empty inventory, rate limits, timeouts, invalid model output, ambiguous places and unavailable timetables can leave gaps. The system has no universal rail booking feed. It uses transit directions and sourced operator research. Web prices, optional charges and admission fees that cannot be mapped reliably to a structured monetary basis are excluded from the subtotal. Adult occupancy, available provider geography and supported travel dates constrain inventory access.
