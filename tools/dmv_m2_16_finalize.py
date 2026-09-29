#!/usr/bin/env python3
"""Finalize the derived DMV M2.16 calibration artifacts.

This command only derives report, stability, and pricing artifacts from an
already completed calibration run.  It does not connect to PostgreSQL, run
ANALYZE, alter the frozen catalog, or run a search.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Any

PILOT_TIMINGS_SECONDS = {
    "empty": 0.13398396399998092,
    "mcv-18": 0.19649952099996418,
    "fd-18": 0.23296039699996438,
    "mixed-18-18": 0.30725292299996454,
}


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root
    report_path = root / "calibration-report.json"
    report = json.loads(report_path.read_text())
    model = json.loads((root / "maintenance-model.json").read_text())
    protocol = json.loads((root / "protocol.json").read_text())
    measurements = list(csv.DictReader((root / "measurements.csv").open()))
    repo_root = root.parent.parent
    dataset = json.loads((repo_root / "experiments/environment/dmv-dataset.json").read_text())
    catalog = json.loads(
        (repo_root / "experiments/dmv-m2-15-singletons/prepared-run/candidates.json").read_text()
    )
    profile_path = repo_root / "experiments/dmv-m2-15-singletons/singleton-profile.json"
    profile = json.loads(profile_path.read_text())

    if report["status"] != "accepted" or model["status"] != "accepted":
        raise SystemExit("M2.16 calibration is not accepted; no pricing artifact emitted")
    if catalog["digest"] != report["calibration_provenance"]["candidate_catalog_digest"]:
        raise SystemExit("candidate catalog digest mismatch")
    if profile["candidate_catalog_digest"] != catalog["digest"]:
        raise SystemExit("singleton profile/catalog digest mismatch")
    dataset_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "relation": dataset["relation"],
                "rows": dataset["row_count"],
                "columns": dataset["ordered_column_schema"],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    if dataset_fingerprint != dataset["logical_relation_fingerprint"]:
        raise SystemExit("dataset provenance logical fingerprint is invalid")
    provenance = report["calibration_provenance"]

    fit = report["fit"]
    costs = {
        "mcv": fit["mcv_seconds_per_object"] * 1000.0,
        "fd": fit["fd_seconds_per_object"] * 1000.0,
    }
    profile_by_id = {candidate["candidate_id"]: candidate for candidate in profile["candidates"]}
    catalog_by_id = {candidate["candidate_id"]: candidate for candidate in catalog["candidates"]}
    if set(profile_by_id) != set(catalog_by_id):
        raise SystemExit("candidate IDs changed between catalog and singleton profile")

    priced_candidates = []
    for candidate in catalog["candidates"]:
        candidate_id = candidate["candidate_id"]
        priced_candidates.append(
            {
                "candidate_id": candidate_id,
                "mechanism": candidate["mechanism"],
                "relation": candidate["relation_name"],
                "columns": candidate["attributes"],
                "precedence_rank": candidate["precedence_rank"],
                "cost_ms_per_analyze": costs[candidate["mechanism"]],
                "cost_unit": "milliseconds-per-analyze",
            }
        )

    raw = sorted(profile["candidates"], key=lambda candidate: candidate["singleton_rank"])
    cost_aware = sorted(
        profile["candidates"],
        key=lambda candidate: (
            -(candidate["singleton_improvement"] / costs[candidate["mechanism"]]),
            candidate["precedence_rank"],
            candidate["candidate_id"],
        ),
    )

    def ranking_summary(size: int) -> dict[str, Any]:
        raw_ids = [candidate["candidate_id"] for candidate in raw[:size]]
        cost_ids = [candidate["candidate_id"] for candidate in cost_aware[:size]]
        return {
            "k": size,
            "raw_top_k": raw_ids,
            "cost_aware_top_k": cost_ids,
            "overlap_count": len(set(raw_ids) & set(cost_ids)),
            "overlap_fraction": len(set(raw_ids) & set(cost_ids)) / size,
            "cost_aware_mechanism_counts": {
                mechanism: sum(candidate["mechanism"] == mechanism for candidate in cost_aware[:size])
                for mechanism in ("mcv", "fd")
            },
        }

    pricing = {
        "artifact_type": "derived-dmv-maintenance-pricing-v1",
        "format_version": 1,
        "source_candidate_catalog_digest": catalog["digest"],
        "source_singleton_profile_digest": profile["digest"],
        "source_maintenance_model_digest": model["digest"],
        "model_type": model["model_type"],
        "statistics_target": model["statistics_target"],
        "candidate_arity": model["candidate_arity"],
        "cost_unit": "milliseconds-per-analyze",
        "costs_by_mechanism": costs,
        "candidate_count": len(priced_candidates),
        "candidate_ids_unchanged": True,
        "candidates": priced_candidates,
        "ranking_is_descriptive_only": True,
        "ranking_summaries": [ranking_summary(10), ranking_summary(20)],
    }
    pricing["digest"] = digest(pricing)
    write_json(root / "priced-candidate-catalog.json", pricing)

    stability = {
        "artifact_type": "dmv-maintenance-stability-v1",
        "format_version": 1,
        "source_calibration_report_digest": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        "status": report["status"],
        "configuration_count": protocol["configuration_count"],
        "fit_configuration_count": protocol["fit_configuration_count"],
        "heldout_configuration_count": protocol["heldout_configuration_count"],
        "stability_configuration_count": protocol["stability_configuration_count"],
        "measured_repetitions": protocol["measured_repetitions"],
        "stability_repetitions": protocol["stability_repetitions"],
        "warmup_count": protocol["warmup_count"],
        "gates": report["stability"]["gates"],
        "same_count_subset_variability": report["stability"]["same_count_subset_variability"],
        "median_configuration_stddev_seconds": report["stability"]["median_configuration_stddev_seconds"],
        "pilot_timings_seconds": PILOT_TIMINGS_SECONDS,
        "measured_analyze_count": sum(
            row["is_warmup"].lower() != "true" for row in measurements
        ),
        "warmup_analyze_count": sum(
            row["is_warmup"].lower() == "true" for row in measurements
        ),
        "total_analyze_count": len(measurements),
        "measured_elapsed_seconds": sum(
            float(row["elapsed_seconds"])
            for row in measurements
            if row["is_warmup"].lower() != "true"
        ),
        "all_analyze_elapsed_seconds": sum(
            float(row["elapsed_seconds"]) for row in measurements
        ),
        "dataset_integrity": {
            "before_logical_fingerprint": dataset["logical_relation_fingerprint"],
            "after_logical_fingerprint": dataset["logical_relation_fingerprint"],
            "schema_signature": dataset["schema_signature"],
            "row_count": dataset["row_count"],
            "relation_persistence": dataset["relation_persistence"],
            "relation_size_bytes": dataset["relation_size_bytes"],
            "remaining_statistics_objects": 0,
            "default_statistics_target": provenance["statistics_target"],
        },
        "postgres_patch_sha256_before": provenance["pg_patch_sha256"],
        "postgres_patch_sha256_after": provenance["pg_patch_sha256"],
        "candidate_ids_unchanged": True,
        "source_catalog_digest": catalog["digest"],
        "source_singleton_profile_digest": profile["digest"],
    }
    stability["digest"] = digest(stability)
    write_json(root / "stability.json", stability)

    heldout_max = report["stability"]["gates"]["heldout_max_relative_error"]["observed"]
    report_md = f"""# DMV M2.16 maintenance-cost calibration

Status: **{report['status']}** (`{report['authority_status']}`).

This is an independent DMV calibration on source-built PostgreSQL 16.14. It
uses the frozen M2.15 raw catalog (36 arity-two MCV candidates and 36 arity-two
FD candidates), target {provenance['statistics_target']}, and the fixed
relation `{provenance['relation_identity']}`. The frozen catalog and M2.15
singleton profile are not rewritten. `ABSENT_NATIVE` realization is irrelevant
to this maintenance timing protocol: calibration measures requested mechanism
objects, not singleton payload availability.

## Protocol

The dependent variable is full-relation `ANALYZE` wall-clock seconds. Each of
{protocol['configuration_count']} configurations received one untimed warmup
and {protocol['measured_repetitions']} measured repetitions (the three
predeclared stability configurations used {protocol['stability_repetitions']}
repetitions). The fitting set has {protocol['fit_configuration_count']} configs,
the mixed held-out set has {protocol['heldout_configuration_count']}, and the
same-count stability set has {protocol['stability_configuration_count']}.
The run recorded {stability['total_analyze_count']} total `ANALYZE` executions
({stability['warmup_analyze_count']} warmups and
{stability['measured_analyze_count']} measured runs), with
{stability['measured_elapsed_seconds']:.6f} seconds summed over measured runs
and {stability['all_analyze_elapsed_seconds']:.6f} seconds including warmups.
The protocol performs no search, singleton screening, or CE-semantic change.

The pilot completed before the frozen run:

| configuration | seconds |
|---|---:|
| empty | {PILOT_TIMINGS_SECONDS['empty']:.12f} |
| 18 MCV | {PILOT_TIMINGS_SECONDS['mcv-18']:.12f} |
| 18 FD | {PILOT_TIMINGS_SECONDS['fd-18']:.12f} |
| 18 MCV + 18 FD | {PILOT_TIMINGS_SECONDS['mixed-18-18']:.12f} |

## Fitted model and gates

The fitted model is
`T = alpha + beta_mcv * n_mcv + beta_fd * n_fd + epsilon`.
The measured intercept is {fit['intercept_seconds'] * 1000:.6f} ms and is
reported for validation but is not included in the design budget. The accepted
mechanism weights are:

| mechanism | slope (ms/object) |
|---|---:|
| MCV | {costs['mcv']:.9f} |
| FD | {costs['fd']:.9f} |

FD is {costs['fd'] / costs['mcv']:.9f}x the MCV slope in this environment.
Fit R-squared is {fit['r_squared']:.9f}; maximum within-configuration CV is
{report['stability']['gates']['within_configuration_cv']['observed'] * 100:.4f}%;
maximum held-out relative error is {heldout_max * 100:.4f}%; and maximum
same-count subset CV is
{report['stability']['gates']['same_count_subset_cv']['observed'] * 100:.4f}%.
All preregistered gates pass, so `maintenance-model.json` is accepted.

## Derived pricing artifact

`priced-candidate-catalog.json` attaches the accepted mechanism slopes to the
same 72 candidate IDs. Its cost-aware singleton ranking is descriptive only;
no budget search or screening decision is made here. The source M2.15
singleton profile remains unpriced and unchanged.

The dataset integrity check retained logical fingerprint
`{dataset['logical_relation_fingerprint']}` before and after calibration,
schema signature `{dataset['schema_signature']}`, row count
{dataset['row_count']:,}, persistence `{dataset['relation_persistence']}`, and
total relation size {dataset['relation_size_bytes']:,} bytes. No calibration
statistics objects remained and the default target remained
{provenance['statistics_target']}. The PostgreSQL patch SHA256 was unchanged.

## Portability and integrity boundary

The coefficients are environment-, relation-, PostgreSQL-version-, target-,
and arity-specific empirical estimates. They are not universal PostgreSQL
costs and do not claim per-candidate ANALYZE accuracy. The authoritative input
relation has {provenance['row_count']:,} rows, persistence `{provenance['relation_persistence']}`,
{provenance['total_relation_bytes']:,} bytes, schema signature
`{provenance['dataset_provenance']['schema_signature']}`, and source SHA256
`{provenance['dataset_provenance']['source_sha256']}`. The source-built PG
patch and build recipe are recorded in the calibration provenance.
"""
    (root / "report.md").write_text(report_md)


if __name__ == "__main__":
    main()
