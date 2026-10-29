import time
from datetime import datetime, timezone
from uuid import uuid4

from agents.analytics.service import analyze_goal
from agents.security.service import assess_risk
from agents.monetization.service import price_usage
from agents.resources.service import measure
from database.models import AgentRunRecord, UsageRecord
from monitoring.health import heartbeat


def _run_with_recovery(session, agent: str, function):
    for attempt in range(2):
        try:
            result = measure(function)
            heartbeat(session, agent)
            return result
        except Exception as error:
            heartbeat(
                session,
                agent,
                "recovering" if attempt == 0 else "unhealthy",
                type(error).__name__,
            )
            if attempt == 1:
                raise
            time.sleep(0.05)


def run_workflow(session, tenant_id: str, goal: str, requires_approval: bool):
    now = datetime.now(timezone.utc)
    try:
        analysis, analysis_cpu, analysis_memory = _run_with_recovery(
            session, "analytics", lambda: analyze_goal(goal)
        )
        risk, risk_cpu, risk_memory = _run_with_recovery(
            session, "security", lambda: assess_risk(requires_approval)
        )
    except Exception:
        session.commit()
        raise
    heartbeat(session, "orchestrator")
    steps = [
        {"agent": "analytics", "status": "completed", "detail": analysis},
        {"agent": "security", "status": risk[0], "detail": risk[1]},
    ]
    if requires_approval:
        steps.append(
            {
                "agent": "execution",
                "status": "pending_approval",
                "detail": "No action taken; awaiting administrator approval.",
            }
        )
        run_status = "pending_approval"
        result = "Risky action is paused until an administrator approves it."
    else:
        steps.append(
            {
                "agent": "execution",
                "status": "simulated",
                "detail": "Execution is simulated; no external system was contacted.",
            }
        )
        run_status = "completed"
        result = "Simulation completed. No external systems were contacted."

    run = AgentRunRecord(
        id=str(uuid4()),
        tenant_id=tenant_id,
        goal=goal,
        status=run_status,
        result=result,
        steps=steps,
        created_at=now,
    )
    session.add(run)
    for agent, cpu_milliseconds, memory_bytes in (
        ("analytics", analysis_cpu, analysis_memory),
        ("security", risk_cpu, risk_memory),
    ):
        feature = (
            "goal_analysis" if agent == "analytics" else "risk_assessment"
        )
        billable, operating_cost = price_usage(feature, cpu_milliseconds)
        session.add(
            UsageRecord(
                id=str(uuid4()),
                tenant_id=tenant_id,
                run_id=run.id,
                agent=agent,
                feature=feature,
                cpu_milliseconds=cpu_milliseconds,
                memory_bytes=memory_bytes,
                billable_cents=billable,
                operating_cost_cents=operating_cost,
                created_at=now,
            )
        )
    return run
