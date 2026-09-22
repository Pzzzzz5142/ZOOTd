"""Explicit experimental retrieval only; never starts MAA or the game."""
import argparse
import json
import sys
from pathlib import Path

from .prts import PrtsCopilotClient, PrtsError, StageCatalog


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    query = commands.add_parser("copilot-query")
    query.add_argument("stage")
    query.add_argument("--page", type=int, default=1)
    query.add_argument("--limit", type=int, default=10)
    get = commands.add_parser("copilot-get")
    get.add_argument("copilot_id", type=int)
    get.add_argument("--stage", required=True)
    args = parser.parse_args(argv)
    try:
        catalog = StageCatalog.load(args.project_root / "var/data/MaaResource/resource/stages.json")
        client = PrtsCopilotClient(catalog)
        if args.command == "copilot-query":
            output = client.query(args.stage, page=args.page, limit=args.limit)
        else:
            output = client.get(args.copilot_id, stage=args.stage)
        print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except PrtsError as exc:
        print(json.dumps({"status": "error", "category": exc.category, "message": str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
