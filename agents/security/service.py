from agents.contracts import AgentDecision


def assess_request(goal: str, requires_approval: bool) -> AgentDecision:
    risk_terms = {
        "delete": ("delete", "erase", "drop database"),
        "financial": ("payment", "transfer", "purchase", "refund", "charge"),
        "infrastructure": ("deploy", "restart server", "change firewall"),
        "sensitive_data": ("password", "secret key", "personal data"),
    }
    categories = sorted(
        category
        for category, terms in risk_terms.items()
        if any(term in goal.casefold() for term in terms)
    )
    return AgentDecision(
        agent="security",
        status="review_required" if requires_approval or categories else "allowed",
        evidence={
            "approval_requested": requires_approval,
            "risk_categories": categories,
            "tools_available": False,
            "external_actions_permitted": False,
        },
    )


def assess_risk(requires_approval: bool) -> tuple[str, str]:
    decision = assess_request("", requires_approval)
    if decision.status == "review_required":
        return "approval_required", "Risky action held for administrator approval."
    return "completed", "No external action requested."
