from agents.contracts import AgentDecision
from agents.analytics.service import analyze_goal

MAX_PLAN_STEPS = 6


def plan_run(goal: str, approval_required: bool) -> AgentDecision:
    steps = [
        "security_policy",
        "runtime_diagnostics",
        "gpt4o_completion",
        "token_pricing",
        "result_analytics",
        "persist_audit_and_usage",
    ]
    return AgentDecision(
        agent="orchestrator",
        status="planned",
        evidence={
            **analyze_goal(goal),
            "steps": steps[:MAX_PLAN_STEPS],
            "provider": "gpt-4o",
            "max_provider_calls": 1,
            "automatic_retries": 0,
            "human_approval_requested": approval_required,
        },
    )
