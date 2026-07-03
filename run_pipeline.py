"""Meladon Haymarket demo run (PLACEHOLDER numbers) through the shared
payload-driven pipeline in cre/intake.py — the same path the web API uses.

Usage: python run_pipeline.py [db_path]
"""
import sys
from pathlib import Path
from cre.intake import run_full_pipeline, MELADON_PAYLOAD

OUT = Path(__file__).parent / "outputs"


def run(db_path=":memory:"):
    r = run_full_pipeline(MELADON_PAYLOAD, db_path=db_path, out_dir=OUT)
    return r["con"], r["report"], r["ctx"]


if __name__ == "__main__":
    _, report, _ = run(sys.argv[1] if len(sys.argv) > 1 else ":memory:")
    import json
    print(json.dumps(report, indent=2, default=str))
