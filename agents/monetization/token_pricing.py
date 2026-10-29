from config.settings import (
    openai_input_cost_micro_usd_per_million,
    openai_output_cost_micro_usd_per_million,
    token_price_tiers,
)

MICRO_USD_PER_CENT = 10_000
TOKENS_PER_MILLION = 1_000_000


def markup_for_monthly_usage(monthly_tokens: int) -> int:
    tiers = token_price_tiers()
    applicable = tiers[0]["markup_bps"]
    for tier in tiers:
        if monthly_tokens < tier["monthly_tokens"]:
            break
        applicable = tier["markup_bps"]
    return applicable


def quote_token_usage(
    input_tokens: int, output_tokens: int, monthly_tokens_before: int
) -> dict[str, int]:
    input_provider_cost = ceil_div(
        input_tokens * openai_input_cost_micro_usd_per_million(),
        TOKENS_PER_MILLION,
    )
    output_provider_cost = ceil_div(
        output_tokens * openai_output_cost_micro_usd_per_million(),
        TOKENS_PER_MILLION,
    )
    provider_cost = input_provider_cost + output_provider_cost
    markup_bps = markup_for_monthly_usage(
        monthly_tokens_before + input_tokens + output_tokens
    )
    sell_price = ceil_div(provider_cost * (10_000 + markup_bps), 10_000)
    return {
        "provider_cost_micro_usd": provider_cost,
        "sell_price_micro_usd": sell_price,
        "markup_bps": markup_bps,
        "input_provider_cost_micro_usd": input_provider_cost,
        "output_provider_cost_micro_usd": output_provider_cost,
        "input_sell_price_micro_usd": ceil_div(
            input_provider_cost * (10_000 + markup_bps), 10_000
        ),
        "output_sell_price_micro_usd": ceil_div(
            output_provider_cost * (10_000 + markup_bps), 10_000
        ),
        "billable_cents": ceil_div(sell_price, MICRO_USD_PER_CENT),
        "operating_cost_cents": ceil_div(provider_cost, MICRO_USD_PER_CENT),
    }


def ceil_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator
