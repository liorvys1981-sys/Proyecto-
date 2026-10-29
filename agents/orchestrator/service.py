from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from agents.analytics.service import analyze_completion
from agents.bus import publish_decision
from agents.contracts import AgentDecision
from agents.monetization.service import assess_run_cost
from agents.orchestrator.planning import plan_run
from agents.resources.service import measure_async, resource_decision
from agents.security.service import assess_request
from core.llm.gpt4o import generate
from database.models import AgentDecisionRecord, AgentRunRecord, UsageRecord
from monitoring.health import diagnose_runtime, heartbeat


def _record_decisions(
    session: Session, run: AgentRunRecord, decisions: list[AgentDecision]
) -> None:
    for decision in decisions:
        record = AgentDecisionRecord(
            id=str(uuid4()),
            tenant_id=run.tenant_id,
            run_id=run.id,
            agent=decision.agent,
            contract_version=decision.contract_version,
            status=decision.status,
            evidence=decision.evidence,
            created_at=datetime.now(timezone.utc),
        )
        session.add(record)
        publish_decision(session, record)


def create_queued_run(
    session: Session,
    tenant_id: str,
    goal: str,
    approval_required: bool = False,
    idempotency_key: str | None = None,
    request_hash: str | None = None,
) -> AgentRunRecord:
    run = AgentRunRecord(
        id=str(uuid4()),
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        goal=goal,
        status="queued",
        result="Waiting for the GPT-4o worker.",
        steps=[],
        approval_required=approval_required,
        created_at=datetime.now(timezone.utc),
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def claim_next_run(session: Session) -> AgentRunRecord | None:
    candidate_id = session.scalar(
        select(AgentRunRecord.id)
        .where(AgentRunRecord.status == "queued")
        .order_by(AgentRunRecord.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if candidate_id is None:
        session.rollback()
        return None
    result = session.execute(
        update(AgentRunRecord)
        .where(
            AgentRunRecord.id == candidate_id,
            AgentRunRecord.status == "queued",
        )
        .values(
            status="processing",
            result="GPT-4o is processing the request.",
            processing_started_at=datetime.now(timezone.utc),
        )
    )
    if result.rowcount != 1:
        session.rollback()
        return None
    session.commit()
    return session.get(AgentRunRecord, candidate_id)


async def run_workflow(
    session: Session, run: AgentRunRecord, client: Any
) -> AgentRunRecord:
    decisions: list[AgentDecision] = []
    try:
        plan = plan_run(run.goal, run.approval_required)
        security = assess_request(run.goal, run.approval_required)
        diagnostics = diagnose_runtime(session)
        decisions.extend(
            [
                plan,
                security,
                AgentDecision(
                    agent="devops",
                    status=(
                        "warning"
                        if diagnostics["stale_processing_runs"]
                        else "diagnosed"
                    ),
                    evidence=diagnostics,
                ),
            ]
        )
        (answer, input_tokens, output_tokens), cpu_milliseconds, memory_bytes = (
            await measure_async(generate, client, run.goal)
        )
        month_start = datetime.now(timezone.utc).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        monthly_tokens = session.scalar(
            select(
                func.coalesce(
                    func.sum(UsageRecord.input_tokens + UsageRecord.output_tokens), 0
                )
            ).where(
                UsageRecord.tenant_id == run.tenant_id,
                UsageRecord.created_at >= month_start,
            )
        )
        monetization, quote = assess_run_cost(
            input_tokens, output_tokens, monthly_tokens or 0
        )
        resources = resource_decision(cpu_milliseconds, memory_bytes)
        analytics = analyze_completion(answer, input_tokens, output_tokens)
        decisions.extend([resources, monetization, analytics])
        now = datetime.now(timezone.utc)
        approval_required = (
            run.approval_required or security.status == "review_required"
        )
        run.approval_required = approval_required
        run.status = "pending_approval" if approval_required else "completed"
        run.result = answer
        run.finished_at = datetime.now(timezone.utc)
        run.steps = [
            {
                "agent": decision.agent,
                "status": decision.status,
                "detail": decision.evidence,
            }
            for decision in decisions
        ]
        if approval_required:
            run.steps.append(
                {
                    "status": "pending_approval",
                    "agent": "human_approval",
                    "detail": "High-impact requests require review; no external tools are enabled.",
                }
            )
        _record_decisions(session, run, decisions)
        session.add(
            UsageRecord(
                id=str(uuid4()),
                tenant_id=run.tenant_id,
                run_id=run.id,
                agent="gpt-4o",
                feature="text_completion",
                cpu_milliseconds=cpu_milliseconds,
                memory_bytes=memory_bytes,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                provider_cost_micro_usd=quote["provider_cost_micro_usd"],
                input_provider_cost_micro_usd=quote[
                    "input_provider_cost_micro_usd"
                ],
                output_provider_cost_micro_usd=quote[
                    "output_provider_cost_micro_usd"
                ],
                sell_price_micro_usd=quote["sell_price_micro_usd"],
                input_sell_price_micro_usd=quote["input_sell_price_micro_usd"],
                output_sell_price_micro_usd=quote["output_sell_price_micro_usd"],
                markup_bps=quote["markup_bps"],
                billable_cents=quote["billable_cents"],
                operating_cost_cents=quote["operating_cost_cents"],
                created_at=now,
            )
        )
        heartbeat(session, "gpt4o", "healthy")
        heartbeat(session, "orchestrator", "healthy")
    except Exception as error:
        run.status = "failed"
        run.finished_at = datetime.now(timezone.utc)
        run.result = "The GPT-4o request failed; no automatic retry was made."
        run.steps = [
            {
                "agent": "gpt-4o",
                "status": "failed",
                "detail": f"Provider error: {type(error).__name__}.",
            }
        ]
        decisions.append(
            AgentDecision(
                agent="orchestrator",
                status="failed",
                evidence={"error_type": type(error).__name__},
            )
        )
        _record_decisions(session, run, decisions)
        heartbeat(session, "gpt4o", "unhealthy", type(error).__name__)
    session.commit()
    session.refresh(run)
    return run


async def process_next_run(session: Session, client: Any) -> bool:
    run = claim_next_run(session)
    if run is None:
        return False
    await run_workflow(session, run, client)
    return True


def expire_stuck_runs(session: Session, older_than_seconds: int = 180) -> int:
    deadline = datetime.now(timezone.utc).timestamp() - older_than_seconds
    stale_runs = session.scalars(
        select(AgentRunRecord).where(
            AgentRunRecord.status == "processing",
            AgentRunRecord.processing_started_at.is_not(None),
        )
    ).all()
    expired = 0
    for run in stale_runs:
        started_at = run.processing_started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        if started_at.timestamp() <= deadline:
            run.status = "failed"
            run.finished_at = datetime.now(timezone.utc)
            run.result = (
                "Worker stopped during processing; automatic retry disabled to avoid "
                "duplicate provider charges."
            )
            expired += 1
    if expired:
        session.commit()
    return expired
