"""Run AI-planned examples and optionally save the full machine-readable report."""

import argparse
import json
from pathlib import Path

from travel.agent import PlanningError, plan
from travel.request import Submission
from travel.scenarios import SCENARIOS


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=[*SCENARIOS, "all"], default="all")
    parser.add_argument("--save", action="store_true")
    args = parser.parse_args()
    for key in SCENARIOS if args.scenario == "all" else [args.scenario]:
        prompt = SCENARIOS[key]
        try:
            report = plan(Submission(prompt=prompt))
        except PlanningError as exc:
            parser.exit(1, f"{key} | {exc}\n")
        choice = report.selected
        print(
            f"{key} | {choice.transport.mode} | {choice.hotel.name} | USD {choice.total_cents / 100:.2f} | "
            f"budget balance {choice.remaining_cents / 100:+.2f} | {len(report.trace)} tool calls"
        )
        if args.save:
            folder = Path(__file__).resolve().parent / "evidence"
            folder.mkdir(exist_ok=True)
            data = {"prompt": prompt, **report.model_dump(mode="json")}
            (folder / f"scenario-{key}.json").write_text(
                json.dumps(data, indent=2) + "\n", encoding="utf8"
            )


if __name__ == "__main__":
    main()
