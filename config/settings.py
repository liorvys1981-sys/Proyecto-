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


def price_for_feature(feature: str) -> int:
    return feature_rates(
        "PRICE_CENTS_PER_CPU_SECOND",
        {"goal_analysis": 2, "risk_assessment": 2},
    ).get(feature, 2)


def cost_for_feature(feature: str) -> int:
    return feature_rates(
        "COST_CENTS_PER_CPU_SECOND",
        {"goal_analysis": 1, "risk_assessment": 1},
    ).get(feature, 1)
