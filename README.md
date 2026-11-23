# Wayledger

Wayledger is an M3 engineering prototype that uses an LLM to plan complete trips from New York City to Boston or Philadelphia. It compares round-trip services, terminal transfers, hotel nights, local travel, visits, meals and lodging tax. All travel inventory is simulated.

## Run locally on Windows

From the repository root, install the dependencies and configure the backend.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

Set `OPENAI_API_KEY` in the new file. Select a Chat Completions model with native function calling via `OPENAI_MODEL`. Start the API.

```powershell
.\.venv\Scripts\python -m uvicorn travel.api:app --host 127.0.0.1 --port 8000
```

In another terminal in the same directory, start Streamlit.

```powershell
.\.venv\Scripts\python -m streamlit run app.py --server.address 127.0.0.1
```

Open [the planner](http://127.0.0.1:8501). Try “Plan the cheapest three-day trip from NYC to Philadelphia starting April 12, 2027 for one traveler with an $850 total budget. I like history.” Select **Compare trips**. Inspect the constraints, itinerary, cost ledger, alternatives and **Agent execution**. If the agent asks for details, edit the form and resubmit. The [API explorer](http://127.0.0.1:8000/docs) also accepts a prompt and optional overrides.

## Configuration

| Variable | Use |
|---|---|
| OPENAI_API_KEY | Server credential for OpenAI. Requests send the prompt, settings and tool results to that provider |
| OPENAI_MODEL | OpenAI model name, default gpt-4o-mini |
| TRAVEL_LLM_PROVIDER | openai (default) or ollama |
| OLLAMA_MODEL | Required for Ollama. Use an installed model that supports tools, with the local Ollama server running on port 11434 |
| TRAVEL_MAX_ROUNDS | Model turns per plan, default 12, valid range 1–30 |
| TRAVEL_API_URL | UI backend address, default http://127.0.0.1:8000 |

Restart the API after changing provider settings. Ollama uses its local OpenAI-compatible endpoint and a placeholder SDK credential. Provider errors appear with the completed tool trace. The application requires a functioning model to produce a plan.

## How a plan is made

The model extracts the route, dates or duration, party size, USD budget and preferences. Form settings take precedence. The agent asks about missing or conflicting essentials. It requests applicable catalog searches, reads their results, invokes estimation and ranking, then inspects the timed itinerary and selects a verified fact to explain the result. The dispatcher validates native function arguments and returns structured results and errors by call ID. The SDK loop supports batched searches and bounded retries.

Python schedules each eligible service and hotel pair and calculates the party total in integer cents. The optimizer ranks feasible plans ahead of over-budget plans and shows the smallest shortfall when none qualify. The local catalog provides illustrative daily services, rooms and activity hours. Meals and local connections are estimates. All supported cities share Eastern time. Coverage is one to eight travelers and one to fourteen days. The activity heuristic schedules at most two visits per day. See [DESIGN.md](DESIGN.md) for assumptions, ranking weights and milestone evaluation.

## Verification

```powershell
.\.venv\Scripts\python -m pytest -q
```

The suite uses scripted SDK responses for reproducible orchestration tests. To exercise the actual configured provider, enable the live check explicitly.

```powershell
$env:TRAVEL_RUN_LIVE = "1"
.\.venv\Scripts\python -m pytest -q -k live_provider
```

Run the sample requests through the actual agent with the following command. Adding `--save` writes the current scenario reports into `evidence/`. The original files in that directory are historical development fixtures. Their traces and screenshot predate the current agent.

```powershell
.\.venv\Scripts\python demo.py --scenario all
```
