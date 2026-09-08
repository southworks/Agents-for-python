# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.

import re
import zipfile

import yaml

from teams_api_drift.candidate import _extension_dependencies
from teams_api_drift.cli import TOOL_COMMANDS, entrypoint
from teams_api_drift.common import ROOT


def test_unified_cli_lists_and_dispatches_focused_subcommands(tmp_path, capsys):
    assert entrypoint(["--help"]) == 0
    help_text = capsys.readouterr().out
    assert "compare" in help_text
    assert "prepare-candidate" in help_text
    assert "analyze" not in help_text
    assert "publish" not in help_text
    assert "policy" not in help_text
    assert entrypoint(["summary", "--output", str(tmp_path), "--build", "success"]) == 0
    summary = yaml.safe_load((tmp_path / "test-summary.json").read_text())
    assert summary["checks"]["build"] == "success"
    assert entrypoint(["unknown"]) == 2


def test_extension_dependencies_exclude_teams_api(tmp_path):
    wheel = tmp_path / "extension.whl"
    metadata = """Metadata-Version: 2.1
Name: example
Version: 1.0
Requires-Dist: microsoft-teams-api>=2,<3
Requires-Dist: aiohttp>=3
Requires-Dist: microsoft-agents-hosting-core==0.0.0

"""
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("example-1.0.dist-info/METADATA", metadata)

    assert _extension_dependencies(wheel) == [
        "aiohttp>=3",
        "microsoft-agents-hosting-core==0.0.0",
    ]


def test_renderer_can_preserve_evidence_after_an_upstream_failure(tmp_path):
    assert (
        entrypoint(
            [
                "render",
                "--findings",
                str(tmp_path / "missing-findings.json"),
                "--allow-incomplete",
                "--output",
                str(tmp_path),
            ]
        )
        == 0
    )
    report = (tmp_path / "deterministic-report.md").read_text()
    assert "Analysis is incomplete" in report


def test_python_commands_are_transformations_without_workflow_policy():
    assert TOOL_COMMANDS == (
        "resolve",
        "verify-usage",
        "compare",
        "prepare-candidate",
        "detect",
        "summary",
        "render",
        "prepare",
        "validate",
    )
    package = ROOT / "scripts/teams-api-drift/teams_api_drift"
    assert not (package / "workflow.py").exists()
    source = "\n".join(path.read_text() for path in package.glob("*.py"))
    for workflow_concern in (
        "GITHUB_EVENT_NAME",
        "GITHUB_EVENT_PATH",
        "GITHUB_TOKEN",
        "pull_request",
        "workflow_dispatch",
    ):
        assert workflow_concern not in source


def _workflow(path):
    return yaml.load(path.read_text(), Loader=yaml.BaseLoader)


def test_workflows_orchestrate_focused_commands_and_deferred_failure():
    expected_commands = {
        "verify-usage",
        "compare",
        "prepare-candidate",
        "detect",
        "summary",
        "render",
        "prepare",
        "validate",
    }
    for path in (ROOT / ".github/workflows").glob("teams-api-drift-*.yml"):
        workflow = _workflow(path)
        assert "on" in workflow
        steps = next(
            job["steps"]
            for job in workflow["jobs"].values()
            if any(
                "teams-api-drift.py compare" in step.get("run", "")
                for step in job.get("steps", [])
            )
        )
        commands = set()
        for step in steps:
            run = step.get("run", "")
            commands.update(re.findall(r"teams-api-drift\.py\s+([a-z-]+)", run))
            if "continue-on-error" in step:
                assert step["continue-on-error"] == "true"
        assert expected_commands <= commands

        upload = next(
            index
            for index, step in enumerate(steps)
            if "upload-artifact@" in step.get("uses", "")
        )
        publication = next(
            index
            for index, step in enumerate(steps)
            if "github-script@" in step.get("uses", "")
        )
        final_gate = next(
            index
            for index, step in enumerate(steps)
            if "for check in" in step.get("run", "")
        )
        assert upload < publication < final_gate
        assert steps[upload]["with"]["retention-days"] == "21"
        assert "always()" in steps[final_gate]["if"]
        assert "for check in" in steps[final_gate]["run"]

        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                if "uses" in step:
                    assert re.fullmatch(r"[\w/-]+@[0-9a-f]{40}", step["uses"])


def test_event_and_publication_policy_live_only_in_the_workflows():
    workflows = ROOT / ".github/workflows"
    pull_requests = (workflows / "teams-api-drift-prs.yml").read_text()
    scheduled = (workflows / "teams-api-drift-scheduled.yml").read_text()

    assert (
        "github.event.pull_request.head.repo.full_name == github.repository"
        in pull_requests
    )
    assert "github.event_name == 'workflow_dispatch'" in pull_requests
    assert "<!-- teams-api-drift-report -->" in pull_requests
    assert "github.paginate(github.rest.issues.listComments" in pull_requests
    assert "github.rest.issues.updateComment" in pull_requests
    assert "github.rest.issues.createComment" in pull_requests
    assert 'input=prompt + "\\n## Runtime context\\n\\n" + context' in pull_requests
    assert "actionable.slice(0, 5)" in pull_requests
    assert "FINDINGS_PATH:" in pull_requests
    assert "pull_request" not in scheduled
    assert "fork" not in scheduled.lower()
    assert "<!-- scheduled-teams-api-drift -->" in scheduled
    assert re.search(r"steps\.[\w-]+\.outputs\.value == 'true'", scheduled)
    assert "github.paginate(github.rest.issues.listForRepo" in scheduled
    assert "github.rest.issues.update" in scheduled
    assert "github.rest.issues.create" in scheduled


def test_scheduled_fake_candidate_override_is_visibly_bounded():
    path = ROOT / ".github/workflows/teams-api-drift-scheduled.yml"
    source = path.read_text()
    assert source.count("BEGIN TEMPORARY WORKFLOW TEST") == 2
    assert source.count("END TEMPORARY WORKFLOW TEST") == 2
    assert 'FAKE_TEAMS_API_CANDIDATE: "2.99.901"' in source
    assert "PIP_FIND_LINKS:" in source


def test_pr_fake_candidate_override_matches_scheduled_test_pair():
    source = (ROOT / ".github/workflows/teams-api-drift-prs.yml").read_text()
    assert source.count("BEGIN TEMPORARY WORKFLOW TEST") == 3
    assert source.count("END TEMPORARY WORKFLOW TEST") == 3
    assert 'FAKE_TEAMS_API_BASELINE: "2.0.16"' in source
    assert 'FAKE_TEAMS_API_CANDIDATE: "2.99.901"' in source
    assert "PIP_FIND_LINKS:" in source
