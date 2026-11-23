from fastapi import FastAPI, HTTPException

from .agent import PlanningError, plan
from .models import Report
from .request import Submission

app = FastAPI(title="Wayledger travel planner", version="1.0")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/plan", response_model=Report)
def create_plan(submission: Submission):
    try:
        return plan(submission)
    except PlanningError as exc:
        raise HTTPException(
            exc.status,
            detail={"message": str(exc), "trace": [e.model_dump() for e in exc.trace]},
        ) from exc
