"""Deploy component boundary."""

"""Physical deployment planning and execution."""

from pg_extstats_advisor.deploy.bundle import (
    RecommendationBundle,
    validate_recommendation_bundle,
)
from pg_extstats_advisor.deploy.model import DeploymentPlan, DeploymentResult
from pg_extstats_advisor.deploy.preflight import run_preflight
from pg_extstats_advisor.deploy.sql import (
    build_deployment_plan,
    build_rollback_statements,
    build_search_deployment_plan,
)

__all__ = [
    "DeploymentPlan",
    "DeploymentResult",
    "RecommendationBundle",
    "build_deployment_plan",
    "build_rollback_statements",
    "build_search_deployment_plan",
    "run_preflight",
    "validate_recommendation_bundle",
]
