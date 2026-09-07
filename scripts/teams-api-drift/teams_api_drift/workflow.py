# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
"""Workflow scope, deferred verification, publication and final policy.

Publication is an explicit command; analysis and tests never contact GitHub write APIs.
"""

import json
from email.parser import BytesParser
import os
from pathlib import Path
import sys
import zipfile
from urllib.request import Request, urlopen

from packaging.version import Version
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from .classify import classify, read_capabilities, validate_manifest
from .common import (
    ARTIFACTS,
    CAPABILITIES,
    DEPENDENCY,
    MANIFEST,
    PACKAGE,
    PACKAGE_ROOT,
    ROOT,
    declared_minimum,
    declared_requirement,
    latest_stable,
    python_in,
    read_json,
    run,
    temporary_environment,
    write_json,
    write_text,
)
from .compare import compare_versions
from .report import prepare_context, render_report, validate_agent_report

REQUIRED_CHECKS = (
    "build",
    "usageCollection",
    "apiExtraction",
    "apiComparison",
    "contractTests",
    "boundaryTests",
    "impactClassification",
    "verificationSummary",
    "deterministicReport",
)


def resolve_scope(
    event_name, base_source=None, head_source=None, from_version=None, to_version=None
):
    if event_name == "pull_request":
        before, after = declared_requirement(base_source), declared_requirement(
            head_source
        )
        return {
            "run": before.specifier != after.specifier,
            "fromVersion": declared_minimum(before),
            "toVersion": declared_minimum(after),
        }
    if from_version is not None:
        if not to_version:
            raise ValueError("Manual PR analysis requires both --from and --to")
        return {
            "run": True,
            "fromVersion": str(Version(from_version)),
            "toVersion": str(Version(to_version)),
        }
    requirement = declared_requirement(head_source)
    return {
        "run": True,
        "fromVersion": declared_minimum(requirement),
        "toVersion": latest_stable(),
    }


def write_scope(mode, from_version=None, to_version=None):
    source_path = "libraries/microsoft-agents-hosting-msteams/setup.py"
    event = os.environ.get("GITHUB_EVENT_NAME", "workflow_dispatch")
    base = head = None
    if event == "pull_request":
        base = run(["git", "show", f"{os.environ['BASE_SHA']}:{source_path}"])
        head = run(["git", "show", f"{os.environ['HEAD_SHA']}:{source_path}"])
    else:
        head = (ROOT / source_path).read_text(encoding="utf-8")
    if mode == "pr" and event != "pull_request" and not (from_version and to_version):
        raise ValueError("Manual PR analysis requires explicit baseline and candidate")
    result = resolve_scope(event, base, head, from_version, to_version)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as file:
            file.write(
                f"run={str(result['run']).lower()}\nfrom={result['fromVersion']}\nto={result['toVersion']}\n"
            )
    print(json.dumps(result))
    return result


def analysis(from_version, to_version, output=ARTIFACTS):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    # Avoid a prior local run being mistaken for fresh evidence.
    if any(
        (output / name).exists()
        for name in ("run-state.json", "raw-api-diff.json", "findings.json")
    ):
        raise ValueError("Use a fresh output directory for each workflow analysis")
    checks = dict.fromkeys(REQUIRED_CHECKS, "skipped")
    summary = {
        "schemaVersion": 1,
        "checks": checks,
        "fromVersion": from_version,
        "toVersion": to_version,
    }

    def check(name, action):
        print(f"Running {name}...", flush=True)
        try:
            value = action()
            checks[name] = "success"
            if isinstance(value, str):
                write_text(output / f"{name}.log", value)
            return value
        except Exception as error:
            checks[name] = "failure"
            write_text(output / f"{name}.log", str(error) + "\n")
            print(f"{name} failed; evidence saved to {name}.log", flush=True)
            return None

    manifest = check("usageCollection", lambda: validate_manifest(read_json(MANIFEST)))
    comparison = findings = None
    with temporary_environment(prefix="teams-api-verification-") as work:
        comparison = check(
            "apiExtraction",
            lambda: compare_versions(from_version, to_version, output, work),
        )
        checks["apiComparison"] = checks["apiExtraction"]
        candidate = python_in(Path(work) / "candidate")

        def build():
            env = dict(os.environ, PackageVersion="0.0.0")
            wheel_root = Path(work) / "wheels"
            # Build from copies: setup.py must not generate metadata in the checkout.
            import shutil

            for name in (
                "microsoft-agents-activity",
                "microsoft-agents-hosting-core",
                "microsoft-agents-authentication-msal",
                PACKAGE,
            ):
                source = Path(work) / "sources" / name
                shutil.copytree(
                    ROOT / "libraries" / name,
                    source,
                    ignore=shutil.ignore_patterns("build", "*.egg-info", "__pycache__"),
                )
                run(
                    [
                        sys.executable,
                        "-m",
                        "build",
                        "--wheel",
                        "--outdir",
                        wheel_root,
                        source,
                    ],
                    env=env,
                )
            run(
                [
                    candidate,
                    "-m",
                    "pip",
                    "install",
                    "-r",
                    ROOT / "scripts/teams-api-drift/requirements.txt",
                ]
            )
            wheels = sorted(wheel_root.glob("*.whl"))
            extension = next(
                w
                for w in wheels
                if w.name.startswith("microsoft_agents_hosting_msteams-")
            )
            # Resolve core and the other extension dependencies normally. Install the
            # extension itself without its Teams version constraint for future-major analysis.
            with zipfile.ZipFile(extension) as wheel:
                metadata_file = next(
                    name
                    for name in wheel.namelist()
                    if name.endswith(".dist-info/METADATA")
                )
                metadata = BytesParser().parsebytes(wheel.read(metadata_file))
                dependencies = [
                    value
                    for value in metadata.get_all("Requires-Dist", [])
                    if canonicalize_name(Requirement(value).name) != DEPENDENCY
                ]
            run(
                [
                    candidate,
                    "-m",
                    "pip",
                    "install",
                    *[w for w in wheels if w != extension],
                    *dependencies,
                ]
            )
            run([candidate, "-m", "pip", "install", "--no-deps", extension])
            actual = run(
                [
                    candidate,
                    "-c",
                    "from importlib.metadata import version; print(version('microsoft-teams-api'))",
                ]
            ).strip()
            if Version(actual) != Version(to_version):
                raise ValueError(
                    f"Tests resolved {actual} instead of candidate {to_version}"
                )
            return f"Built and installed local wheels; test candidate: {actual}\n"

        if comparison is not None:
            check("build", build)
            if checks["build"] == "success":
                check(
                    "contractTests",
                    lambda: run(
                        [
                            candidate,
                            "-m",
                            "mypy",
                            "--config-file",
                            ROOT / "scripts/teams-api-drift/mypy.ini",
                            ROOT / "tests/teams_api_drift/contracts.py",
                        ]
                    ),
                )
                check(
                    "boundaryTests",
                    lambda: run(
                        [
                            candidate,
                            "-m",
                            "pytest",
                            "tests/hosting_msteams",
                            "-o",
                            "asyncio_default_fixture_loop_scope=function",
                            "-o",
                            f"cache_dir={output / 'pytest-cache'}",
                        ]
                    ),
                )
            if manifest:

                def classify_changes():
                    result = classify(
                        comparison, manifest, read_capabilities(CAPABILITIES)
                    )
                    write_json(output / "findings.json", result)
                    return result

                findings = check("impactClassification", classify_changes)
                if findings and (
                    findings["summary"]["blocking"] or findings["summary"]["required"]
                ):
                    checks["impactClassification"] = "failure"
            requirement = declared_requirement(
                (PACKAGE_ROOT / "setup.py").read_text(encoding="utf-8")
            )
            summary["candidateOutsideDeclaredRange"] = (
                not requirement.specifier.contains(to_version, prereleases=True)
            )
    checks["verificationSummary"] = "success"
    write_json(output / "test-summary.json", summary)
    check(
        "deterministicReport",
        lambda: write_text(
            output / "deterministic-report.md", render_report(findings, summary, output)
        ),
    )
    write_json(output / "test-summary.json", summary)
    # Re-render to include the completed render check.
    if checks["deterministicReport"] == "success":
        write_text(
            output / "deterministic-report.md", render_report(findings, summary, output)
        )
    state = {
        "schemaVersion": 1,
        "checks": checks,
        "changed": comparison["changed"] if comparison else None,
        "fromVersion": from_version,
        "toVersion": to_version,
    }
    write_json(output / "run-state.json", state)
    return state


def ai_required(mode, event_name, fork, changed):
    if event_name == "pull_request" and fork:
        return False
    return mode == "pr" or changed is True


def final_errors(state, require_ai, ai_outcomes):
    errors = [
        f"{name}: {state.get('checks', {}).get(name, 'missing')}"
        for name in REQUIRED_CHECKS
        if state.get("checks", {}).get(name) != "success"
    ]
    if require_ai:
        errors += [
            f"{name}: {ai_outcomes.get(name, 'missing')}"
            for name in ("context", "install", "generate", "validate")
            if ai_outcomes.get(name) != "success"
        ]
    return errors


class GitHub:
    """Small REST client; token is used only in headers and never written to artifacts."""

    def __init__(self, repository, token):
        self.base = "https://api.github.com/repos/" + repository
        self.token = token

    def request(self, method, path, body=None):
        request = Request(
            self.base + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urlopen(request, timeout=60) as response:
            return json.load(response)

    def paginate(self, path):
        result, page = [], 1
        separator = "&" if "?" in path else "?"
        while True:
            items = self.request("GET", f"{path}{separator}per_page=100&page={page}")
            result.extend(items)
            if len(items) < 100:
                return result
            page += 1


def publish(
    client, mode, findings, artifact_url, *, pr_number=None, fork=False, report=None
):
    if fork:
        return None
    if mode == "pr":
        if not pr_number:
            return None  # Manual runs have artifacts but no PR comment target.
        marker = "<!-- teams-api-drift-report -->"
        actionable = [
            f for f in findings["findings"] if f["classification"] != "no-action"
        ]
        body = "\n".join(
            [
                marker,
                "## TEAMS API drift analysis",
                "",
                f"Compared `{findings['fromVersion']}` to `{findings['toVersion']}`.",
                " · ".join(
                    f"{key}: **{value}**" for key, value in findings["summary"].items()
                ),
                "",
                *[
                    f"- **{f['id']}** — {f['classification']} · {f['kind']}: `{f['upstreamSymbol']}`"
                    for f in actionable[:5]
                ],
                "",
                f"Full report and evidence: {artifact_url}",
            ]
        )
        existing = next(
            (
                c
                for c in client.paginate(f"/issues/{pr_number}/comments")
                if marker in (c.get("body") or "")
                and c.get("user", {}).get("type") == "Bot"
            ),
            None,
        )
        return client.request(
            "PATCH" if existing else "POST",
            (
                f"/issues/comments/{existing['id']}"
                if existing
                else f"/issues/{pr_number}/comments"
            ),
            {"body": body},
        )
    if report is None or not validate_agent_report(report, findings)["valid"]:
        raise ValueError("A validated advisory report is required to publish an issue")
    marker = "<!-- scheduled-teams-api-drift -->"
    body = "\n".join(
        [
            marker,
            f"Scheduled comparison of `{DEPENDENCY}` **{findings['fromVersion']}** to **{findings['toVersion']}** found API differences.",
            "",
            report.strip(),
            "",
            f"Deterministic artifacts: {artifact_url}",
        ]
    )
    existing = next(
        (
            i
            for i in client.paginate("/issues?state=open")
            if not i.get("pull_request")
            and marker in (i.get("body") or "")
            and i.get("user", {}).get("type") == "Bot"
        ),
        None,
    )
    return client.request(
        "PATCH" if existing else "POST",
        f"/issues/{existing['number']}" if existing else "/issues",
        {
            "title": f"teams.api drift detected: {findings['fromVersion']} → {findings['toVersion']}",
            "body": body,
        },
    )


def context_for(output):
    output = Path(output)
    return prepare_context(
        read_json(output / "findings.json"),
        read_json(MANIFEST),
        CAPABILITIES.read_text(encoding="utf-8"),
        (output / "deterministic-report.md").read_text(encoding="utf-8"),
        read_json(output / "test-summary.json"),
    )
