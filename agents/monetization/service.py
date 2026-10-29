from agents.monetization.token_pricing import quote_token_usage as price_usage
from agents.contracts import AgentDecision

__all__ = ["price_usage"]


def assess_run_cost(
    input_tokens: int, output_tokens: int, monthly_tokens_before: int
) -> tuple[AgentDecision, dict[str, int]]:
    quote = price_usage(input_tokens, output_tokens, monthly_tokens_before)
    return (
        AgentDecision(
            agent="monetization",
            status="estimated",
            evidence={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "monthly_tokens_before": monthly_tokens_before,
                "provider_cost_micro_usd": quote["provider_cost_micro_usd"],
                "sell_price_micro_usd": quote["sell_price_micro_usd"],
                "markup_bps": quote["markup_bps"],
                "payment_confirmed": False,
            },
        ),
        quote,
    )
