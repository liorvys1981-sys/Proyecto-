from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models import AgentHeartbeat

AGENT_NAMES = ("analytics", "security", "orchestrator", "billing")
HEARTBEAT_TIMEOUT = timedelta(seconds=120)


def heartbeat(session: Session, name: str, state: str = "healthy", error: str | None = None):
    agent = session.get(AgentHeartbeat, name)
    if agent is None:
        agent = AgentHeartbeat(
            name=name, status=state, last_seen_at=datetime.now(timezone.utc)
        )
        session.add(agent)
    else:
        agent.last_seen_at = datetime.now(timezone.utc)
        if state != "alive" or agent.status not in ("unhealthy", "recovering"):
            agent.status = state
            agent.last_error = error


def agent_health(session: Session) -> list[dict[str, str]]:
    now = datetime.now(timezone.utc)
    agents = {
        row.name: row
        for row in session.scalars(
            select(AgentHeartbeat).where(AgentHeartbeat.name.in_(AGENT_NAMES))
        )
    }
    results = []
    for name in AGENT_NAMES:
        row = agents.get(name)
        seen = row.last_seen_at if row else None
        if seen and seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        stale = seen is None or now - seen > HEARTBEAT_TIMEOUT
        results.append(
            {
                "agent": name,
                "status": (
                    "stale"
                    if stale
                    else "healthy"
                    if row.status == "alive"
                    else row.status
                ),
                "last_seen_at": seen.isoformat() if seen else "",
                "alert": "heartbeat_missing_or_stale" if stale else "",
                "last_error": row.last_error if row and row.last_error else "",
            }
        )
    return results
