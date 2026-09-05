"""Run the capability API, chatbot and dashboard.

    python scripts/serve.py
    python scripts/serve.py --policy config/policy.yaml --artifacts artifacts
    CUA_HEADLESS=1 python scripts/serve.py      # no visible browser

Headed by default: an operator cannot take over a browser window they cannot
see, and the handoff is a required path rather than an extra.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# The wrapper lives at the repository root rather than inside the installed
# package, so that a diff of the adaptation shows the core untouched. Running
# a file in scripts/ puts scripts/ on the path, not the root -- so put it there,
# along with src/, so the service starts whether or not cua was pip-installed.
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(1, str(_ROOT / "src"))

import uvicorn  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from service.api import create_app  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts"))
    parser.add_argument(
        "--policy", type=Path, default=Path("config/policy.meridian-hosted.yaml")
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    load_dotenv()
    app = create_app(
        artifacts=args.artifacts,
        policy=args.policy,
        headed=not args.headless,
    )
    print(f"  catalog  {len(app.state.catalog)} capabilities from {args.artifacts}")
    print(f"  policy   {args.policy}")
    print(f"  docs     http://{args.host}:{args.port}/docs")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
