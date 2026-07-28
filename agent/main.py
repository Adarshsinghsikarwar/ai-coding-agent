"""
main.py
-------
CLI entrypoint. Orchestrates the 4-phase workflow:

    EXPLORE -> PLAN -> IMPLEMENT -> SUMMARIZE

and writes an artifact (Markdown) for each phase into <repo>/agent_artifacts/,
so a human reviewer can inspect exactly what the agent understood, decided,
and did - at every step, not just the final diff.

Usage:
    python -m agent.main --repo /path/to/node-easy-notes-app \\
        --request "Improve the application so users can better organise and search their notes."
"""

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path

from .core_agent import Agent
from .prompts import (
    EXPLORE_SYSTEM_PROMPT,
    PLAN_SYSTEM_PROMPT,
    IMPLEMENT_SYSTEM_PROMPT,
    SUMMARY_SYSTEM_PROMPT,
)

DEFAULT_REQUEST = "Improve the application so users can better organise and search their notes."


def _write_artifact(repo_root: str, filename: str, content: str) -> None:
    out_dir = Path(repo_root) / "agent_artifacts"
    out_dir.mkdir(exist_ok=True)
    # Force UTF-8 explicitly - on Windows, write_text() without this defaults
    # to the system codepage (often cp1252), which crashes on unicode
    # characters the model may output (em-dashes, minus signs, etc.).
    (out_dir / filename).write_text(content, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Run the AI coding agent against a repo.")
    parser.add_argument("--repo", required=True, help="Path to the target repository")
    parser.add_argument("--request", default=DEFAULT_REQUEST, help="Product request (one line)")
    args = parser.parse_args()

    if "MISTRAL_API_KEY" not in os.environ:
        raise SystemExit("Set MISTRAL_API_KEY in your environment first.")

    agent = Agent(repo_root=args.repo)
    started = datetime.now(timezone.utc).isoformat()

    # ---------- Phase 1: Explore ----------
    print("\n=== PHASE 1/4: EXPLORE ===")
    exploration_summary = agent.run_phase(
        system_prompt=EXPLORE_SYSTEM_PROMPT,
        user_message=(
            "Explore this repository from scratch. Start with list_directory('.'), then "
            "read the README and package manifest, then dig into whatever is relevant to "
            "this product request:\n\n"
            f'"{args.request}"'
        ),
    )
    _write_artifact(args.repo, "01_exploration.md", exploration_summary)
    print(exploration_summary)

    # ---------- Phase 2: Plan ----------
    print("\n=== PHASE 2/4: PLAN ===")
    plan = agent.run_phase(
        system_prompt=PLAN_SYSTEM_PROMPT,
        user_message=(
            f"Product request:\n\"{args.request}\"\n\n"
            f"Exploration summary of the codebase:\n\n{exploration_summary}"
        ),
        allow_tools=False,
    )
    _write_artifact(args.repo, "02_plan.md", plan)
    print(plan)

    # ---------- Phase 3: Implement ----------
    print("\n=== PHASE 3/4: IMPLEMENT ===")
    implement_max_turns = int(os.environ.get("AGENT_IMPLEMENT_MAX_TURNS", "45"))
    implementation_log = agent.run_phase(
        system_prompt=IMPLEMENT_SYSTEM_PROMPT,
        user_message=(
            f"Here is the approved plan. Implement it now using your file tools.\n\n{plan}"
        ),
        max_turns=implement_max_turns,
    )
    _write_artifact(args.repo, "03_implementation_log.md", implementation_log)
    print(implementation_log)
    if implementation_log.startswith("ERROR: hit MAX_TURNS"):
        print(
            "\n[WARNING] Implement phase hit its turn limit before finishing. "
            "Some file changes may be partial or missing - check "
            "`git diff` inside the repo, and see agent_artifacts/03_implementation_log.md "
            "for what it was doing when it ran out of turns.\n"
        )

    # ---------- Phase 4: Summarize ----------
    print("\n=== PHASE 4/4: SUMMARIZE ===")
    summary = agent.run_phase(
        system_prompt=SUMMARY_SYSTEM_PROMPT,
        user_message="Summarize the changes you just made to the repository.",
    )
    _write_artifact(args.repo, "04_summary.md", summary)
    print(summary)

    finished = datetime.now(timezone.utc).isoformat()
    _write_artifact(
        args.repo,
        "run_metadata.md",
        f"# Run Metadata\n\nRequest: {args.request}\nStarted: {started}\nFinished: {finished}\n",
    )
    print(f"\nAll done. Artifacts written to {args.repo}/agent_artifacts/")


if __name__ == "__main__":
    main()
