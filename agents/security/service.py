def assess_risk(requires_approval: bool) -> tuple[str, str]:
    if requires_approval:
        return "approval_required", "Risky action held for administrator approval."
    return "completed", "No external action requested."
