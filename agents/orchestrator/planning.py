from agents.contracts import AgentDecision
from agents.analytics.service import analyze_goal

MAX_PLAN_STEPS = 7


def plan_run(goal: str, approval_required: bool) -> AgentDecision:
    steps = [
        {"key": "security_policy", "agent": "security", "depends_on": []},
        {
            "key": "runtime_diagnostics",
            "agent": "devops",
            "depends_on": ["security_policy"],
        },
        {
            "key": "gpt4o_completion",
            "agent": "orchestrator",
            "depends_on": ["runtime_diagnostics"],
        },
        {
            "key": "resources_measurement",
            "agent": "resources",
            "depends_on": ["gpt4o_completion"],
        },
        {
            "key": "token_pricing",
            "agent": "monetization",
            "depends_on": ["resources_measurement"],
        },
        {
            "key": "result_analytics",
            "agent": "analytics",
            "depends_on": ["token_pricing"],
        },
        {
            "key": "persist_audit_and_usage",
            "agent": "orchestrator",
            "depends_on": ["result_analytics"],
        },
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
