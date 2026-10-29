from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class AgentRunRecord(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        Index("ix_agent_runs_tenant_created", "tenant_id", "created_at"),
        Index(
            "uq_agent_runs_tenant_idempotency_key",
            "tenant_id",
            "idempotency_key",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(200), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[str] = mapped_column(Text, nullable=False)
    steps: Mapped[list[dict[str, str]]] = mapped_column(JSON, nullable=False)
    approval_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    processing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    usage: Mapped[list["UsageRecord"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class UsageRecord(Base):
    __tablename__ = "usage_records"
    __table_args__ = (Index("ix_usage_records_tenant_created", "tenant_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(200), nullable=False)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), nullable=False)
    agent: Mapped[str] = mapped_column(String(80), nullable=False)
    feature: Mapped[str] = mapped_column(String(80), nullable=False)
    cpu_milliseconds: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    billable_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    operating_cost_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provider_cost_micro_usd: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    input_provider_cost_micro_usd: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    output_provider_cost_micro_usd: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    input_sell_price_micro_usd: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    output_sell_price_micro_usd: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    sell_price_micro_usd: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    markup_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    invoice_id: Mapped[str | None] = mapped_column(
        ForeignKey("billing_invoices.id"), nullable=True
    )
    run: Mapped[AgentRunRecord] = relationship(back_populates="usage")


class BillingInvoice(Base):
    __tablename__ = "billing_invoices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="usd")
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    stripe_session_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    checkout_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    consented_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProcessedWebhook(Base):
    __tablename__ = "processed_webhooks"

    event_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentHeartbeat(Base):
    __tablename__ = "agent_heartbeats"

    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
