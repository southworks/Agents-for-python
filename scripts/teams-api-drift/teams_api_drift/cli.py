# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
"""Unified command-line interface for Teams API drift tooling."""

import argparse
import json
import os
from pathlib import Path
import sys

from importlib.metadata import version

from .classify import classify, read_capabilities, validate_manifest
from .common import (
    ARTIFACTS,
    CAPABILITIES,
    DEPENDENCY,
    MANIFEST,
    destination,
    read_json,
    write_json,
    write_text,
)
from .compare import compare_versions
from .report import prepare_context, render_report, validate_agent_report

TOOL_COMMANDS = ("compare", "detect", "summary", "render", "prepare", "validate")
WORKFLOW_COMMANDS = ("scope", "analyze", "context", "publish", "policy")


def entrypoint(argv=None):
    """Dispatch one public subcommand to its focused parser and implementation."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] in ("-h", "--help"):
        print("usage: teams-api-drift.py COMMAND [OPTIONS]")
        print()
        print("commands:")
        for command in TOOL_COMMANDS + WORKFLOW_COMMANDS:
            print(f"  {command}")
        return 0 if arguments else 2
    command = arguments.pop(0)
    if command not in TOOL_COMMANDS + WORKFLOW_COMMANDS:
        print(f"teams-api-drift.py: unknown command: {command}", file=sys.stderr)
        return 2
    if command in WORKFLOW_COMMANDS:
        return main(None, [command, *arguments])
    return main(command, arguments)


def main(command=None, argv=None):
    parser = argparse.ArgumentParser(
        prog=f"teams-api-drift.py {command}" if command else "teams-api-drift.py",
        description="Teams API drift analysis",
    )
    parser.add_argument("--output", "-o", type=Path, default=ARTIFACTS)
    if command == "compare":
        parser.add_argument("candidate", nargs="?")
        parser.add_argument("--from", dest="baseline")
        parser.add_argument("--to")
        parser.add_argument("--verbose", "-v", action="store_true")
    elif command == "detect":
        parser.add_argument(
            "--comparison", "-c", type=Path, default=ARTIFACTS / "raw-api-diff.json"
        )
        parser.add_argument("--manifest", "-m", type=Path, default=MANIFEST)
        parser.add_argument("--capabilities", type=Path, default=CAPABILITIES)
        parser.add_argument("--public-api-report", type=Path)
        parser.add_argument("--fail-on-drift", action="store_true")
    elif command == "summary":
        for option in (
            "build",
            "usage-collection",
            "api-extraction",
            "api-comparison",
            "contract-tests",
            "boundary-tests",
        ):
            parser.add_argument(
                "--" + option,
                choices=("success", "failure", "skipped", "cancelled"),
                default="skipped",
            )
    elif command in ("render", "prepare", "validate"):
        parser.add_argument(
            "--findings", "-f", type=Path, default=ARTIFACTS / "findings.json"
        )
        if command in ("render", "prepare"):
            parser.add_argument("--test-summary", type=Path)
        if command == "prepare":
            parser.add_argument("--usage-manifest", type=Path, default=MANIFEST)
            parser.add_argument("--capabilities", type=Path, default=CAPABILITIES)
            parser.add_argument(
                "--deterministic-report",
                type=Path,
                default=ARTIFACTS / "deterministic-report.md",
            )
            parser.add_argument("--public-api-report", type=Path)
        if command == "validate":
            parser.add_argument(
                "--report", type=Path, default=ARTIFACTS / "agent-report.md"
            )
    else:
        parser.add_argument(
            "action", choices=("scope", "analyze", "context", "publish", "policy")
        )
        parser.add_argument("--mode", choices=("pr", "scheduled"), default="pr")
        parser.add_argument(
            "--from", dest="baseline", default=os.environ.get("INPUT_FROM")
        )
        parser.add_argument("--to", default=os.environ.get("INPUT_TO"))
    args = parser.parse_args(argv)
    try:
        if command == "compare":
            if args.to and args.candidate:
                parser.error("Use either --to or a positional candidate, not both")
            result = compare_versions(
                args.baseline or version(DEPENDENCY),
                args.to or args.candidate,
                args.output,
            )
            print(
                f"Compared {result['fromVersion']} to {result['toVersion']}: {len(result['changes'])} changes"
            )
            if args.verbose:
                print(result["diff"])
        elif command == "detect":
            result = classify(
                read_json(args.comparison),
                validate_manifest(read_json(args.manifest)),
                read_capabilities(args.capabilities),
                read_json(args.public_api_report) if args.public_api_report else None,
            )
            write_json(destination(args.output, "findings.json"), result)
            return int(
                args.fail_on_drift
                and (
                    result["summary"]["blocking"] > 0
                    or result["summary"]["required"] > 0
                )
            )
        elif command == "summary":
            names = {
                "build": "build",
                "usage_collection": "usageCollection",
                "api_extraction": "apiExtraction",
                "api_comparison": "apiComparison",
                "contract_tests": "contractTests",
                "boundary_tests": "boundaryTests",
            }
            write_json(
                destination(args.output, "test-summary.json"),
                {
                    "schemaVersion": 1,
                    "checks": {
                        value: getattr(args, key) for key, value in names.items()
                    },
                },
            )
        elif command == "render":
            target = destination(args.output, "deterministic-report.md")
            write_text(
                target,
                render_report(
                    read_json(args.findings),
                    read_json(args.test_summary) if args.test_summary else {},
                    target.parent,
                ),
            )
        elif command == "prepare":
            result = prepare_context(
                read_json(args.findings),
                read_json(args.usage_manifest),
                args.capabilities.read_text(encoding="utf-8"),
                args.deterministic_report.read_text(encoding="utf-8"),
                read_json(args.test_summary) if args.test_summary else None,
                read_json(args.public_api_report) if args.public_api_report else None,
            )
            write_json(destination(args.output, "agent-context.json"), result)
        elif command == "validate":
            result = validate_agent_report(
                args.report.read_text(encoding="utf-8"), read_json(args.findings)
            )
            write_json(destination(args.output, "agent-report-validation.json"), result)
            print(json.dumps(result))
            return int(not result["valid"])
        else:
            return workflow_command(args)
    except Exception as error:
        print(f"Teams API drift error: {error}", file=sys.stderr)
        return 1
    return 0


def workflow_command(args):
    from .workflow import (
        GitHub,
        ai_required,
        analysis,
        context_for,
        final_errors,
        publish,
        write_scope,
    )

    if args.action == "scope":
        write_scope(args.mode, args.baseline, args.to)
        return 0
    if args.action == "analyze":
        if not args.baseline or not args.to:
            raise ValueError("Analysis requires --from and --to")
        analysis(args.baseline, args.to, args.output)
        return 0  # Enforce deferred outcomes only after artifact upload.
    state = read_json(args.output / "run-state.json")
    event = os.environ.get("GITHUB_EVENT_NAME", "workflow_dispatch")
    fork = os.environ.get("IS_FORK", "false") == "true"
    required = ai_required(args.mode, event, fork, state["changed"])
    if args.action == "context":
        ready = (
            required
            and (args.output / "findings.json").is_file()
            and state["checks"]["deterministicReport"] == "success"
        )
        if ready:
            write_json(args.output / "agent-context.json", context_for(args.output))
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                output.write(f"ready={str(ready).lower()}\n")
        return 0
    if args.action == "policy":
        outcomes = {
            key: os.environ.get(f"AI_{key.upper()}_OUTCOME", "skipped")
            for key in ("context", "install", "generate", "validate")
        }
        errors = final_errors(state, required, outcomes)
        for error in errors:
            print("Blocking drift check: " + error)
        return int(bool(errors))
    if args.action == "publish":
        if fork or (args.mode == "scheduled" and not state["changed"]):
            return 0
        findings_path = args.output / "findings.json"
        if not findings_path.is_file():
            return 0
        report = None
        if args.mode == "scheduled":
            validation_path = args.output / "agent-report-validation.json"
            if not validation_path.is_file() or not read_json(validation_path)["valid"]:
                return 0
            report = (args.output / "agent-report.md").read_text(encoding="utf-8")
        repository = os.environ["GITHUB_REPOSITORY"]
        pr_number = os.environ.get("PR_NUMBER")
        publish(
            GitHub(repository, os.environ["GITHUB_TOKEN"]),
            args.mode,
            read_json(findings_path),
            f"https://github.com/{repository}/actions/runs/{os.environ['GITHUB_RUN_ID']}",
            pr_number=int(pr_number) if pr_number else None,
            fork=fork,
            report=report,
        )
        return 0
    raise ValueError("Unknown workflow command")
