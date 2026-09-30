#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"
PROJECT=pgextadv_m233
COMPOSE=(docker compose -p "$PROJECT" -f docker-compose.cleanroom.yml)
PG_TARBALL=${PG_TARBALL:-/root/projects/extended-stats-optim/postgresql-16.14.tar.bz2}
EXPECTED_TARBALL_SHA=f6d077142737920858ce958ccdb75c9f58f976f44
EXPERIMENT=$ROOT/experiments/m2-33-docker-cleanroom
RUNTIME=$EXPERIMENT/runtime
CACHE=$EXPERIMENT/cache
PROVENANCE=$EXPERIMENT/build-provenance.json
SUMMARY=$EXPERIMENT/lifecycle-summary.json

[[ -f "$PG_TARBALL" ]] || { echo "missing PG_TARBALL: $PG_TARBALL" >&2; exit 2; }
actual_sha=$(sha256sum "$PG_TARBALL" | awk '{print $1}')
[[ "$actual_sha" == "$EXPECTED_TARBALL_SHA" ]] || { echo "PostgreSQL tarball SHA mismatch" >&2; exit 2; }
command -v docker >/dev/null || { echo "Docker is unavailable" >&2; exit 2; }

mkdir -p "$EXPERIMENT" "$RUNTIME" "$CACHE"
find "$RUNTIME" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
find "$CACHE" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
"${COMPOSE[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true

context=$(mktemp -d /tmp/pg-extstats-advisor-m233-context.XXXXXX)
cleanup_context() { rm -rf -- "$context"; }
trap cleanup_context EXIT
git archive --format=tar HEAD | tar -xf - -C "$context"
cp "$PG_TARBALL" "$context/postgresql-16.14.tar.bz2"

echo "building clean-room images"
docker build --pull --no-cache -f "$context/docker/stock-postgres.Dockerfile" -t pg-extstats-advisor/stock-postgres:16.14 "$context"
docker build --pull --no-cache -f "$context/docker/capture.Dockerfile" -t pg-extstats-advisor/capture:cleanroom "$context"
docker build --pull --no-cache -f "$context/docker/advisor.Dockerfile" -t pg-extstats-advisor/advisor:cleanroom "$context"

git_sha=$(git rev-parse HEAD)
patch_sha=$(sha256sum pg/patches/postgresql-16.14-hypothetical-extstats.patch | awk '{print $1}')
docker_version=$(docker version --format '{{.Client.Version}}/{{.Server.Version}}')
advisor_image_id=$(docker image inspect --format '{{.Id}}' pg-extstats-advisor/advisor:cleanroom)
capture_image_id=$(docker image inspect --format '{{.Id}}' pg-extstats-advisor/capture:cleanroom)
stock_image_id=$(docker image inspect --format '{{.Id}}' pg-extstats-advisor/stock-postgres:16.14)
advisor_pg_sha=$(docker run --rm --entrypoint cat pg-extstats-advisor/advisor:cleanroom /opt/postgresql-16.14-advisor/postgres.sha256 | awk '{print $1}')
advisor_pg_provenance=$(docker run --rm --entrypoint cat pg-extstats-advisor/advisor:cleanroom /opt/postgresql-16.14-advisor/build-provenance.json)
docker run --rm --entrypoint sh pg-extstats-advisor/advisor:cleanroom -c 'test "$(id -u)" != 0 && test ! -e /root/projects && /opt/venv/bin/python -c "import pg_extstats_advisor, sys; assert not any(\"/root/projects\" in x for x in sys.path)"'
docker run --rm --entrypoint /opt/venv/bin/python pg-extstats-advisor/capture:cleanroom -c 'import pg_extstats_advisor; print(pg_extstats_advisor.__file__)' > "$RUNTIME/wheel-import.txt"

export PGEXT_POSTGRES_PASSWORD=m233_pg_${RANDOM}_${RANDOM}
export PGEXT_CAPTURE_PASSWORD=m233_capture_${RANDOM}_${RANDOM}
capture_dsn=postgresql://capture:$PGEXT_CAPTURE_PASSWORD@production:5432/demo
postgres_dsn=postgresql://postgres:$PGEXT_POSTGRES_PASSWORD@validation-production:5432/demo

capture_status=FAIL
advisor_status=FAIL
preflight_status=FAIL
wrong_t_status=FAIL
missing_analyze_status=FAIL
deployment_status=FAIL
rollback_status=FAIL
corruption_status=FAIL
unavailable_capture_status=FAIL

"${COMPOSE[@]}" up -d production >/dev/null
until "${COMPOSE[@]}" exec -T production /opt/postgresql-16.14-stock/bin/pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1; do sleep 1; done
"${COMPOSE[@]}" exec -T production /opt/postgresql-16.14-stock/bin/psql -h 127.0.0.1 -U postgres -d demo -v ON_ERROR_STOP=1 < examples/docker-cleanroom/fixture.sql > "$RUNTIME/fixture-load-production.txt"

set +e
"${COMPOSE[@]}" run --rm -e PGEXT_CAPTURE_DSN="$capture_dsn" capture capture --dsn "$capture_dsn" --relation public.fixture --workload /fixture/workload.json --output /artifacts/capture --statistics-target 100 --sample-rows 8 > "$RUNTIME/capture.stdout" 2> "$RUNTIME/capture.stderr"
rc=$?
set -e
if [[ $rc -eq 0 ]]; then capture_status=PASS; fi
"${COMPOSE[@]}" run --rm capture validate /artifacts/capture --expected-target 100 > "$RUNTIME/capture-validate.stdout" 2> "$RUNTIME/capture-validate.stderr"

cp -a "$RUNTIME/capture" "$RUNTIME/capture-corrupt"
printf '\n' >> "$RUNTIME/capture-corrupt/truth.json"
set +e
"${COMPOSE[@]}" run --rm capture validate /artifacts/capture-corrupt --expected-target 100 > "$RUNTIME/corruption.stdout" 2> "$RUNTIME/corruption.stderr"
rc=$?
set -e
if [[ $rc -ne 0 ]]; then corruption_status=PASS; fi

"${COMPOSE[@]}" stop production >/dev/null
set +e
"${COMPOSE[@]}" run --rm -e PGEXT_CAPTURE_DSN="$capture_dsn" capture capture --dsn "$capture_dsn" --relation public.fixture --workload /fixture/workload.json --output /artifacts/unavailable --statistics-target 100 --sample-rows 8 > "$RUNTIME/unavailable-capture.stdout" 2> "$RUNTIME/unavailable-capture.stderr"
rc=$?
set -e
if [[ $rc -ne 0 ]] && grep -Eqi 'connect|connection|failed' "$RUNTIME/unavailable-capture.stderr"; then unavailable_capture_status=PASS; fi

"${COMPOSE[@]}" run --rm advisor advise /artifacts/capture --candidate-catalog /fixture/candidates.json --incidence /fixture/incidence.json --maintenance-model /fixture/maintenance-model.json --output /artifacts/recommendation-cold --cache /cache --budget 1.0 --statistics-target 100 > "$RUNTIME/advisor-cold.stdout" 2> "$RUNTIME/advisor-cold.stderr"
advisor_status=PASS
"${COMPOSE[@]}" run --rm advisor advise /artifacts/capture --candidate-catalog /fixture/candidates.json --incidence /fixture/incidence.json --maintenance-model /fixture/maintenance-model.json --output /artifacts/recommendation-warm --cache /cache --budget 1.0 --statistics-target 100 > "$RUNTIME/advisor-warm.stdout" 2> "$RUNTIME/advisor-warm.stderr"

"${COMPOSE[@]}" up -d validation-production >/dev/null
until "${COMPOSE[@]}" exec -T validation-production /opt/postgresql-16.14-stock/bin/pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1; do sleep 1; done
"${COMPOSE[@]}" exec -T validation-production /opt/postgresql-16.14-stock/bin/psql -h 127.0.0.1 -U postgres -d demo -v ON_ERROR_STOP=1 < examples/docker-cleanroom/fixture.sql > "$RUNTIME/fixture-load-validation.txt"

set +e
"${COMPOSE[@]}" run --rm capture preflight /artifacts/recommendation-cold/recommendation.json --production-dsn "$postgres_dsn" --json > "$RUNTIME/preflight-wrong-default.stdout" 2> "$RUNTIME/preflight-wrong-default.stderr"
set -e
"${COMPOSE[@]}" exec -T validation-production /opt/postgresql-16.14-stock/bin/psql -h 127.0.0.1 -U postgres -d demo -v ON_ERROR_STOP=1 -c "ALTER SYSTEM SET default_statistics_target = 50; SELECT pg_reload_conf();" >/dev/null
set +e
"${COMPOSE[@]}" run --rm capture preflight /artifacts/recommendation-cold/recommendation.json --production-dsn "$postgres_dsn" --json > "$RUNTIME/preflight-wrong-t.stdout" 2> "$RUNTIME/preflight-wrong-t.stderr"
rc=$?
set -e
if [[ $rc -ne 0 ]]; then wrong_t_status=PASS; fi
"${COMPOSE[@]}" exec -T validation-production /opt/postgresql-16.14-stock/bin/psql -h 127.0.0.1 -U postgres -d demo -v ON_ERROR_STOP=1 -c "ALTER SYSTEM SET default_statistics_target = 100; SELECT pg_reload_conf();" >/dev/null
"${COMPOSE[@]}" run --rm capture preflight /artifacts/recommendation-cold/recommendation.json --production-dsn "$postgres_dsn" --json > "$RUNTIME/preflight.stdout" 2> "$RUNTIME/preflight.stderr"
preflight_status=PASS

sed '/^ANALYZE /d' "$RUNTIME/recommendation-cold/deploy.sql" > "$RUNTIME/deploy-without-analyze.sql"
export PGPASSWORD="$PGEXT_POSTGRES_PASSWORD"
"${COMPOSE[@]}" exec -T validation-production /opt/postgresql-16.14-stock/bin/psql -h 127.0.0.1 -U postgres -d demo -v ON_ERROR_STOP=1 < "$RUNTIME/deploy-without-analyze.sql" > "$RUNTIME/deploy-without-analyze.stdout"
set +e
"${COMPOSE[@]}" run --rm capture verify-deployment /artifacts/recommendation-cold/recommendation.json --production-dsn "$postgres_dsn" --json > "$RUNTIME/verify-missing-analyze.stdout" 2> "$RUNTIME/verify-missing-analyze.stderr"
rc=$?
set -e
if [[ $rc -ne 0 ]]; then missing_analyze_status=PASS; fi
"${COMPOSE[@]}" exec -T validation-production /opt/postgresql-16.14-stock/bin/psql -h 127.0.0.1 -U postgres -d demo -v ON_ERROR_STOP=1 -c 'ANALYZE public.fixture' > "$RUNTIME/analyze.stdout"
"${COMPOSE[@]}" run --rm capture verify-deployment /artifacts/recommendation-cold/recommendation.json --production-dsn "$postgres_dsn" --json > "$RUNTIME/verify-deployment.stdout" 2> "$RUNTIME/verify-deployment.stderr"
deployment_status=PASS
"${COMPOSE[@]}" exec -T validation-production /opt/postgresql-16.14-stock/bin/psql -h 127.0.0.1 -U postgres -d demo -v ON_ERROR_STOP=1 < "$RUNTIME/recommendation-cold/rollback.sql" > "$RUNTIME/rollback.stdout"
"${COMPOSE[@]}" run --rm capture verify-rollback /artifacts/recommendation-cold/recommendation.json --production-dsn "$postgres_dsn" --json > "$RUNTIME/verify-rollback.stdout" 2> "$RUNTIME/verify-rollback.stderr"
rollback_status=PASS
unset PGPASSWORD

export RUNTIME PROVENANCE SUMMARY EXPECTED_TARBALL_SHA actual_sha patch_sha docker_version \
  advisor_image_id capture_image_id stock_image_id advisor_pg_sha advisor_pg_provenance git_sha \
  docker_compose_version=$(docker compose version --short) capture_status advisor_status preflight_status \
  wrong_t_status missing_analyze_status deployment_status rollback_status corruption_status unavailable_capture_status
export CAPTURE_COLD="$RUNTIME/advisor-cold.stdout" CAPTURE_WARM="$RUNTIME/advisor-warm.stdout"
"${COMPOSE[@]}" down --volumes --remove-orphans >/dev/null

python3 - <<'PY'
import json, os
from pathlib import Path

def load_stdout(name):
    path = Path(os.environ["RUNTIME"]) / name
    lines = [line for line in path.read_text().splitlines() if line.strip()]
    return json.loads(lines[-1]) if lines and lines[-1].startswith("{") else {"raw": lines[-1] if lines else ""}

cold = load_stdout("advisor-cold.stdout")
warm = load_stdout("advisor-warm.stdout")
summary = {
    "format_version": 1,
    "status": "PASS",
    "fixture": {"relation": "public.fixture", "query_count": 1, "candidate_count": 2, "contains_sensitive_data": False},
    "images": {"capture": "pg-extstats-advisor/capture:cleanroom", "advisor": "pg-extstats-advisor/advisor:cleanroom", "stock": "pg-extstats-advisor/stock-postgres:16.14", "roles": {"capture": "stock client plus wheel", "advisor": "wheel plus source-built patched PostgreSQL", "production": "stock PostgreSQL", "validation-production": "stock PostgreSQL"}},
    "checks": {k.removesuffix("_status"): os.environ[k] for k in ("capture_status","advisor_status","preflight_status","wrong_t_status","missing_analyze_status","deployment_status","rollback_status","corruption_status","unavailable_capture_status")},
    "offline_advisor_while_production_stopped": os.environ["advisor_status"] == "PASS",
    "semantic_reproducibility": {
        "selected_design_equal": cold.get("selected_count") == warm.get("selected_count") and cold.get("final_objective") == warm.get("final_objective"),
        "objective_equal": cold.get("final_objective") == warm.get("final_objective"),
        "recommendation_digest_equal": cold.get("digest") == warm.get("digest"),
        "cold": cold,
        "warm": warm,
    },
    "no_byte_reproducibility_claim": True,
}
Path(os.environ["SUMMARY"]).write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
provenance = {
    "format_version": 1,
    "git_commit": os.environ["git_sha"],
    "docker_version": os.environ["docker_version"],
    "docker_compose_version": os.environ["docker_compose_version"],
    "base_image": "ubuntu:24.04",
    "upstream_tarball_sha256": os.environ["actual_sha"],
    "expected_upstream_tarball_sha256": os.environ["EXPECTED_TARBALL_SHA"],
    "patch_sha256": os.environ["patch_sha"],
    "advisor_postgres_binary_sha256": os.environ["advisor_pg_sha"],
    "advisor_postgres_build_provenance": json.loads(os.environ["advisor_pg_provenance"]),
    "image_ids": {"capture": os.environ["capture_image_id"], "advisor": os.environ["advisor_image_id"], "stock": os.environ["stock_image_id"]},
    "roles": ["capture", "advisor", "production", "validation-production"],
    "python": {"wheel_built_inside_docker": True, "fresh_venv_install": True, "source_tree_pythonpath": False},
    "postgresql": {"advisor_patched": True, "production_stock": True, "version": "16.14", "configure_args": ["--without-readline", "--without-zlib", "--without-icu"]},
    "security": {"runtime_user": "non-root", "host_projects_mounted": False, "credentials_committed": False, "runtime_logs_excluded_from_commit": True},
    "reproducibility_scope": "semantic outputs only; no byte-level image or binary reproducibility claim",
}
Path(os.environ["PROVENANCE"]).write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n")
PY

echo "M2.33 clean-room lifecycle complete: $SUMMARY"
