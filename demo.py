"""Exercise the same planning entry point as the API."""

import argparse
import json

from travel.agent import plan
from travel.request import Submission
from travel.scenarios import SCENARIOS

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Research an itinerary with travelAGENT."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--scenario", choices=SCENARIOS)
    group.add_argument("--prompt")
    parser.add_argument("--model")
    args = parser.parse_args()
    report = plan(
        Submission(
            prompt=SCENARIOS[args.scenario] if args.scenario else args.prompt,
            model=args.model,
        )
    )
    print(json.dumps(report, indent=2, ensure_ascii=True))
