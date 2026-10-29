import resource
import time
from datetime import datetime, timezone
from uuid import uuid4

from agents.analytics.service import analyze_goal
from agents.security.service import assess_risk
from config.settings import cost_for_feature, price_for_feature
from database.models import AgentRunRecord, UsageRecord


def _measure(function):
    cpu_before = time.process_time()
    result = function()
    cpu_milliseconds = max(1, round((time.process_time() - cpu_before) * 1000))
    peak_memory = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    memory_bytes = int(peak_memory if peak_memory > 10**8 else peak_memory * 1024)
    return result, cpu_milliseconds, memory_bytes


def run_workflow(session, tenant_id: str, goal: str, requires_approval: bool):
    now = datetime.now(timezone.utc)
    analysis, analysis_cpu, analysis_memory = _measure(lambda: analyze_goal(goal))
    risk, risk_cpu, risk_memory = _measure(lambda: assess_risk(requires_approval))
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
        billable = (cpu_milliseconds * price_for_feature(feature) + 999) // 1000
        operating_cost = (cpu_milliseconds * cost_for_feature(feature) + 999) // 1000
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
