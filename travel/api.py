import json
import os
from queue import Queue
from threading import Thread

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from openai import APIError

from . import llm
from .agent import PlanningError, plan
from .request import Submission

app = FastAPI(
    title="travelAGENT",
    description="AI travel planning with sourced itineraries and live research activity.",
    version="2.0",
)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "configured": {
            name: bool(os.getenv(name))
            for name in (
                "OPENAI_API_KEY",
                "DUFFEL_ACCESS_TOKEN",
                "GOOGLE_MAPS_API_KEY",
                "SERPAPI_API_KEY",
            )
        },
    }


@app.get("/models")
def models():
    try:
        with llm.client() as client:
            return {"models": sorted(model.id for model in client.models.list())}
    except (ValueError, APIError):
        raise HTTPException(
            503, detail="Model discovery unavailable. Enter a model ID manually."
        ) from None


@app.post("/plan")
def create_plan(submission: Submission):
    try:
        return plan(submission)
    except PlanningError as exc:
        raise HTTPException(exc.status, detail=str(exc)) from None


@app.post("/plan/stream")
def stream_plan(submission: Submission):
    queue = Queue()

    def worker():
        try:
            result = plan(
                submission,
                lambda event: queue.put({"event": "progress", "data": event}),
            )
            queue.put({"event": "result", "data": result})
        except PlanningError as exc:
            queue.put({"event": "error", "message": str(exc)})
        except Exception:  # noqa: BLE001 - end the stream without exposing provider internals
            queue.put({"event": "error", "message": "Unexpected backend error."})
        finally:
            queue.put(None)

    def lines():
        Thread(target=worker, daemon=True).start()
        while (item := queue.get()) is not None:
            yield json.dumps(item) + "\n"

    return StreamingResponse(
        lines(), media_type="application/x-ndjson", headers={"X-Accel-Buffering": "no"}
    )
