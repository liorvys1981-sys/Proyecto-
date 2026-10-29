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
from database.models import (
    AgentDecisionRecord,
    AgentRunRecord,
    AgentSubtaskRecord,
    UsageRecord,
)
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


def _update_subtask(
    session: Session,
    run_id: str,
    task_key: str,
    status: str,
    result: dict[str, Any] | None = None,
    commit: bool = True,
) -> None:
    subtask = session.scalar(
        select(AgentSubtaskRecord).where(
            AgentSubtaskRecord.run_id == run_id,
            AgentSubtaskRecord.task_key == task_key,
        )
    )
    if subtask is None:
        return
    if status == "running" and subtask.depends_on:
        completed_dependencies = set(
            session.scalars(
                select(AgentSubtaskRecord.task_key).where(
                    AgentSubtaskRecord.run_id == run_id,
                    AgentSubtaskRecord.task_key.in_(subtask.depends_on),
                    AgentSubtaskRecord.status == "completed",
                )
            ).all()
        )
        if completed_dependencies != set(subtask.depends_on):
            raise RuntimeError("Subtask dependencies are not complete")
    now = datetime.now(timezone.utc)
    if status == "running":
        subtask.attempts += 1
        subtask.started_at = now
    if status in ("completed", "failed"):
        subtask.finished_at = now
    subtask.status = status
    if result is not None:
        subtask.result = result
    if commit:
        session.commit()


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
    active_subtask: str | None = None
    try:
        plan = plan_run(run.goal, run.approval_required)
        decisions.append(plan)
        for step in plan.evidence["steps"]:
            session.add(
                AgentSubtaskRecord(
                    id=str(uuid4()),
                    tenant_id=run.tenant_id,
                    run_id=run.id,
                    task_key=step["key"],
                    agent=step["agent"],
                    depends_on=step["depends_on"],
                    status="planned",
                    attempts=0,
                    result={},
                    created_at=datetime.now(timezone.utc),
                )
            )
        session.commit()

        active_subtask = "security_policy"
        _update_subtask(session, run.id, active_subtask, "running")
        security = assess_request(run.goal, run.approval_required)
        decisions.append(security)
        _update_subtask(
            session,
            run.id,
            active_subtask,
            "completed",
            security.evidence,
        )

        active_subtask = "runtime_diagnostics"
        _update_subtask(session, run.id, active_subtask, "running")
        diagnostics = diagnose_runtime(session)
        devops = AgentDecision(
            agent="devops",
            status="warning" if diagnostics["stale_processing_runs"] else "diagnosed",
            evidence=diagnostics,
        )
        decisions.append(devops)
        _update_subtask(
            session, run.id, active_subtask, "completed", diagnostics
        )

        active_subtask = "gpt4o_completion"
        _update_subtask(session, run.id, active_subtask, "running")
        (answer, input_tokens, output_tokens), cpu_milliseconds, memory_bytes = (
            await measure_async(generate, client, run.goal)
        )
        _update_subtask(
            session,
            run.id,
            active_subtask,
            "completed",
            {"input_tokens": input_tokens, "output_tokens": output_tokens},
        )

        active_subtask = "resources_measurement"
        _update_subtask(session, run.id, active_subtask, "running")
        resources = resource_decision(cpu_milliseconds, memory_bytes)
        decisions.append(resources)
        _update_subtask(
            session,
            run.id,
            active_subtask,
            "completed",
            resources.evidence,
        )

        active_subtask = "token_pricing"
        _update_subtask(session, run.id, active_subtask, "running")
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
        decisions.append(monetization)
        _update_subtask(
            session,
            run.id,
            active_subtask,
            "completed",
            monetization.evidence,
        )

        active_subtask = "result_analytics"
        _update_subtask(session, run.id, active_subtask, "running")
        analytics = analyze_completion(answer, input_tokens, output_tokens)
        decisions.append(analytics)
        _update_subtask(
            session, run.id, active_subtask, "completed", analytics.evidence
        )

        active_subtask = "persist_audit_and_usage"
        _update_subtask(session, run.id, active_subtask, "running")
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
        _update_subtask(
            session,
            run.id,
            active_subtask,
            "completed",
            {"decision_count": len(decisions), "usage_recorded": True},
            commit=False,
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
        session.rollback()
        run = session.get(AgentRunRecord, run.id)
        if active_subtask:
            _update_subtask(
                session,
                run.id,
                active_subtask,
                "failed",
                {"error_type": type(error).__name__},
                commit=False,
            )
        for pending_subtask in session.scalars(
            select(AgentSubtaskRecord).where(
                AgentSubtaskRecord.run_id == run.id,
                AgentSubtaskRecord.status == "planned",
            )
        ):
            pending_subtask.status = "failed"
            pending_subtask.result = {
                "error": "prerequisite_failed",
                "failed_task": active_subtask,
            }
            pending_subtask.finished_at = datetime.now(timezone.utc)
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
            for subtask in session.scalars(
                select(AgentSubtaskRecord).where(
                    AgentSubtaskRecord.run_id == run.id,
                    AgentSubtaskRecord.status.in_(("planned", "running")),
                )
            ):
                subtask.status = "failed"
                subtask.result = {"error": "worker_lease_expired"}
                subtask.finished_at = datetime.now(timezone.utc)
            expired += 1
    if expired:
        session.commit()
    return expired
