from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import uuid4

from sqlalchemy import case, select, text, update
from sqlalchemy.orm import Session

from database.models import (
    AgentCoordinationRecord,
    AgentDecisionRecord,
    AgentMessageRecord,
)

EVENT_TYPE_DECISION = "agent.decision"
EVENT_SCHEMA_VERSION = 1
COORDINATING_AGENTS = {
    "orchestrator",
    "security",
    "devops",
    "resources",
    "monetization",
    "analytics",
}
MAX_DELIVERY_ATTEMPTS = 3
BASE_RETRY_SECONDS = 1


def publish_decision(
    session: Session, decision: AgentDecisionRecord
) -> AgentMessageRecord:
    dedupe_key = str(decision.id)
    existing = session.scalar(
        select(AgentMessageRecord).where(
            AgentMessageRecord.target == "orchestrator",
            AgentMessageRecord.dedupe_key == dedupe_key,
        )
    )
    if existing is not None:
        return existing
    message = AgentMessageRecord(
        id=str(uuid4()),
        event_type=EVENT_TYPE_DECISION,
        schema_version=EVENT_SCHEMA_VERSION,
        tenant_id=decision.tenant_id,
        correlation_id=decision.run_id,
        run_id=decision.run_id,
        sender=decision.agent,
        target="orchestrator",
        dedupe_key=dedupe_key,
        payload={"decision_id": decision.id},
        status="queued",
        attempts=0,
        max_attempts=MAX_DELIVERY_ATTEMPTS,
        available_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    session.add(message)
    return message


def claim_next_message(session: Session) -> AgentMessageRecord | None:
    now = datetime.now(timezone.utc)
    lease_expired_before = now - timedelta(seconds=60)
    session.execute(
        update(AgentMessageRecord)
        .where(
            AgentMessageRecord.status == "processing",
            AgentMessageRecord.locked_at < lease_expired_before,
        )
        .execution_options(synchronize_session=False)
        .values(
            status=case(
                (
                    AgentMessageRecord.attempts >= AgentMessageRecord.max_attempts,
                    "dead_letter",
                ),
                else_="queued",
            ),
            available_at=now,
            locked_at=None,
            last_error="DeliveryLeaseExpired",
        )
    )
    session.commit()
    candidate_id = session.scalar(
        select(AgentMessageRecord.id)
        .where(
            AgentMessageRecord.status == "queued",
            AgentMessageRecord.available_at <= now,
        )
        .order_by(AgentMessageRecord.available_at, AgentMessageRecord.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if candidate_id is None:
        session.rollback()
        return None
    result = session.execute(
        update(AgentMessageRecord)
        .where(
            AgentMessageRecord.id == candidate_id,
            AgentMessageRecord.status == "queued",
            AgentMessageRecord.available_at <= now,
        )
        .execution_options(synchronize_session=False)
        .values(
            status="processing",
            attempts=AgentMessageRecord.attempts + 1,
            locked_at=now,
        )
    )
    if result.rowcount != 1:
        session.rollback()
        return None
    session.commit()
    message = session.get(AgentMessageRecord, candidate_id)
    if message is not None:
        session.refresh(message)
    return message


def _consume_decision(session: Session, message: AgentMessageRecord) -> None:
    if (
        message.event_type != EVENT_TYPE_DECISION
        or message.schema_version != EVENT_SCHEMA_VERSION
        or message.target != "orchestrator"
        or message.correlation_id != message.run_id
    ):
        raise ValueError("Unsupported agent event contract")
    decision_id = message.payload.get("decision_id")
    decision = session.get(AgentDecisionRecord, decision_id)
    if (
        decision is None
        or decision.run_id != message.run_id
        or decision.tenant_id != message.tenant_id
        or decision.agent != message.sender
    ):
        raise ValueError("Agent event does not match a persisted decision")

    message.status = "delivered"
    message.locked_at = None
    message.acknowledged_at = datetime.now(timezone.utc)
    message.last_error = None
    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text(
                "SELECT pg_advisory_xact_lock(hashtextextended(:run_id, 1))"
            ),
            {"run_id": message.run_id},
        )
    delivered = session.scalars(
        select(AgentMessageRecord).where(
            AgentMessageRecord.run_id == message.run_id,
            AgentMessageRecord.target == "orchestrator",
            AgentMessageRecord.event_type == EVENT_TYPE_DECISION,
            AgentMessageRecord.status == "delivered",
        )
    ).all()
    agents = sorted({item.sender for item in delivered})
    if COORDINATING_AGENTS.issubset(agents):
        run = session.get(AgentCoordinationRecord, message.run_id)
        if run is None:
            session.add(
                AgentCoordinationRecord(
                    run_id=message.run_id,
                    tenant_id=message.tenant_id,
                    status="verified",
                    participating_agents=agents,
                    verified_at=datetime.now(timezone.utc),
                )
            )


def process_next_message(
    session: Session,
    handler: Callable[[Session, AgentMessageRecord], None] = _consume_decision,
) -> bool:
    message = claim_next_message(session)
    if message is None:
        return False
    try:
        handler(session, message)
        session.commit()
    except Exception as error:
        session.rollback()
        message = session.get(AgentMessageRecord, message.id)
        if message is None:
            return True
        message.last_error = type(error).__name__
        message.locked_at = None
        if message.attempts >= message.max_attempts:
            message.status = "dead_letter"
        else:
            message.status = "queued"
            delay = min(
                BASE_RETRY_SECONDS * (2 ** max(0, message.attempts - 1)), 60
            )
            message.available_at = datetime.now(timezone.utc) + timedelta(
                seconds=delay
            )
        session.commit()
    return True
