from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from config.settings import max_queued_runs_per_tenant
from database.models import AgentRunRecord, UsageRecord
from monitoring.health import agent_health

SPECIALIST_MODULES = (
    {
        "agent": "monetization",
        "module": "agents.monetization.token_pricing",
        "mode": "local_module",
        "capabilities": ["token_cost_estimates", "tiered_pricing"],
    },
    {
        "agent": "devops",
        "module": "monitoring.health",
        "mode": "monitoring_only",
        "capabilities": ["health_checks", "alerts"],
    },
    {
        "agent": "resources",
        "module": "agents.resources.service",
        "mode": "measurement_helper",
        "capabilities": ["resource_measurement_helper"],
    },
    {
        "agent": "security",
        "module": "agents.security.service",
        "mode": "guardrails_only",
        "capabilities": ["approval_gate", "restricted_no_tools_execution"],
    },
    {
        "agent": "analytics",
        "module": "api.routes.admin_metrics",
        "mode": "metrics_only",
        "capabilities": ["usage_and_operational_metrics"],
    },
)


def build_executive_report(session: Session) -> dict:
    components = agent_health(session)
    run_counts = dict(
        session.execute(
            select(AgentRunRecord.status, func.count()).group_by(AgentRunRecord.status)
        ).all()
    )
    tenant_active_counts = session.execute(
        select(AgentRunRecord.tenant_id, func.count())
        .where(AgentRunRecord.status.in_(("queued", "processing")))
        .group_by(AgentRunRecord.tenant_id)
    ).all()
    tenants_over_capacity = sum(
        count > max_queued_runs_per_tenant() for _, count in tenant_active_counts
    )
    usage = session.execute(
        select(
            func.count(UsageRecord.id),
            func.coalesce(func.sum(UsageRecord.input_tokens), 0),
            func.coalesce(func.sum(UsageRecord.output_tokens), 0),
            func.coalesce(func.sum(UsageRecord.provider_cost_micro_usd), 0),
            func.coalesce(func.sum(UsageRecord.sell_price_micro_usd), 0),
        )
    ).one()
    queue = {
        "queued": run_counts.get("queued", 0),
        "processing": run_counts.get("processing", 0),
        "failed": run_counts.get("failed", 0),
        "total": sum(run_counts.values()),
    }
    unhealthy_components = [
        item["agent"] for item in components if item["status"] != "healthy"
    ]
    recommendations = []
    if unhealthy_components:
        recommendations.append(
            {
                "priority": "high",
                "action": "Investigate missing or unhealthy runtime heartbeats.",
                "evidence": unhealthy_components,
            }
        )
    if tenants_over_capacity:
        recommendations.append(
            {
                "priority": "medium",
                "action": "Review tenant queue limits and worker capacity.",
                "evidence": {"tenants_over_configured_limit": tenants_over_capacity},
            }
        )
    if queue["failed"]:
        recommendations.append(
            {
                "priority": "medium",
                "action": "Review failed runs before manually resubmitting them.",
                "evidence": {"failed_runs": queue["failed"]},
            }
        )
    if not usage[0]:
        recommendations.append(
            {
                "priority": "low",
                "action": "Run a controlled test task to establish a usage baseline.",
                "evidence": {"usage_events": 0},
            }
        )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "degraded" if unhealthy_components else "healthy",
        "decision_mode": "read_only_advisory",
        "task_routing": "one GPT-4o completion per queued run; no specialist tool calls",
        "runtime_components": components,
        "specialist_modules": list(SPECIALIST_MODULES),
        "queue": queue,
        "usage": {
            "events": usage[0],
            "input_tokens": usage[1],
            "output_tokens": usage[2],
            "total_tokens": usage[1] + usage[2],
            "estimated_provider_cost_micro_usd": usage[3],
            "estimated_sell_price_micro_usd": usage[4],
            "revenue_source": "estimated_usage; payment reconciliation is separate",
        },
        "recommendations": recommendations[:3],
        "automated_actions_taken": [],
    }
