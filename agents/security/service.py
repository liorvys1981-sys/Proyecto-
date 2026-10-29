import unicodedata

from agents.contracts import AgentDecision


def assess_request(goal: str, requires_approval: bool) -> AgentDecision:
    normalized_goal = "".join(
        character
        for character in unicodedata.normalize("NFKD", goal.casefold())
        if not unicodedata.combining(character)
    )
    risk_terms = {
        "delete": (
            "delete",
            "erase",
            "drop database",
            "drop table",
            "elimina",
            "eliminar",
            "borra",
            "borrar",
            "suprime",
            "suprimir",
            "destruye",
            "destruir",
        ),
        "financial": (
            "payment",
            "transfer",
            "transfiere",
            "transferir",
            "purchase",
            "refund",
            "charge",
            "pago",
            "pagar",
            "compra",
            "comprar",
            "reembolso",
            "devolucion",
            "reembolsar",
            "cobrar",
            "cargo",
        ),
        "infrastructure": (
            "deploy",
            "restart server",
            "change firewall",
            "desplegar",
            "reiniciar servidor",
            "reiniciar el servidor",
            "reinicia servidor",
            "reinicia el servidor",
            "cambiar firewall",
            "cortafuegos",
        ),
        "sensitive_data": (
            "password",
            "secret key",
            "personal data",
            "contrasena",
            "clave secreta",
            "clave api",
            "datos personales",
            "datos sensibles",
            "informacion personal",
            "informacion confidencial",
        ),
    }
    categories = sorted(
        category
        for category, terms in risk_terms.items()
        if any(term in normalized_goal for term in terms)
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
