import hashlib

from agents.contracts import AgentDecision


def analyze_goal(goal: str) -> dict[str, int | bool | str]:
    return {
        "goal_characters": len(goal),
        "goal_words": len(goal.split()),
        "goal_nonempty": bool(goal.strip()),
        "goal_fingerprint": hashlib.sha256(goal.encode("utf-8")).hexdigest(),
    }


def analyze_completion(
    answer: str, input_tokens: int, output_tokens: int
) -> AgentDecision:
    return AgentDecision(
        agent="analytics",
        status="measured" if answer.strip() else "quality_warning",
        evidence={
            "answer_characters": len(answer),
            "answer_nonempty": bool(answer.strip()),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "quality_reviewed_by_human": False,
        },
    )
