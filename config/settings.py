import json
import os


def setting(name: str, default: str | None = None) -> str | None:
    return os.getenv(name, default)


def json_setting(name: str, default: dict[str, int]) -> dict[str, int]:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = json.loads(raw_value)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{name} must be a JSON object") from error
    if not isinstance(value, dict) or any(
        not isinstance(key, str)
        or not key
        or not isinstance(amount, int)
        or amount < 0
        for key, amount in value.items()
    ):
        raise RuntimeError(f"{name} must map names to non-negative integer rates")
    return value


def tenant_api_keys() -> dict[str, str]:
    raw_keys = setting("TENANT_API_KEYS")
    if raw_keys is None:
        raise RuntimeError("TENANT_API_KEYS must be configured")
    try:
        keys = json.loads(raw_keys)
    except json.JSONDecodeError as error:
        raise RuntimeError("TENANT_API_KEYS must contain a JSON object") from error
    if not isinstance(keys, dict) or any(
        not isinstance(tenant_id, str)
        or not tenant_id
        or not isinstance(api_key, str)
        or not api_key
        for tenant_id, api_key in keys.items()
    ):
        raise RuntimeError("TENANT_API_KEYS must map non-empty tenant IDs to API keys")
    return keys


def feature_rates(name: str, defaults: dict[str, int]) -> dict[str, int]:
    return json_setting(name, defaults)


def openai_api_key() -> str | None:
    return setting("OPENAI_API_KEY")


def max_output_tokens() -> int:
    return _positive_setting("MAX_OUTPUT_TOKENS", 800)


def max_monthly_tokens_per_tenant() -> int:
    return _positive_setting("MAX_MONTHLY_TOKENS_PER_TENANT", 1_000_000)


def max_queued_runs_per_tenant() -> int:
    return _positive_setting("MAX_QUEUED_RUNS_PER_TENANT", 10)


def openai_input_cost_micro_usd_per_million() -> int:
    return _positive_setting("OPENAI_INPUT_COST_MICROUSD_PER_MILLION", 2_500_000)


def openai_output_cost_micro_usd_per_million() -> int:
    return _positive_setting("OPENAI_OUTPUT_COST_MICROUSD_PER_MILLION", 10_000_000)


def token_price_tiers() -> list[dict[str, int]]:
    default = [
        {"monthly_tokens": 0, "markup_bps": 3000},
        {"monthly_tokens": 100_000, "markup_bps": 3500},
        {"monthly_tokens": 500_000, "markup_bps": 5000},
    ]
    raw_value = os.getenv("TOKEN_PRICE_TIERS_JSON")
    if raw_value is None:
        return default
    try:
        tiers = json.loads(raw_value)
    except json.JSONDecodeError as error:
        raise RuntimeError("TOKEN_PRICE_TIERS_JSON must be a JSON array") from error
    if not isinstance(tiers, list) or not tiers:
        raise RuntimeError("TOKEN_PRICE_TIERS_JSON must be a non-empty JSON array")
    previous_tokens = -1
    previous_markup = -1
    for tier in tiers:
        if (
            not isinstance(tier, dict)
            or type(tier.get("monthly_tokens")) is not int
            or type(tier.get("markup_bps")) is not int
            or tier["monthly_tokens"] <= previous_tokens
            or tier["markup_bps"] < previous_markup
            or tier["markup_bps"] > 100_000
        ):
            raise RuntimeError(
                "Price tiers must have increasing usage thresholds and non-decreasing markups"
            )
        previous_tokens = tier["monthly_tokens"]
        previous_markup = tier["markup_bps"]
    if tiers[0]["monthly_tokens"] != 0:
        raise RuntimeError("The first token price tier must start at zero monthly tokens")
    return tiers


def validate_token_pricing() -> None:
    openai_input_cost_micro_usd_per_million()
    openai_output_cost_micro_usd_per_million()
    token_price_tiers()


def _positive_setting(name: str, default: int) -> int:
    raw = setting(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"{name} must be a positive integer") from error
    if value < 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return value
