import secrets
from datetime import datetime, timezone
from typing import Annotated
from uuid import uuid4

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from agents.monetization.token_pricing import MICRO_USD_PER_CENT, ceil_div
from agents.orchestrator.service import create_queued_run
from config.settings import (
    openai_api_key,
    max_monthly_tokens_per_tenant,
    max_output_tokens,
    max_queued_runs_per_tenant,
    setting,
    tenant_api_keys,
    token_price_tiers,
    validate_token_pricing,
)
from database.models import (
    AgentRunRecord,
    BillingInvoice,
    ProcessedWebhook,
    UsageRecord,
)
from database.session import get_session
from monitoring.health import agent_health, heartbeat

router = APIRouter()
bearer_scheme = HTTPBearer(auto_error=False)
SessionDependency = Annotated[Session, Depends(get_session)]


class AgentRunRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=2000)
    requires_approval: bool = False


class CheckoutRequest(BaseModel):
    consent: bool
    approved_amount_cents: int = Field(ge=0)


async def resolve_tenant(
    tenant_id: Annotated[str, Header(alias="X-Tenant-ID")],
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ],
) -> str:
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        api_key = tenant_api_keys().get(tenant_id)
    except RuntimeError as error:
        raise HTTPException(
            status_code=503, detail="Tenant authentication is not configured"
        ) from error
    if api_key is None or not secrets.compare_digest(
        api_key.encode("utf-8"), credentials.credentials.encode("utf-8")
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid tenant credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return tenant_id


TenantDependency = Annotated[str, Depends(resolve_tenant)]


def require_admin(admin_key: Annotated[str | None, Header(alias="X-Admin-Key")] = None):
    configured_key = setting("ADMIN_API_KEY")
    if not configured_key:
        raise HTTPException(status_code=503, detail="Admin authentication is not configured")
    if admin_key is None or not secrets.compare_digest(
        configured_key.encode("utf-8"), admin_key.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="Invalid administrator credentials")


def require_metrics(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ],
) -> None:
    configured_key = setting("ADMIN_API_KEY")
    if not configured_key:
        raise HTTPException(status_code=503, detail="Metrics authentication is not configured")
    if credentials is None or not secrets.compare_digest(
        configured_key.encode("utf-8"), credentials.credentials.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="Invalid metrics credentials")


AdminDependency = Annotated[None, Depends(require_admin)]


def _serialize_run(run: AgentRunRecord) -> dict:
    return {
        "id": run.id,
        "status": run.status,
        "goal": run.goal,
        "created_at": run.created_at,
        "steps": run.steps,
        "result": run.result,
        "usage": [
            {
                "model": usage.agent,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "total_tokens": usage.input_tokens + usage.output_tokens,
                "provider_cost_micro_usd": usage.provider_cost_micro_usd,
                "input_provider_cost_micro_usd": (
                    usage.input_provider_cost_micro_usd
                ),
                "output_provider_cost_micro_usd": (
                    usage.output_provider_cost_micro_usd
                ),
                "sell_price_micro_usd": usage.sell_price_micro_usd,
                "input_sell_price_micro_usd": usage.input_sell_price_micro_usd,
                "output_sell_price_micro_usd": usage.output_sell_price_micro_usd,
                "markup_bps": usage.markup_bps,
            }
            for usage in run.usage
        ],
    }


def _month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _usage_summary(session: Session, tenant_id: str) -> dict:
    now = datetime.now(timezone.utc)
    entries = session.scalars(
        select(UsageRecord).where(
            UsageRecord.tenant_id == tenant_id,
            UsageRecord.invoice_id.is_(None),
        )
    ).all()
    provider_cost_micro_usd = sum(
        item.provider_cost_micro_usd for item in entries
    )
    sell_price_micro_usd = sum(item.sell_price_micro_usd for item in entries)
    return {
        "period_start": min(
            (item.created_at for item in entries), default=_month_start(now)
        ),
        "currency": "usd",
        "billable_cents": ceil_div(sell_price_micro_usd, MICRO_USD_PER_CENT),
        "estimated_operating_cost_cents": ceil_div(
            provider_cost_micro_usd, MICRO_USD_PER_CENT
        ),
        "estimated_margin_cents": ceil_div(
            sell_price_micro_usd, MICRO_USD_PER_CENT
        )
        - ceil_div(provider_cost_micro_usd, MICRO_USD_PER_CENT),
        "input_tokens": sum(item.input_tokens for item in entries),
        "output_tokens": sum(item.output_tokens for item in entries),
        "total_tokens": sum(
            item.input_tokens + item.output_tokens for item in entries
        ),
        "provider_cost_micro_usd": provider_cost_micro_usd,
        "sell_price_micro_usd": sell_price_micro_usd,
        "price_tiers": token_price_tiers(),
        "events": [
            {
                "agent": item.agent,
                "feature": item.feature,
                "input_tokens": item.input_tokens,
                "output_tokens": item.output_tokens,
                "provider_cost_micro_usd": item.provider_cost_micro_usd,
                "input_provider_cost_micro_usd": (
                    item.input_provider_cost_micro_usd
                ),
                "output_provider_cost_micro_usd": (
                    item.output_provider_cost_micro_usd
                ),
                "sell_price_micro_usd": item.sell_price_micro_usd,
                "input_sell_price_micro_usd": item.input_sell_price_micro_usd,
                "output_sell_price_micro_usd": item.output_sell_price_micro_usd,
                "markup_bps": item.markup_bps,
            }
            for item in entries
        ],
    }


@router.get("/health")
def health(session: SessionDependency) -> dict[str, str]:
    try:
        session.execute(text("SELECT 1"))
    except Exception as error:
        raise HTTPException(status_code=503, detail="Database unavailable") from error
    return {"status": "ok"}


@router.post("/v1/agent-runs", status_code=status.HTTP_202_ACCEPTED)
def create_agent_run(
    request: AgentRunRequest,
    session: SessionDependency,
    tenant_id: TenantDependency,
) -> dict:
    if not openai_api_key():
        raise HTTPException(
            status_code=503, detail="GPT-4o worker is not configured"
        )
    try:
        validate_token_pricing()
    except RuntimeError as error:
        raise HTTPException(
            status_code=503, detail="Token pricing configuration is invalid"
        ) from error
    active_jobs = session.scalar(
        select(func.count(AgentRunRecord.id)).where(
            AgentRunRecord.tenant_id == tenant_id,
            AgentRunRecord.status.in_(("queued", "processing")),
        )
    )
    if active_jobs >= max_queued_runs_per_tenant():
        raise HTTPException(
            status_code=429, detail="Tenant agent queue is at capacity"
        )
    month_start = _month_start(datetime.now(timezone.utc))
    monthly_tokens = session.scalar(
        select(
            func.coalesce(
                func.sum(UsageRecord.input_tokens + UsageRecord.output_tokens), 0
            )
        ).where(
            UsageRecord.tenant_id == tenant_id,
            UsageRecord.created_at >= month_start,
        )
    ) or 0
    reserved_tokens = session.scalar(
        select(
            func.coalesce(
                func.sum(func.length(AgentRunRecord.goal) + max_output_tokens()), 0
            )
        ).where(
            AgentRunRecord.tenant_id == tenant_id,
            AgentRunRecord.status.in_(("queued", "processing")),
            AgentRunRecord.created_at >= month_start,
        )
    ) or 0
    if (
        monthly_tokens
        + reserved_tokens
        + len(request.goal)
        + max_output_tokens()
        > max_monthly_tokens_per_tenant()
    ):
        raise HTTPException(
            status_code=429,
            detail="Tenant monthly GPT-4o token budget is exhausted",
        )
    run = create_queued_run(
        session, tenant_id, request.goal, request.requires_approval
    )
    return _serialize_run(run)


@router.get("/v1/agent-runs/{run_id}")
def get_agent_run(
    run_id: str, session: SessionDependency, tenant_id: TenantDependency
) -> dict:
    run = session.scalar(
        select(AgentRunRecord).where(
            AgentRunRecord.id == run_id, AgentRunRecord.tenant_id == tenant_id
        )
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return _serialize_run(run)


@router.post("/v1/agent-runs/{run_id}/approve")
def approve_agent_run(
    run_id: str,
    session: SessionDependency,
    _: AdminDependency,
) -> dict:
    run = session.get(AgentRunRecord, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status != "pending_approval":
        raise HTTPException(status_code=409, detail="Run is not awaiting approval")
    steps = [dict(step) for step in run.steps]
    for step in steps:
        if step["agent"] == "execution":
            step["status"] = "approved_simulation"
            step["detail"] = "Administrator approved a simulation; no external action was taken."
    run.steps = steps
    run.status = "completed"
    run.result = "Approval recorded. No external systems were contacted."
    session.commit()
    return _serialize_run(run)


@router.get("/v1/billing/preview")
def billing_preview(session: SessionDependency, tenant_id: TenantDependency) -> dict:
    return _usage_summary(session, tenant_id)


@router.post("/v1/billing/checkout")
def create_checkout(
    request: CheckoutRequest,
    session: SessionDependency,
    tenant_id: TenantDependency,
) -> dict:
    if not request.consent:
        raise HTTPException(status_code=400, detail="Explicit payment consent is required")
    secret_key = setting("STRIPE_SECRET_KEY")
    if not secret_key:
        raise HTTPException(status_code=503, detail="Payment provider is not configured")
    if not setting("STRIPE_SUCCESS_URL") or not setting("STRIPE_CANCEL_URL"):
        raise HTTPException(status_code=503, detail="Checkout URLs are not configured")

    previous_invoice = session.scalar(
        select(BillingInvoice)
        .where(
            BillingInvoice.tenant_id == tenant_id,
            BillingInvoice.status.in_(
                ("checkout_pending", "checkout_failed", "checkout_open")
            ),
        )
        .order_by(BillingInvoice.created_at.desc())
    )
    if previous_invoice and previous_invoice.status == "checkout_open":
        if request.approved_amount_cents != previous_invoice.amount_cents:
            raise HTTPException(
                status_code=409, detail="Preview changed; confirm the displayed amount"
            )
        return {
            "invoice_id": previous_invoice.id,
            "amount_cents": previous_invoice.amount_cents,
            "currency": previous_invoice.currency,
            "status": previous_invoice.status,
            "checkout_url": previous_invoice.checkout_url,
        }

    if previous_invoice:
        invoice = previous_invoice
        amount = invoice.amount_cents
        if request.approved_amount_cents != amount:
            raise HTTPException(
                status_code=409, detail="Preview changed; confirm the displayed amount"
            )
        invoice.consented_at = datetime.now(timezone.utc)
        session.commit()
    else:
        usage = session.scalars(
            select(UsageRecord)
            .where(
                UsageRecord.tenant_id == tenant_id,
                UsageRecord.invoice_id.is_(None),
            )
            .with_for_update(skip_locked=True)
        ).all()
        amount = ceil_div(
            sum(entry.sell_price_micro_usd for entry in usage),
            MICRO_USD_PER_CENT,
        )
        if amount <= 0:
            raise HTTPException(status_code=409, detail="There is no billable usage")
        if amount < 50:
            raise HTTPException(
                status_code=409,
                detail="Balance is below Stripe's minimum; usage will carry forward",
            )
        if request.approved_amount_cents != amount:
            raise HTTPException(
                status_code=409, detail="Preview changed; confirm the displayed amount"
            )
        consented_at = datetime.now(timezone.utc)
        invoice = BillingInvoice(
            id=str(uuid4()),
            tenant_id=tenant_id,
            amount_cents=amount,
            status="checkout_pending",
            currency="usd",
            consented_at=consented_at,
            created_at=consented_at,
        )
        session.add(invoice)
        session.flush()
        for entry in usage:
            entry.invoice_id = invoice.id
        session.commit()

    session.refresh(invoice)

    try:
        checkout = stripe.checkout.Session.create(
            api_key=secret_key,
            idempotency_key=invoice.id,
            mode="payment",
            payment_method_types=["card"],
            line_items=[
                {
                    "price_data": {
                        "currency": invoice.currency,
                        "product_data": {"name": "YIMI agent usage"},
                        "unit_amount": amount,
                    },
                    "quantity": 1,
                }
            ],
            success_url=setting("STRIPE_SUCCESS_URL"),
            cancel_url=setting("STRIPE_CANCEL_URL"),
            metadata={"invoice_id": invoice.id, "tenant_id": tenant_id},
        )
    except stripe.StripeError as error:
        invoice.status = "checkout_failed"
        session.commit()
        raise HTTPException(status_code=502, detail="Payment checkout could not be created") from error

    invoice.stripe_session_id = checkout.id
    invoice.checkout_url = checkout.url
    invoice.status = "checkout_open"
    session.commit()
    return {
        "invoice_id": invoice.id,
        "amount_cents": invoice.amount_cents,
        "currency": invoice.currency,
        "status": invoice.status,
        "checkout_url": invoice.checkout_url,
    }


@router.post("/v1/billing/webhook")
async def stripe_webhook(request: Request, session: SessionDependency) -> dict[str, str]:
    webhook_secret = setting("STRIPE_WEBHOOK_SECRET")
    signature = request.headers.get("stripe-signature")
    if not webhook_secret or not signature:
        raise HTTPException(status_code=400, detail="Webhook signature is required")
    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(payload, signature, webhook_secret)
    except (ValueError, stripe.SignatureVerificationError) as error:
        raise HTTPException(status_code=400, detail="Invalid webhook signature") from error

    event_id = event.get("id")
    if not event_id:
        raise HTTPException(status_code=400, detail="Webhook event ID is missing")
    if session.get(ProcessedWebhook, event_id):
        return {"status": "already_processed"}

    if event.get("type") == "checkout.session.completed":
        checkout = event["data"]["object"]
        invoice_id = checkout.get("metadata", {}).get("invoice_id")
        invoice = session.get(BillingInvoice, invoice_id) if invoice_id else None
        if (
            invoice
            and invoice.tenant_id == checkout.get("metadata", {}).get("tenant_id")
            and invoice.stripe_session_id == checkout.get("id")
            and invoice.amount_cents == checkout.get("amount_total")
            and invoice.currency == checkout.get("currency")
            and checkout.get("payment_status") == "paid"
        ):
            invoice.status = "paid"
    elif event.get("type") == "checkout.session.expired":
        checkout = event["data"]["object"]
        invoice_id = checkout.get("metadata", {}).get("invoice_id")
        invoice = session.get(BillingInvoice, invoice_id) if invoice_id else None
        if invoice and invoice.stripe_session_id == checkout.get("id"):
            invoice.status = "checkout_expired"
            for entry in session.scalars(
                select(UsageRecord).where(UsageRecord.invoice_id == invoice.id)
            ):
                entry.invoice_id = None
    session.add(
        ProcessedWebhook(event_id=event_id, processed_at=datetime.now(timezone.utc))
    )
    session.commit()
    return {"status": "processed"}


@router.get("/v1/admin/health", dependencies=[Depends(require_admin)])
def admin_health(session: SessionDependency) -> dict:
    agents = agent_health(session)
    return {
        "status": "ok" if all(item["status"] == "healthy" for item in agents) else "degraded",
        "agents": agents,
    }


@router.get("/v1/admin/metrics", dependencies=[Depends(require_admin)])
def admin_metrics(session: SessionDependency) -> dict:
    usage = session.execute(
        select(
            func.count(UsageRecord.id),
            func.coalesce(func.sum(UsageRecord.provider_cost_micro_usd), 0),
            func.coalesce(func.sum(UsageRecord.sell_price_micro_usd), 0),
            func.coalesce(func.sum(UsageRecord.input_tokens), 0),
            func.coalesce(func.sum(UsageRecord.output_tokens), 0),
        )
    ).one()
    return {
        "usage_events": usage[0],
        "estimated_operating_cost_cents": ceil_div(
            usage[1], MICRO_USD_PER_CENT
        ),
        "estimated_billable_cents": ceil_div(usage[2], MICRO_USD_PER_CENT),
        "estimated_margin_cents": ceil_div(usage[2], MICRO_USD_PER_CENT)
        - ceil_div(usage[1], MICRO_USD_PER_CENT),
        "input_tokens": usage[3],
        "output_tokens": usage[4],
        "total_tokens": usage[3] + usage[4],
    }


@router.get(
    "/metrics",
    response_class=PlainTextResponse,
    dependencies=[Depends(require_metrics)],
)
def prometheus_metrics(session: SessionDependency) -> str:
    metrics = admin_metrics(session)
    return "\n".join(
        [
            "# HELP yimi_usage_events_total Recorded agent usage events.",
            "# TYPE yimi_usage_events_total counter",
            f"yimi_usage_events_total {metrics['usage_events']}",
            "# HELP yimi_gpt4o_input_tokens_total GPT-4o input tokens.",
            "# TYPE yimi_gpt4o_input_tokens_total counter",
            f"yimi_gpt4o_input_tokens_total {metrics['input_tokens']}",
            "# HELP yimi_gpt4o_output_tokens_total GPT-4o output tokens.",
            "# TYPE yimi_gpt4o_output_tokens_total counter",
            f"yimi_gpt4o_output_tokens_total {metrics['output_tokens']}",
            "# HELP yimi_operating_cost_cents_estimated Estimated operating cost.",
            "# TYPE yimi_operating_cost_cents_estimated gauge",
            f"yimi_operating_cost_cents_estimated {metrics['estimated_operating_cost_cents']}",
            "# HELP yimi_usage_revenue_cents_estimated Usage revenue before payment.",
            "# TYPE yimi_usage_revenue_cents_estimated gauge",
            f"yimi_usage_revenue_cents_estimated {metrics['estimated_billable_cents']}",
            "",
        ]
    )


@router.get("/v1/admin/agents", dependencies=[Depends(require_admin)])
def admin_agents(session: SessionDependency) -> dict:
    return {"agents": agent_health(session)}
