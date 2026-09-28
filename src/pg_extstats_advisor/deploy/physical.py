"""Explicit physical deployment and fresh-ANALYZE execution."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from psycopg import Connection

from pg_extstats_advisor.deploy.model import CreatedStatistic, DeploymentPlan, DeploymentResult


class PhysicalDeployer:
    def __init__(self, connection: Connection[Any], environment_identity: str):
        if not environment_identity:
            raise ValueError("deployment environment identity is required")
        self.connection = connection
        self.environment_identity = environment_identity

    def deploy(self, plan: DeploymentPlan) -> DeploymentResult:
        started = datetime.now(UTC).isoformat()
        try:
            version = str(self.connection.execute("SHOW server_version").fetchone()[0])
            overlay_reset = self.connection.execute(
                "SELECT to_regprocedure('pg_hypothetical_extstats_reset()')"
            ).fetchone()[0]
            if overlay_reset is not None:
                self.connection.execute("SELECT pg_hypothetical_extstats_reset()")
            created: list[CreatedStatistic] = []
            for candidate, name, statement in zip(
                plan.ordered_candidates,
                plan.statistics_names,
                plan.create_statements,
                strict=True,
            ):
                self.connection.execute(statement)
                schema = (
                    candidate.relation_name.split(".")[0]
                    if "." in candidate.relation_name
                    else "public"
                )
                row = self.connection.execute(
                    "SELECT e.oid,e.stxrelid,e.stxrelid::regclass::text "
                    "FROM pg_statistic_ext e "
                    "JOIN pg_namespace n ON n.oid=e.stxnamespace "
                    "WHERE n.nspname=%s AND e.stxname=%s",
                    (schema, name),
                ).fetchone()
                if row is None:
                    raise RuntimeError(f"created statistic not visible: {name}")
                created.append(
                    CreatedStatistic(
                        str(candidate.candidate_id), name, int(row[0]), int(row[1]), str(row[2])
                    )
                )
            for statement in plan.target_statements:
                self.connection.execute(statement)
            for statement in plan.analyze_statements:
                self.connection.execute(statement)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return DeploymentResult(
            plan=plan,
            created_statistics=tuple(created),
            analyze_commands=plan.analyze_statements,
            environment_identity=self.environment_identity,
            postgres_version=version,
            started_at=started,
            completed_at=datetime.now(UTC).isoformat(),
            success=True,
        )
