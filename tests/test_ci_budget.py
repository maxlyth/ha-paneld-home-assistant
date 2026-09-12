"""The CI budget check must be able to go red, and must not pass on absent data."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "check_ci_budget", ROOT / ".github/scripts/check_ci_budget.py"
)
assert _SPEC and _SPEC.loader
check_ci_budget = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_ci_budget)


def _run(started: str, finished: str) -> dict[str, str]:
    return {"run_started_at": started, "updated_at": finished}


def test_run_seconds_measures_wall_clock() -> None:
    run = _run("2026-09-12T16:10:02Z", "2026-09-12T16:13:06Z")
    assert check_ci_budget.run_seconds(run) == 184.0


@pytest.mark.parametrize(
    "run",
    [
        {"updated_at": "2026-09-12T16:13:06Z"},
        {"run_started_at": "2026-09-12T16:10:02Z"},
        _run("not a timestamp", "2026-09-12T16:13:06Z"),
        _run("2026-09-12T16:13:06Z", "2026-09-12T16:10:02Z"),
    ],
)
def test_unusable_runs_are_not_measured(run: dict[str, str]) -> None:
    """A malformed or negative duration must be dropped, never counted as fast."""
    assert check_ci_budget.run_seconds(run) is None


def test_median_uses_only_the_most_recent_sample() -> None:
    runs = [
        _run("2026-09-12T16:00:00Z", "2026-09-12T16:01:00Z"),
        _run("2026-09-12T15:00:00Z", "2026-09-12T15:05:00Z"),
        _run("2026-09-12T14:00:00Z", "2026-09-12T14:03:00Z"),
        _run("2026-09-12T13:00:00Z", "2026-09-12T13:59:00Z"),
    ]
    assert check_ci_budget.median_seconds(runs, 3) == 180.0
    assert check_ci_budget.median_seconds(runs, 4) == 240.0


def test_median_of_no_usable_runs_is_none() -> None:
    assert check_ci_budget.median_seconds([{"updated_at": "x"}], 5) is None


def test_median_rejects_a_meaningless_sample_size() -> None:
    with pytest.raises(ValueError):
        check_ci_budget.median_seconds([], 0)


def test_a_workflow_over_budget_is_reported_over() -> None:
    rows = check_ci_budget.verdicts({"Python tests": 151.0}, {"Python tests": 150})
    assert rows == [("over", "Python tests", 151.0, 150)]


def test_a_workflow_exactly_at_budget_passes() -> None:
    rows = check_ci_budget.verdicts({"Python tests": 150.0}, {"Python tests": 150})
    assert rows == [("ok", "Python tests", 150.0, 150)]


def test_absence_of_data_is_not_a_pass() -> None:
    """A workflow that produced no runs must not be silently credited."""
    rows = check_ci_budget.verdicts({"Python tests": None}, {"Python tests": 150})
    assert rows == [("no-data", "Python tests", None, 150)]


def test_budget_file_covers_every_workflow_that_runs_on_push() -> None:
    """A new push-triggered workflow must arrive with a budget, not without one."""
    import yaml

    config = json.loads((ROOT / ".github/ci-budget.json").read_text(encoding="utf-8"))
    budgeted = set(config["budgets"])
    on_push = set()
    for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        triggers = workflow.get(True, workflow.get("on", {}))
        if isinstance(triggers, dict) and "push" in triggers:
            on_push.add(workflow["name"])
    assert on_push == budgeted


def test_every_workflow_cancels_superseded_runs_off_main() -> None:
    """Without this, one release burns a runner for every ref it touches."""
    import yaml

    for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        triggers = workflow.get(True, workflow.get("on", {}))
        if not (isinstance(triggers, dict) and "push" in triggers):
            continue
        concurrency = workflow.get("concurrency")
        assert concurrency, f"{path.name} has no concurrency group"
        assert "github.ref" in concurrency["group"], path.name
        assert "refs/heads/main" in str(concurrency["cancel-in-progress"]), path.name
