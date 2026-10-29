from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AgentDecision:
    agent: str
    status: str
    evidence: dict[str, Any]
    contract_version: int = 1
