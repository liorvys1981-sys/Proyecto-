import hashlib
import hmac
import asyncio
import json
import os
import time
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

os.environ["DATABASE_URL"] = "sqlite://"

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
                "STRIPE_SECRET_KEY": "",
                "STRIPE_WEBHOOK_SECRET": "test-webhook-secret",
                "STRIPE_SUCCESS_URL": "https://example.test/success",
                "STRIPE_CANCEL_URL": "https://example.test/cancel",
                "PRICE_CENTS_PER_CPU_SECOND": (
                    '{"goal_analysis":30000,"risk_assessment":30000}'
                ),
            },
        )
        self.environment.start()
        Base.metadata.drop_all(get_engine())
        Base.metadata.create_all(get_engine())
        self.session = get_session_factory()()
        self.addCleanup(self.session.close)
        self.addCleanup(self.environment.stop)

    def tenant(self, tenant_id="tenant-a", key="test-key-a") -> str:
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=key)
        return asyncio.run(resolve_tenant(tenant_id, credentials))

    def test_runs_are_persisted_metered_and_tenant_scoped(self) -> None:
        run = create_agent_run(
            AgentRunRequest(goal="Review service health"), self.session, self.tenant()
        )
        retrieved = get_agent_run(run["id"], self.session, "tenant-a")
        preview = billing_preview(self.session, "tenant-a")

        self.assertEqual(retrieved["id"], run["id"])
        self.assertEqual(run["steps"][-1]["status"], "simulated")
        self.assertEqual(len(preview["events"]), 2)
        self.assertGreaterEqual(preview["billable_cents"], 0)
        self.assertGreaterEqual(preview["cpu_milliseconds"], 2)
        with self.assertRaises(HTTPException) as error:
            get_agent_run(run["id"], self.session, "tenant-b")
        self.assertEqual(error.exception.status_code, 404)

    def test_risky_run_waits_for_admin_approval(self) -> None:
        run = create_agent_run(
            AgentRunRequest(goal="risky action", requires_approval=True),
            self.session,
            self.tenant(),
        )
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
        create_agent_run(
            AgentRunRequest(goal="Review service health"), self.session, self.tenant()
        )
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
                    CheckoutRequest(consent=True, approved_amount_cents=60),
                    self.session,
                    "tenant-a",
                )
        self.assertEqual(unconfigured.exception.status_code, 503)

    def test_checkout_requires_consent_and_webhook_marks_paid_idempotently(self) -> None:
        create_agent_run(
            AgentRunRequest(goal="Review service health"), self.session, self.tenant()
        )
        checkout_response = type(
            "Checkout",
            (),
            {"id": "cs_test_123", "url": "https://checkout.stripe.test/session"},
        )()
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
        with (
            patch.dict(
                os.environ,
                {
                    "PRICE_CENTS_PER_CPU_SECOND": (
                        '{"goal_analysis":2,"risk_assessment":2}'
                    ),
                    "STRIPE_SECRET_KEY": "sk_test_dummy",
                },
            ),
            self.assertRaises(HTTPException) as error,
        ):
            create_agent_run(
                AgentRunRequest(goal="small usage"), self.session, self.tenant()
            )
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

        self.assertEqual(error.exception.status_code, 409)
        self.assertGreater(billing_preview(self.session, "tenant-a")["billable_cents"], 0)

    def test_transient_agent_failure_recovers_and_reports_health(self) -> None:
        with patch(
            "agents.orchestrator.service.analyze_goal",
            side_effect=[RuntimeError("temporary"), "analysis recovered"],
        ) as analyze:
            run = create_agent_run(
                AgentRunRequest(goal="retry test"), self.session, self.tenant()
            )

        self.assertEqual(analyze.call_count, 2)
        self.assertEqual(run["steps"][0]["detail"], "analysis recovered")
        status_by_name = {
            entry["agent"]: entry["status"]
            for entry in agent_health(self.session)
        }
        self.assertEqual(status_by_name["analytics"], "healthy")

    def test_admin_metrics_are_tenant_agnostic_aggregates(self) -> None:
        create_agent_run(
            AgentRunRequest(goal="metrics test"), self.session, self.tenant()
        )
        metrics = admin_metrics(self.session)
        self.assertEqual(metrics["usage_events"], 2)
        self.assertGreaterEqual(metrics["cpu_milliseconds"], 2)

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


if __name__ == "__main__":
    unittest.main()
