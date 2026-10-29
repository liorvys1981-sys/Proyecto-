from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database.models import AgentHeartbeat, AgentRunRecord

AGENT_NAMES = ("gpt4o", "orchestrator", "billing", "message_bus")
PROCESS_HEARTBEAT_NAMES = ("gpt4o", "orchestrator", "billing")
HEARTBEAT_TIMEOUT = timedelta(seconds=120)
STALE_RUN_TIMEOUT = timedelta(seconds=180)


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


def diagnose_runtime(session: Session) -> dict:
    session.execute(select(1))
    stale_before = datetime.now(timezone.utc) - STALE_RUN_TIMEOUT
    counts = dict(
        session.execute(
            select(AgentRunRecord.status, func.count()).group_by(AgentRunRecord.status)
        ).all()
    )
    stale_processing = session.scalar(
        select(func.count(AgentRunRecord.id)).where(
            AgentRunRecord.status == "processing",
            AgentRunRecord.processing_started_at < stale_before,
        )
    )
    return {
        "database": "healthy",
        "queued_runs": counts.get("queued", 0),
        "processing_runs": counts.get("processing", 0),
        "failed_runs": counts.get("failed", 0),
        "stale_processing_runs": stale_processing or 0,
        "automatic_recovery_enabled": False,
    }
