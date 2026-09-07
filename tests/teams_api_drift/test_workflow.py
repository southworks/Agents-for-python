# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.

from pathlib import Path
import re

import pytest
import yaml

from teams_api_drift.cli import entrypoint
from teams_api_drift.common import DEPENDENCY, ROOT
from teams_api_drift.workflow import (
    GitHub,
    REQUIRED_CHECKS,
    ai_required,
    final_errors,
    publish,
)
from teams_api_drift.report import ADVISORY, SECTIONS, TITLE


class FakeGitHub:
    def __init__(self, existing=()):
        self.existing = existing
        self.calls = []

    def paginate(self, path):
        self.calls.append(("GET", path))
        return self.existing

    def request(self, method, path, body):
        self.calls.append((method, path, body))
        return body


def findings():
    return {
        "schemaVersion": 1,
        "dependency": DEPENDENCY,
        "fromVersion": "2.0.0",
        "toVersion": "2.0.16",
        "summary": {"blocking": 0, "required": 0, "review": 0, "no-action": 0},
        "findings": [],
    }


def advisory():
    return (
        TITLE
        + "\n\n"
        + "\n\n".join(
            f"## {section}\n\n"
            + (ADVISORY if section == "Summary" else "- No supported items.")
            for section in SECTIONS
        )
        + "\n"
    )


def test_unified_cli_lists_and_dispatches_subcommands(tmp_path, capsys):
    assert entrypoint(["--help"]) == 0
    assert "compare" in capsys.readouterr().out
    assert entrypoint(["summary", "--output", str(tmp_path), "--build", "success"]) == 0
    summary = yaml.safe_load((tmp_path / "test-summary.json").read_text())
    assert summary["checks"]["build"] == "success"
    assert entrypoint(["unknown"]) == 2


@pytest.mark.parametrize(
    "mode,event,fork,changed,expected",
    [
        ("pr", "pull_request", True, True, False),
        ("pr", "pull_request", False, False, True),
        ("pr", "workflow_dispatch", False, False, True),
        ("scheduled", "schedule", False, False, False),
        ("scheduled", "schedule", False, True, True),
    ],
)
def test_advisory_policy(mode, event, fork, changed, expected):
    assert ai_required(mode, event, fork, changed) is expected


def test_final_gate_accounts_for_missing_and_skipped_checks():
    state = {"checks": dict.fromkeys(REQUIRED_CHECKS, "success")}
    assert final_errors(state, False, {}) == []
    assert len(final_errors(state, True, {})) == 4
    state["checks"]["contractTests"] = "skipped"
    assert final_errors(state, False, {}) == ["contractTests: skipped"]


def test_pr_comment_create_update_and_fork_guard():
    github = FakeGitHub()
    publish(github, "pr", findings(), "https://example.com/run", pr_number=12)
    assert github.calls[-1][:2] == ("POST", "/issues/12/comments")
    github = FakeGitHub(
        [{"id": 9, "body": "<!-- teams-api-drift-report -->", "user": {"type": "Bot"}}]
    )
    publish(github, "pr", findings(), "https://example.com/run", pr_number=12)
    assert github.calls[-1][:2] == ("PATCH", "/issues/comments/9")
    github = FakeGitHub()
    publish(github, "pr", findings(), "", pr_number=12, fork=True)
    assert github.calls == []


def test_scheduled_issue_upsert_requires_valid_report():
    github = FakeGitHub()
    with pytest.raises(ValueError):
        publish(github, "scheduled", findings(), "", report="unvalidated")
    assert github.calls == []
    publish(
        github, "scheduled", findings(), "https://example.com/run", report=advisory()
    )
    assert github.calls[-1][:2] == ("POST", "/issues")
    github = FakeGitHub(
        [
            {
                "number": 8,
                "body": "<!-- scheduled-teams-api-drift -->",
                "user": {"type": "Bot"},
            }
        ]
    )
    publish(
        github, "scheduled", findings(), "https://example.com/run", report=advisory()
    )
    assert github.calls[-1][:2] == ("PATCH", "/issues/8")


def test_github_paginates_until_short_page(monkeypatch):
    github = GitHub("owner/repo", "fake-token")
    paths = []

    def request(method, path):
        paths.append(path)
        return [{}] * (100 if len(paths) == 1 else 1)

    monkeypatch.setattr(github, "request", request)
    assert len(github.paginate("/issues?state=open")) == 101
    assert paths[-1].endswith("&per_page=100&page=2")


def test_workflows_preserve_upload_before_publication_and_policy():
    for path in (ROOT / ".github/workflows").glob("teams-api-drift-*.yml"):
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        assert "on" in workflow
        steps = workflow["jobs"]["analyze"]["steps"]
        upload = next(
            i
            for i, step in enumerate(steps)
            if "upload-artifact@" in step.get("uses", "")
        )
        publication = next(
            i for i, step in enumerate(steps) if " publish " in step.get("run", "")
        )
        policy = next(
            i for i, step in enumerate(steps) if " policy " in step.get("run", "")
        )
        assert upload < publication < policy
        assert steps[upload]["with"]["retention-days"] == "21"
        assert "always()" in steps[policy]["if"]
        assert "fork == false" in steps[publication]["if"]
        for step in steps:
            if "uses" in step:
                assert re.fullmatch(r"[\w/-]+@[0-9a-f]{40}", step["uses"])


def test_analysis_retains_evidence_when_extraction_fails(tmp_path, monkeypatch):
    from teams_api_drift.workflow import analysis

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic extraction failure")

    monkeypatch.setattr("teams_api_drift.workflow.compare_versions", fail)
    state = analysis("2.0.0", "2.0.16", tmp_path)
    assert state["changed"] is None
    assert state["checks"]["apiExtraction"] == "failure"
    assert state["checks"]["contractTests"] == "skipped"
    assert (tmp_path / "run-state.json").is_file()
    assert "incomplete" in (tmp_path / "deterministic-report.md").read_text()
    assert not (tmp_path / "findings.json").exists()
