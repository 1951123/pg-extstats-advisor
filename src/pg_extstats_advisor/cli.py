"""Non-interactive command-line interface for restartable MVP stages."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import traceback
from pathlib import Path

import psycopg

from pg_extstats_advisor.calibration import CalibrationConfig, run_calibration
from pg_extstats_advisor.capture.fixed import verify_fixed_t_bundle
from pg_extstats_advisor.capture.workflow import CaptureConfig, capture_fixed_t
from pg_extstats_advisor.deploy.bundle import RecommendationBundle, validate_recommendation_bundle
from pg_extstats_advisor.deploy.preflight import run_preflight
from pg_extstats_advisor.deploy.verification import (
    verify_deployment,
    verify_rollback,
    write_verification_report,
)
from pg_extstats_advisor.errors import AdvisorCLIError, ExitCode
from pg_extstats_advisor.orchestration import (
    cleanup_acquisition_stage,
    execute_recommendation_stage,
    execute_search_stage,
    execute_validation_stage,
    load_maintenance_model,
    load_optional_maintenance_model,
    load_prepared_run,
)
from pg_extstats_advisor.prepare.artifacts import prepare_mvp
from pg_extstats_advisor.prepare.config import PreparationConfig
from pg_extstats_advisor.screening import (
    build_candidate_set,
    build_singleton_profile_from_csv,
    build_singleton_profile_native,
    load_artifact,
    write_artifact,
)
from pg_extstats_advisor.workflow import AdviseConfig, advise_fixed_t


def _dsn(value: str | None, env_name: str) -> str:
    result = value or os.environ.get(env_name)
    if not result:
        raise AdvisorCLIError(f"DSN required via option or {env_name}", ExitCode.USAGE)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pg-extstats-advisor", description="Offline PostgreSQL extended-statistics advisor"
    )
    parser.add_argument("--verbose", action="store_true", help="show diagnostic tracebacks")
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture", help="capture a sealed fixed-target bundle")
    capture.add_argument("--dsn", help="read-only production connection string")
    capture.add_argument("--relation", required=True)
    capture.add_argument("--workload", required=True, type=Path)
    capture.add_argument("--output", required=True, type=Path)
    capture.add_argument("--statistics-target", type=int, default=100)
    capture.add_argument("--sample-rows", type=int, default=30_000)
    advise = commands.add_parser("advise", help="run the offline advisor from a sealed bundle")
    advise.add_argument("bundle", type=Path)
    advise.add_argument("--advisor-dsn", help="disposable patched advisor connection string")
    advise.add_argument("--candidate-catalog", required=True, type=Path)
    advise.add_argument("--incidence", required=True, type=Path)
    advise.add_argument("--maintenance-model", required=True, type=Path)
    advise.add_argument("--output", required=True, type=Path)
    advise.add_argument("--cache", required=True, type=Path)
    advise.add_argument("--budget", required=True)
    advise.add_argument("--statistics-target", type=int, default=100)
    calibrate = commands.add_parser(
        "calibrate-maintenance", help="run isolated aggregate ANALYZE calibration"
    )
    calibrate.add_argument("config", type=Path)
    prepare = commands.add_parser(
        "prepare", help="prepare frozen workload/candidate/payload artifacts"
    )
    prepare.add_argument("config", type=Path)
    search = commands.add_parser("search", help="run deterministic search from prepared artifacts")
    search.add_argument("run_dir", type=Path)
    search.add_argument("--budget")
    search.add_argument("--candidate-set", type=Path)
    search.add_argument("--search-mode", choices=("full", "add-only"), default="full")
    search.add_argument(
        "--budget-mode", choices=("absolute", "candidate-set-total"), default="absolute"
    )
    search.add_argument("--acquisition-dsn")
    profile = commands.add_parser(
        "singleton-profile", help="materialize a versioned singleton utility profile"
    )
    profile.add_argument("run_dir", type=Path)
    profile.add_argument("--output", required=True, type=Path)
    profile.add_argument("--source-csv", type=Path)
    profile.add_argument("--acquisition-dsn")
    profile.add_argument(
        "--unpriced",
        action="store_true",
        help="profile singleton utility without a maintenance-cost model",
    )
    screen = commands.add_parser(
        "screen-candidates", help="build a deterministic screened candidate-set artifact"
    )
    screen.add_argument("run_dir", type=Path)
    screen.add_argument("--singleton-profile", required=True, type=Path)
    screen.add_argument("--top-fraction", required=True, type=float)
    screen.add_argument("--output", required=True, type=Path)
    recommend = commands.add_parser("recommend", help="render persisted search result")
    recommend.add_argument("run_dir", type=Path)
    validate = commands.add_parser("validate", help="validate a capture or recommendation artifact")
    validate.add_argument("artifact", type=Path)
    validate.add_argument("--expected-target", type=int, default=100)
    validate.add_argument("--validation-dsn")
    inspect = commands.add_parser("inspect", help="print a recommendation summary")
    inspect.add_argument("artifact", type=Path)
    preflight = commands.add_parser(
        "preflight", help="read-only production compatibility check for a recommendation"
    )
    preflight.add_argument("recommendation", type=Path)
    preflight.add_argument("--production-dsn", "--dsn", dest="production_dsn")
    preflight.add_argument("--json", action="store_true", help="emit the structured report")
    verify_deployment_command = commands.add_parser(
        "verify-deployment", help="read-only verification after manual deployment and ANALYZE"
    )
    verify_deployment_command.add_argument("recommendation", type=Path)
    verify_deployment_command.add_argument("--production-dsn", "--dsn", dest="production_dsn")
    verify_deployment_command.add_argument("--output", type=Path)
    verify_deployment_command.add_argument("--json", action="store_true", help="emit the structured report")
    verify_rollback_command = commands.add_parser(
        "verify-rollback", help="read-only verification after manual rollback"
    )
    verify_rollback_command.add_argument("recommendation", type=Path)
    verify_rollback_command.add_argument("--production-dsn", "--dsn", dest="production_dsn")
    verify_rollback_command.add_argument("--output", type=Path)
    verify_rollback_command.add_argument("--json", action="store_true", help="emit the structured report")
    cleanup = commands.add_parser(
        "cleanup-acquisition", help="drop only this run's acquisition handles"
    )
    cleanup.add_argument("run_dir", type=Path)
    cleanup.add_argument("--acquisition-dsn")
    run = commands.add_parser("run", help="prepare, search, and recommend")
    run.add_argument("config", type=Path)
    run.add_argument("--budget", required=True)
    run.add_argument("--validate", action="store_true")
    run.add_argument("--validation-dsn")
    return parser


def _prepare(path: Path) -> PreparationConfig:
    config = PreparationConfig.load(path)
    patch = (
        Path(__file__).parents[2]
        / "pg"
        / "patches"
        / "postgresql-16.14-hypothetical-extstats.patch"
    )
    patch_identity = hashlib.sha256(patch.read_bytes()).hexdigest()
    upstream = "f6d077142737920858ce958ccdb75c6ee137a63b5b0853c70693d401ac7e3471"
    with (
        psycopg.connect(config.source_dsn) as source,
        psycopg.connect(config.acquisition_dsn) as acquisition,
    ):
        result = prepare_mvp(
            config, source, acquisition, upstream_sha256=upstream, patch_commit=patch_identity
        )
    print(f"prepared: {result.artifact_root}")
    print(f"candidates: {len(result.catalog.candidates)}")
    print(f"repository: {result.repository.digest}")
    return config


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "capture":
            result = capture_fixed_t(CaptureConfig(_dsn(args.dsn, "PGEXT_CAPTURE_DSN"), args.relation, args.workload, args.output, args.statistics_target, args.sample_rows))
            print(json.dumps(result, sort_keys=True))
        elif args.command == "advise":
            result = advise_fixed_t(AdviseConfig(args.bundle, _dsn(args.advisor_dsn, "PGEXT_ADVISOR_DSN"), args.candidate_catalog, args.incidence, args.maintenance_model, args.output, args.cache, args.budget, args.statistics_target))
            print(json.dumps(result, sort_keys=True))
        elif args.command == "calibrate-maintenance":
            config = CalibrationConfig.load(args.config)
            with psycopg.connect(config.dsn, autocommit=True) as connection:
                report = run_calibration(config, connection)
            print(f"calibration: {report['status']}")
            print(f"report: {config.output_path / 'calibration-report.json'}")
        elif args.command == "prepare":
            _prepare(args.config)
        elif args.command == "search":
            with psycopg.connect(_dsn(args.acquisition_dsn, "PGEXT_ACQUISITION_DSN")) as connection:
                result = execute_search_stage(
                    args.run_dir,
                    connection,
                    args.budget,
                    candidate_set_path=args.candidate_set,
                    search_mode=args.search_mode,
                    budget_mode=args.budget_mode,
                )
            print(f"selected candidates: {len(result.selected_design.candidate_ids)}")
            print(f"objective: {result.selected_objective}")
            print(f"maintenance cost: {result.selected_maintenance_cost}")
        elif args.command == "singleton-profile":
            prepared = load_prepared_run(args.run_dir)
            model = load_optional_maintenance_model(args.run_dir) if args.unpriced else load_maintenance_model(args.run_dir)
            if args.source_csv is not None:
                profile = build_singleton_profile_from_csv(
                    args.source_csv,
                    prepared,
                    model,
                    evaluator_provenance={
                        "mode": "imported-frozen-profile",
                        "source_sha256": hashlib.sha256(args.source_csv.read_bytes()).hexdigest(),
                        "source_artifact": str(args.source_csv),
                        "postgres_version": prepared.repository.postgres_version,
                        "patch_commit": prepared.repository.patch_commit,
                        "native_singleton_evaluations": 0,
                    },
                )
            else:
                with psycopg.connect(
                    _dsn(args.acquisition_dsn, "PGEXT_ACQUISITION_DSN")
                ) as connection:
                    profile = build_singleton_profile_native(prepared, model, connection)
            digest = write_artifact(args.output, profile)
            print(f"singleton profile: {args.output}")
            print(f"digest: {digest}")
        elif args.command == "screen-candidates":
            prepared = load_prepared_run(args.run_dir)
            model = load_maintenance_model(args.run_dir)
            profile = load_artifact(args.singleton_profile)
            candidate_set = build_candidate_set(
                profile, prepared, model, top_fraction=args.top_fraction
            )
            digest = write_artifact(args.output, candidate_set)
            print(f"screened candidate set: {args.output}")
            print(f"digest: {digest}")
        elif args.command == "recommend":
            print(f"deployment SQL: {execute_recommendation_stage(args.run_dir)}")
        elif args.command == "validate":
            artifact = args.artifact
            if (artifact / "bundle.json").exists():
                try:
                    bundle = json.loads((artifact / "bundle.json").read_text())
                    if bundle.get("profile") == "fixed_t_single_snapshot":
                        result = verify_fixed_t_bundle(artifact, expected_target=args.expected_target, require_supported_profile=True)
                        result["validity"] = "valid"
                    else:
                        result = verify_fixed_t_bundle(artifact, expected_target=args.expected_target)
                        result["validity"] = "valid-historical-compatible-profile"
                except ValueError as error:
                    raise AdvisorCLIError(str(error), ExitCode.CORRUPT_ARTIFACT) from error
                print(json.dumps(result, sort_keys=True))
            elif (artifact / "recommendation.json").exists() or (artifact.is_file() and artifact.name == "recommendation.json"):
                recommendation_path = artifact / "recommendation.json" if artifact.is_dir() else artifact
                try:
                    raw_recommendation = json.loads(recommendation_path.read_text())
                    result = validate_recommendation_bundle(
                        recommendation_path,
                        expected_target=args.expected_target,
                        require_product_profile=bool(raw_recommendation.get("capture_bundle_digest")),
                    )
                except ValueError as error:
                    raise AdvisorCLIError(str(error), ExitCode.CORRUPT_ARTIFACT) from error
                result["validity"] = "valid"
                print(json.dumps({key: result[key] for key in ("format_version", "evaluated_statistics_target", "selected_design_digest", "digest", "validity")}, sort_keys=True))
            elif (artifact / "prepare-summary.json").exists():
                if not args.validation_dsn:
                    raise AdvisorCLIError("legacy physical validation requires --validation-dsn", ExitCode.USAGE)
                with psycopg.connect(_dsn(args.validation_dsn, "PGEXT_VALIDATION_DSN")) as connection:
                    summary = execute_validation_stage(artifact, connection)
                print(f"fresh objective: {summary['fresh_objective']}")
            else:
                raise AdvisorCLIError("artifact is not a capture bundle or recommendation", ExitCode.USAGE)
        elif args.command == "inspect":
            bundle = RecommendationBundle.load(args.artifact)
            selected = bundle.selected_objects
            mcv = sum(item.get("mechanism") == "mcv" for item in selected)
            fd = sum(item.get("mechanism") == "fd" for item in selected)
            print(json.dumps({"evaluated_statistics_target": bundle.evaluated_statistics_target, "baseline_objective": bundle.baseline_objective, "final_objective": bundle.final_objective, "selected_count": len(bundle.selected_design), "mcv_count": mcv, "fd_count": fd, "selected_maintenance_cost": bundle.selected_maintenance_cost, "statistics_names": [item.get("statistics_name") for item in selected]}, sort_keys=True))
        elif args.command == "preflight":
            report = run_preflight(args.recommendation, _dsn(args.production_dsn, "PGEXT_PRODUCTION_DSN"))
            exit_code = int(report.pop("_exit_code", 0))
            if args.json:
                print(json.dumps(report, sort_keys=True))
            else:
                for check in report["checks"]:
                    status = check["status"]
                    suffix = f": {check['message']}" if check.get("message") else ""
                    print(f"{check['name']}: {status}{suffix}")
                    if status == "FAIL":
                        if check["name"] != "schema" and "expected" in check:
                            print(f"  expected: {check['expected']}")
                        if check["name"] != "schema" and "observed" in check:
                            print(f"  observed: {check['observed']}")
                        if check["name"] == "schema":
                            print("  details: rerun with --json for the portable schema diff")
                print(f"\nPRE-FLIGHT: {report['status']}")
                print(f"recommendation digest: {report['recommendation_digest']}")
            return exit_code
        elif args.command in {"verify-deployment", "verify-rollback"}:
            runner = verify_deployment if args.command == "verify-deployment" else verify_rollback
            report = runner(args.recommendation, _dsn(args.production_dsn, "PGEXT_PRODUCTION_DSN"))
            exit_code = int(report.pop("_exit_code", 0))
            if args.output is not None:
                write_verification_report(args.output, report)
            if args.json:
                print(json.dumps(report, sort_keys=True))
            elif args.output is not None:
                print(f"report: {args.output}")
            if not args.json:
                for detail in report.get("objects", []):
                    if args.verbose or detail["status"] not in {"DEFINITION_PRESENT_DATA_PRESENT", "ABSENT"}:
                        print(f"{detail['expected_name']}: {detail['status']}")
                label = "DEPLOYMENT VERIFICATION" if args.command == "verify-deployment" else "ROLLBACK VERIFICATION"
                print(f"\n{label}: {report['status']}")
            return exit_code
        elif args.command == "cleanup-acquisition":
            with psycopg.connect(_dsn(args.acquisition_dsn, "PGEXT_ACQUISITION_DSN")) as connection:
                cleanup_acquisition_stage(args.run_dir, connection)
            print("acquisition handles cleaned")
        elif args.command == "run":
            config = _prepare(args.config)
            with psycopg.connect(config.acquisition_dsn) as connection:
                execute_search_stage(config.output_path, connection, args.budget)
            execute_recommendation_stage(config.output_path)
            if args.validate:
                with psycopg.connect(
                    _dsn(args.validation_dsn, "PGEXT_VALIDATION_DSN")
                ) as connection:
                    execute_validation_stage(config.output_path, connection)
            print(
                f"recommendation: {config.output_path / 'recommendation' / 'recommendation.json'}"
            )
        return 0
    except AdvisorCLIError as error:
        print(f"error[{error.code.name}]: {error}", file=sys.stderr)
        if getattr(args, "verbose", False):
            traceback.print_exc()
        return int(error.code)
    except Exception as error:  # noqa: BLE001 - CLI boundary converts failures to exit status
        print(f"error[{ExitCode.EXECUTION.name}]: {error}", file=sys.stderr)
        if getattr(args, "verbose", False):
            traceback.print_exc()
        return int(ExitCode.EXECUTION)


if __name__ == "__main__":
    raise SystemExit(main())
