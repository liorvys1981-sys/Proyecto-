from config.settings import cost_for_feature, price_for_feature


def price_usage(feature: str, cpu_milliseconds: int) -> tuple[int, int]:
    billable_cents = (cpu_milliseconds * price_for_feature(feature) + 999) // 1000
    operating_cost_cents = (cpu_milliseconds * cost_for_feature(feature) + 999) // 1000
    return billable_cents, operating_cost_cents
