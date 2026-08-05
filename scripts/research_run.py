"""Run the repository research pipeline with credentials loaded from .env.

This wrapper loads environment variables from the repository's ``.env`` file
into the process environment without printing or exposing their values, then
delegates to :func:`scripts.research_repo.main`. The isolated research worker
inherits the loaded environment, so provider credentials are available to
GPT-Researcher without being read into the agent's context.

Example:
    .venv\\Scripts\\python -m scripts.research_run --topic security \\
        --query "Audit billing controls"
"""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

from scripts.research_repo import main as research_main

REPO_ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    """Load .env credentials silently, then run the research pipeline."""
    load_dotenv(REPO_ROOT / ".env", override=False, verbose=False)
    return research_main(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
