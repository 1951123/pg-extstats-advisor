"""Non-interactive command-line interface for restartable MVP stages."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

import psycopg

from pg_extstats_advisor.calibration import CalibrationConfig, run_calibration
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


def _dsn(value: str | None, env_name: str) -> str:
    result = value or os.environ.get(env_name)
    if not result:
        raise ValueError(f"DSN required via option or {env_name}")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pg-extstats-advisor", description="Offline PostgreSQL extended-statistics advisor"
    )
    commands = parser.add_subparsers(dest="command", required=True)
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
    validate = commands.add_parser("validate", help="physically validate persisted selected state")
    validate.add_argument("run_dir", type=Path)
    validate.add_argument("--validation-dsn")
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
        if args.command == "calibrate-maintenance":
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
            with psycopg.connect(_dsn(args.validation_dsn, "PGEXT_VALIDATION_DSN")) as connection:
                summary = execute_validation_stage(args.run_dir, connection)
            print(f"fresh objective: {summary['fresh_objective']}")
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
    except Exception as error:  # noqa: BLE001 - CLI boundary converts failures to exit status
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
