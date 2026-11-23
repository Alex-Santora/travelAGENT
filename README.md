# travelAGENT

**M3 · Track A · Working prototype**

travelAGENT is a read-only AI trip planner. Describe a trip in Streamlit and revise it in the same chat. The FastAPI backend asks for missing essentials, calls travel and research tools, then returns a timed itinerary with sources, alternatives, a partial party cost, and any unresolved details. See [DESIGN.md](DESIGN.md) for the architecture, scheduling rules, and provider assumptions.

## M3 rubric

| Criterion | Prototype evidence |
| --- | --- |
| M3.1 · Core functionality (20) | A natural-language request runs through clarification, provider lookups, schedule checks, and ranking. The chat supports follow-up revisions. |
| M3.2 · Component scope (15) | The planner includes the UI, API, model tool loop, travel inventory, scheduler, cost ledger, and execution trace. The earlier checked-in design is the comparison baseline. A standalone M2 proposal is unavailable in this repository. |
| M3.3 · Execution evidence (25) | Run the app or CLI below. The UI shows research activity, item sources, and gaps. CLI output includes the same trace and evidence as JSON. The checked-in `evidence/` files document the earlier simulation. |
| M3.4 · Engineering rigor (20) | Pydantic validates tool inputs. The agent bounds lookups and handles provider failures. Python checks schedule timing and computes costs from provider records. Tests cover call protocols, errors, scheduling, and missing prices. |
| M3.5 · Architecture alignment (10) | Streamlit, FastAPI, and the tool-calling loop carry over from the earlier design. Provider adapters replace the two-route simulation and estimated allowances, expanding coverage and making price gaps visible. The model layer now also supports validated JSON actions. |
| M3.6 · Documentation (10) | This README gives setup and inspection steps. [DESIGN.md](DESIGN.md) describes the implementation and tradeoffs. |

## MVP gaps and next steps

A saved local Japan run recorded 16 returned tool events, 13 unavailable events, and 19 unresolved details. The CLI below can generate a fresh report from the configured providers.

- **Coverage and cost.** Rail fares, local transit schedules, fees, and some flight or hotel prices can be missing. A partial subtotal under budget leaves affordability unresolved. Next, improve rail and transit sourcing and measure fare coverage across the demo trips.
- **Feasibility and search.** Opening hours, future timetables, and connection buffers can leave a schedule needing review. The planner ranks a limited set of candidates. Next, validate transfer windows and day-by-day timing against provider data on multi-city trips.
- **Reliability.** Provider quotas, timeouts, and model variability can interrupt research. Chat history lives in one backend process and expires after one idle hour. Next, capture repeatable live traces and track latency, successful lookups, and unresolved items across runs.

## Run locally

From the repository root in PowerShell, use Python 3.13 and create a virtual environment. The copy command preserves an existing `.env`.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
if (!(Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
```

Set `OPENAI_API_KEY` to an API key with billing access. The example uses `gpt-4.1-mini`. For dated flight and hotel results, add `SERPAPI_API_KEY` for published prices or a live `DUFFEL_ACCESS_TOKEN` for inventory quotes. Duffel hotel quotes also require Stays access. SerpApi takes precedence when configured. `GOOGLE_MAPS_API_KEY` adds Places and Routes coverage. Restart the backend after editing `.env`. To use a running local Ollama server, set `TRAVEL_LLM_PROVIDER=ollama` and `OLLAMA_MODEL` to an installed model.

Start the API, then start Streamlit from a second terminal in the repository root.

```powershell
.\.venv\Scripts\python -m uvicorn travel.api:app --host 127.0.0.1 --port 8000
```

```powershell
.\.venv\Scripts\python -m streamlit run app.py --server.address 127.0.0.1
```

Open the [planner](http://127.0.0.1:8501). Try “Plan a trip from Boston to Tokyo for two adults, April 12–19, 2027, with a total budget of $5,000. Include flights, hotels, and sightseeing.” Open **travelAGENT research activity** and expand individual itinerary items to inspect the trace and sources. The [API explorer](http://127.0.0.1:8000/docs) also exposes `POST /plan`.

Run the default test suite without live credentials. The CLI demo uses configured providers and can incur API charges.

```powershell
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python demo.py --scenario A
```
