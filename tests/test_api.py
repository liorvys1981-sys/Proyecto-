import asyncio
import hashlib
import hmac
import json
import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

os.environ["DATABASE_URL"] = "sqlite://"

from agents.orchestrator.service import (
    create_queued_run,
    expire_stuck_runs,
    process_next_run,
)
from api.routes import (
    AgentRunRequest,
    CheckoutRequest,
    admin_metrics,
    approve_agent_run,
    billing_preview,
    create_agent_run,
    create_checkout,
    get_agent_run,
    require_admin,
    resolve_tenant,
    stripe_webhook,
)
from config.settings import token_price_tiers
from database.models import Base, BillingInvoice
from database.session import get_engine, get_session_factory
from monitoring.health import agent_health


class AgentApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(
            os.environ,
            {
                "TENANT_API_KEYS": '{"tenant-a":"test-key-a","tenant-b":"test-key-b"}',
                "ADMIN_API_KEY": "test-admin-key",
                "OPENAI_API_KEY": "test-openai-key",
                "STRIPE_SECRET_KEY": "",
                "STRIPE_WEBHOOK_SECRET": "test-webhook-secret",
                "STRIPE_SUCCESS_URL": "https://example.test/success",
                "STRIPE_CANCEL_URL": "https://example.test/cancel",
                "TOKEN_PRICE_TIERS_JSON": (
                    '[{"monthly_tokens":0,"markup_bps":3000},'
                    '{"monthly_tokens":100000,"markup_bps":3500},'
                    '{"monthly_tokens":500000,"markup_bps":5000}]'
                ),
            },
        )
        self.environment.start()
        Base.metadata.drop_all(get_engine())
        Base.metadata.create_all(get_engine())
        self.session = get_session_factory()()
        self.fake_client = FakeOpenAIClient()
        self.addCleanup(self.session.close)
        self.addCleanup(self.environment.stop)

    def tenant(self, tenant_id="tenant-a", key="test-key-a") -> str:
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=key)
        return asyncio.run(resolve_tenant(tenant_id, credentials))

    def enqueue_and_process(self, goal: str, requires_approval: bool = False) -> dict:
        run = create_agent_run(
            AgentRunRequest(goal=goal, requires_approval=requires_approval),
            self.session,
            self.tenant(),
        )
        self.assertEqual(run["status"], "queued")
        self.assertTrue(asyncio.run(process_next_run(self.session, self.fake_client)))
        return get_agent_run(run["id"], self.session, "tenant-a")

    def test_runs_are_persisted_metered_and_tenant_scoped(self) -> None:
        run = self.enqueue_and_process("Review service health")
        retrieved = get_agent_run(run["id"], self.session, "tenant-a")
        preview = billing_preview(self.session, "tenant-a")

        self.assertEqual(retrieved["id"], run["id"])
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["usage"][0]["model"], "gpt-4o")
        self.assertEqual(run["usage"][0]["input_tokens"], 100_000)
        self.assertEqual(run["usage"][0]["output_tokens"], 50_000)
        self.assertEqual(len(preview["events"]), 1)
        self.assertGreaterEqual(preview["billable_cents"], 50)
        self.assertEqual(self.fake_client.completions.request["model"], "gpt-4o")
        self.assertNotIn("tools", self.fake_client.completions.request)

        with self.assertRaises(HTTPException) as error:
            get_agent_run(run["id"], self.session, "tenant-b")
        self.assertEqual(error.exception.status_code, 404)

    def test_risky_run_waits_for_admin_approval(self) -> None:
        run = self.enqueue_and_process("risky action", requires_approval=True)
        self.assertEqual(run["status"], "pending_approval")

        with self.assertRaises(HTTPException) as error:
            require_admin(None)
        self.assertEqual(error.exception.status_code, 401)
        require_admin("test-admin-key")

        approved = approve_agent_run(run["id"], self.session, None)
        self.assertEqual(approved["status"], "completed")
        self.assertEqual(approved["steps"][-1]["status"], "approved_simulation")

    def test_tenant_authentication_and_missing_configuration(self) -> None:
        with self.assertRaises(HTTPException) as error:
            asyncio.run(
                resolve_tenant(
                    "tenant-a",
                    HTTPAuthorizationCredentials(
                        scheme="Bearer", credentials="wrong-key"
                    ),
                )
            )
        self.assertEqual(error.exception.status_code, 401)

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(HTTPException) as missing:
                asyncio.run(
                    resolve_tenant(
                        "tenant-a",
                        HTTPAuthorizationCredentials(
                            scheme="Bearer", credentials="test-key-a"
                        ),
                    )
                )
        self.assertEqual(missing.exception.status_code, 503)

    def test_checkout_requires_consent_and_stripe_configuration(self) -> None:
        self.enqueue_and_process("billing consent test")
        with self.assertRaises(HTTPException) as consent:
            create_checkout(
                CheckoutRequest(consent=False, approved_amount_cents=0),
                self.session,
                "tenant-a",
            )
        self.assertEqual(consent.exception.status_code, 400)

        with patch.dict(os.environ, {"STRIPE_SECRET_KEY": ""}):
            with self.assertRaises(HTTPException) as unconfigured:
                create_checkout(
                    CheckoutRequest(
                        consent=True,
                        approved_amount_cents=billing_preview(
                            self.session, "tenant-a"
                        )["billable_cents"],
                    ),
                    self.session,
                    "tenant-a",
                )
        self.assertEqual(unconfigured.exception.status_code, 503)

    def test_checkout_and_signed_webhook_are_idempotent(self) -> None:
        self.enqueue_and_process("checkout test")
        checkout_response = SimpleNamespace(
            id="cs_test_123", url="https://checkout.stripe.test/session"
        )
        with (
            patch.dict(os.environ, {"STRIPE_SECRET_KEY": "sk_test_dummy"}),
            patch(
                "api.routes.stripe.checkout.Session.create",
                return_value=checkout_response,
            ) as stripe_create,
        ):
            checkout = create_checkout(
                CheckoutRequest(
                    consent=True,
                    approved_amount_cents=billing_preview(
                        self.session, "tenant-a"
                    )["billable_cents"],
                ),
                self.session,
                "tenant-a",
            )
            repeated = create_checkout(
                CheckoutRequest(
                    consent=True, approved_amount_cents=checkout["amount_cents"]
                ),
                self.session,
                "tenant-a",
            )

        self.assertEqual(checkout["status"], "checkout_open")
        self.assertEqual(repeated["checkout_url"], checkout["checkout_url"])
        self.assertEqual(stripe_create.call_count, 1)

        invoice_id = checkout["invoice_id"]
        invoice = self.session.get(BillingInvoice, invoice_id)
        self.assertIsNotNone(invoice.consented_at)
        event = {
            "id": "evt_test_123",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test_123",
                    "amount_total": invoice.amount_cents,
                    "currency": "usd",
                    "payment_status": "paid",
                    "metadata": {
                        "invoice_id": invoice_id,
                        "tenant_id": "tenant-a",
                    },
                }
            },
        }
        payload = json.dumps(event, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        signature = hmac.new(
            b"test-webhook-secret",
            timestamp.encode() + b"." + payload,
            hashlib.sha256,
        ).hexdigest()
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "headers": [
                    (b"stripe-signature", f"t={timestamp},v1={signature}".encode())
                ],
            },
            receive=receive_body(payload),
        )
        paid = asyncio.run(stripe_webhook(request, self.session))
        repeated_webhook = asyncio.run(stripe_webhook(request, self.session))

        self.assertEqual(paid["status"], "processed")
        self.assertEqual(repeated_webhook["status"], "already_processed")
        self.assertEqual(self.session.get(BillingInvoice, invoice_id).status, "paid")

    def test_usage_below_provider_minimum_remains_unbilled(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENAI_INPUT_COST_MICROUSD_PER_MILLION": "100000",
                "OPENAI_OUTPUT_COST_MICROUSD_PER_MILLION": "100000",
                "STRIPE_SECRET_KEY": "sk_test_dummy",
            },
        ):
            self.enqueue_and_process("small usage")
            preview = billing_preview(self.session, "tenant-a")
            self.assertGreater(preview["billable_cents"], 0)
            self.assertLess(preview["billable_cents"], 50)
            with self.assertRaises(HTTPException) as error:
                create_checkout(
                    CheckoutRequest(
                        consent=True,
                        approved_amount_cents=preview["billable_cents"],
                    ),
                    self.session,
                    "tenant-a",
                )

        self.assertEqual(error.exception.status_code, 409)
        self.assertGreater(
            billing_preview(self.session, "tenant-a")["billable_cents"], 0
        )

    def test_price_markup_rises_at_monthly_token_tiers(self) -> None:
        self.enqueue_and_process("first call crosses 100k tier")
        run = create_agent_run(
            AgentRunRequest(goal="cross pricing threshold"), self.session, "tenant-a"
        )
        self.fake_client.completions.usage = SimpleNamespace(
            prompt_tokens=400_000, completion_tokens=100_000
        )
        self.assertTrue(asyncio.run(process_next_run(self.session, self.fake_client)))
        charged_run = get_agent_run(run["id"], self.session, "tenant-a")
        self.assertEqual(charged_run["usage"][0]["markup_bps"], 5000)

    def test_queued_worker_claims_each_job_once(self) -> None:
        run = create_agent_run(
            AgentRunRequest(goal="one worker call"), self.session, self.tenant()
        )
        self.assertTrue(asyncio.run(process_next_run(self.session, self.fake_client)))
        self.assertFalse(asyncio.run(process_next_run(self.session, self.fake_client)))
        self.assertEqual(
            get_agent_run(run["id"], self.session, "tenant-a")["status"],
            "completed",
        )

    def test_stuck_run_expires_without_provider_retry(self) -> None:
        queued = create_queued_run(self.session, "tenant-a", "stuck worker")
        self.session.refresh(queued)
        queued.status = "processing"
        queued.processing_started_at = datetime.now(timezone.utc) - timedelta(
            seconds=300
        )
        self.session.commit()

        self.assertEqual(expire_stuck_runs(self.session), 1)
        self.assertEqual(
            get_agent_run(queued.id, self.session, "tenant-a")["status"], "failed"
        )

    def test_provider_failure_is_not_retried_or_charged(self) -> None:
        run = create_agent_run(
            AgentRunRequest(goal="provider failure test"),
            self.session,
            self.tenant(),
        )
        self.fake_client.completions.side_effect = RuntimeError("provider unavailable")
        self.assertTrue(asyncio.run(process_next_run(self.session, self.fake_client)))
        failed = get_agent_run(run["id"], self.session, "tenant-a")

        self.assertEqual(failed["status"], "failed")
        self.assertEqual(self.fake_client.completions.call_count, 1)
        self.assertEqual(billing_preview(self.session, "tenant-a")["total_tokens"], 0)

    def test_admin_metrics_count_provider_tokens(self) -> None:
        self.enqueue_and_process("metrics test")
        metrics = admin_metrics(self.session)

        self.assertEqual(metrics["usage_events"], 1)
        self.assertEqual(metrics["total_tokens"], 150_000)
        self.assertEqual(metrics["input_tokens"], 100_000)
        self.assertEqual(metrics["output_tokens"], 50_000)

    def test_tier_configuration_is_monotonic_and_threshold_based(self) -> None:
        self.assertEqual(token_price_tiers()[0]["monthly_tokens"], 0)
        with patch.dict(
            os.environ,
            {
                "TOKEN_PRICE_TIERS_JSON": (
                    '[{"monthly_tokens":0,"markup_bps":3000},'
                    '{"monthly_tokens":100,"markup_bps":2000}]'
                )
            },
        ):
            with self.assertRaises(RuntimeError):
                token_price_tiers()

    def test_stripe_webhook_rejects_invalid_signatures(self) -> None:
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "headers": [(b"stripe-signature", b"invalid")],
            },
            receive=receive_body(b"{}"),
        )
        with self.assertRaises(HTTPException) as error:
            asyncio.run(stripe_webhook(request, self.session))
        self.assertEqual(error.exception.status_code, 400)


def receive_body(payload: bytes):
    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    return receive


class FakeOpenAIClient:
    def __init__(self) -> None:
        self.completions = FakeCompletions()
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.completions)
        )


class FakeCompletions:
    def __init__(self) -> None:
        self.call_count = 0
        self.request = {}
        self.side_effect = None
        self.usage = SimpleNamespace(prompt_tokens=100_000, completion_tokens=50_000)

    async def __call__(self, **request):
        self.call_count += 1
        self.request = request
        if self.side_effect:
            raise self.side_effect
        return SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content="GPT-4o test answer"))
            ],
            usage=self.usage,
        )


if __name__ == "__main__":
    unittest.main()
