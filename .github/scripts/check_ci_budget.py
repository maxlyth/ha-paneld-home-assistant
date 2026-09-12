"""Fail when a workflow's recent median run time exceeds its budget.

CI slowdown is gradual, so nobody notices it in the run they are looking at.
This turns it into something that fails on a schedule instead: the median of
recent successful runs on main, per workflow, against a recorded ceiling.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

_API = "https://api.github.com"
_TIMESTAMP = "%Y-%m-%dT%H:%M:%SZ"


def run_seconds(run: dict[str, Any]) -> float | None:
    """Return one run's wall-clock seconds, or None when it cannot be told."""
    started = run.get("run_started_at")
    finished = run.get("updated_at")
    if not started or not finished:
        return None
    try:
        elapsed = datetime.strptime(finished, _TIMESTAMP) - datetime.strptime(
            started, _TIMESTAMP
        )
    except ValueError:
        return None
    seconds = elapsed.total_seconds()
    return seconds if seconds >= 0 else None


def median_seconds(runs: list[dict[str, Any]], sample_size: int) -> float | None:
    """Return the median duration of the most recent usable runs."""
    if sample_size < 1:
        raise ValueError("sample_size must be at least 1")
    durations = [seconds for run in runs if (seconds := run_seconds(run)) is not None][
        :sample_size
    ]
    if not durations:
        return None
    return statistics.median(durations)


def verdicts(
    measured: dict[str, float | None], budgets: dict[str, int]
) -> list[tuple[str, str, float | None, int]]:
    """Return one (state, workflow, median, budget) row per budgeted workflow.

    A workflow with no usable runs is reported as "no-data" rather than passed:
    an absent measurement is not evidence that the budget is met.
    """
    rows = []
    for workflow in sorted(budgets):
        budget = budgets[workflow]
        seconds = measured.get(workflow)
        if seconds is None:
            rows.append(("no-data", workflow, None, budget))
        elif seconds > budget:
            rows.append(("over", workflow, seconds, budget))
        else:
            rows.append(("ok", workflow, seconds, budget))
    return rows


def _fetch(url: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "panel-assistant-ci-budget",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--branch", default="main")
    parser.add_argument(
        "--budget-file", type=Path, default=Path(".github/ci-budget.json")
    )
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN")
    if not token or not args.repository:
        print("GITHUB_TOKEN and a repository are required", file=sys.stderr)
        return 2

    config = json.loads(args.budget_file.read_text(encoding="utf-8"))
    budgets: dict[str, int] = config["budgets"]
    sample_size: int = config["sample_size"]

    payload = _fetch(
        f"{_API}/repos/{args.repository}/actions/runs"
        f"?branch={args.branch}&status=success&per_page=100",
        token,
    )
    by_workflow: dict[str, list[dict[str, Any]]] = {}
    for run in payload.get("workflow_runs", []):
        by_workflow.setdefault(run.get("name", ""), []).append(run)

    measured = {
        workflow: median_seconds(by_workflow.get(workflow, []), sample_size)
        for workflow in budgets
    }

    failed = False
    for state, workflow, seconds, budget in verdicts(measured, budgets):
        if state == "no-data":
            print(f"NO DATA  {workflow}: no successful runs on {args.branch}")
            failed = True
        elif state == "over":
            print(f"OVER     {workflow}: median {seconds:.0f}s > budget {budget}s")
            failed = True
        else:
            print(f"ok       {workflow}: median {seconds:.0f}s <= budget {budget}s")
    if failed:
        print(
            "\nCI is slower than its recorded budget. Either make it faster or "
            "raise the number in .github/ci-budget.json with a reason.",
            file=sys.stderr,
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
