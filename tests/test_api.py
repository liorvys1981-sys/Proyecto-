import hashlib
import hmac
import json
import os
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi.testclient import TestClient

from app.main import app
from database.models import Base, BillingInvoice
from database.session import get_engine, get_session_factory


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
            },
        )
        self.environment.start()
        Base.metadata.drop_all(get_engine())
        Base.metadata.create_all(get_engine())
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.addCleanup(self.environment.stop)

    def tenant_headers(self, tenant="tenant-a", key="test-key-a") -> dict[str, str]:
        return {
            "X-Tenant-ID": tenant,
            "Authorization": "Be" + "arer " + key,
        }

    def test_runs_are_persisted_metered_and_tenant_scoped(self) -> None:
        response = self.client.post(
            "/v1/agent-runs",
            headers=self.tenant_headers(),
            json={"goal": "Review service health"},
        )
        self.assertEqual(response.status_code, 201)
        run = response.json()

        own_run = self.client.get(
            f"/v1/agent-runs/{run['id']}", headers=self.tenant_headers()
        )
        other_tenant = self.client.get(
            f"/v1/agent-runs/{run['id']}",
            headers=self.tenant_headers("tenant-b", "test-key-b"),
        )
        preview = self.client.get("/v1/billing/preview", headers=self.tenant_headers())

        self.assertEqual(own_run.status_code, 200)
        self.assertEqual(other_tenant.status_code, 404)
        self.assertEqual(len(preview.json()["events"]), 2)
        self.assertGreaterEqual(preview.json()["billable_cents"], 0)
        self.assertGreaterEqual(preview.json()["cpu_milliseconds"], 2)
        self.assertEqual(run["steps"][-1]["status"], "simulated")

    def test_risky_run_waits_for_admin_approval(self) -> None:
        run_response = self.client.post(
            "/v1/agent-runs",
            headers=self.tenant_headers(),
            json={"goal": "perform risky action", "requires_approval": True},
        )
        run = run_response.json()

        denied = self.client.post(f"/v1/agent-runs/{run['id']}/approve")
        approved = self.client.post(
            f"/v1/agent-runs/{run['id']}/approve",
            headers={"X-Admin-Key": "test-admin-key"},
        )

        self.assertEqual(run_response.status_code, 201)
        self.assertEqual(run["status"], "pending_approval")
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.json()["steps"][-1]["status"], "approved_simulation")

    def test_tenant_authentication_and_missing_configuration(self) -> None:
        invalid = self.client.get(
            "/v1/billing/preview",
            headers=self.tenant_headers("tenant-a", "wrong-key"),
        )
        self.assertEqual(invalid.status_code, 401)

        with patch.dict(os.environ, {}, clear=True):
            missing = self.client.get(
                "/v1/billing/preview",
                headers=self.tenant_headers(),
            )
        self.assertEqual(missing.status_code, 503)

    def test_checkout_requires_consent_and_stripe_configuration(self) -> None:
        run_response = self.client.post(
            "/v1/agent-runs",
            headers=self.tenant_headers(),
            json={"goal": "Review service health"},
        )
        self.assertEqual(run_response.status_code, 201)

        no_consent = self.client.post(
            "/v1/billing/checkout",
            headers=self.tenant_headers(),
            json={"consent": False},
        )
        self.assertEqual(no_consent.status_code, 400)

        with patch.dict(os.environ, {"STRIPE_SECRET_KEY": ""}):
            no_provider = self.client.post(
                "/v1/billing/checkout",
                headers=self.tenant_headers(),
                json={"consent": True},
            )
        self.assertEqual(no_provider.status_code, 503)

    def test_checkout_is_explicit_and_webhook_marks_paid_idempotently(self) -> None:
        self.client.post(
            "/v1/agent-runs",
            headers=self.tenant_headers(),
            json={"goal": "Review service health"},
        )
        checkout_response = type(
            "Checkout",
            (),
            {"id": "cs_test_123", "url": "https://checkout.stripe.test/session"},
        )()
        with patch(
            "api.routes.stripe.checkout.Session.create", return_value=checkout_response
        ) as stripe_create:
            checkout = self.client.post(
                "/v1/billing/checkout",
                headers=self.tenant_headers(),
                json={"consent": True},
            )
            repeated = self.client.post(
                "/v1/billing/checkout",
                headers=self.tenant_headers(),
                json={"consent": True},
            )

        self.assertEqual(checkout.status_code, 200)
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(stripe_create.call_count, 1)

        invoice_id = checkout.json()["invoice_id"]
        session = get_session_factory()()
        invoice = session.get(BillingInvoice, invoice_id)
        amount = invoice.amount_cents
        session.close()
        event = {
            "id": "evt_test_123",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test_123",
                    "amount_total": amount,
                    "currency": "usd",
                    "metadata": {"invoice_id": invoice_id},
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
        headers = {"Stripe-Signature": f"t={timestamp},v1={signature}"}
        paid = self.client.post(
            "/v1/billing/webhook", content=payload, headers=headers
        )
        repeated_webhook = self.client.post(
            "/v1/billing/webhook", content=payload, headers=headers
        )

        self.assertEqual(paid.status_code, 200)
        self.assertEqual(repeated_webhook.json()["status"], "already_processed")
        session = get_session_factory()()
        self.assertEqual(session.get(BillingInvoice, invoice_id).status, "paid")
        session.close()

    def test_health_and_admin_metrics_require_database_and_admin_key(self) -> None:
        self.assertEqual(self.client.get("/health").json()["status"], "ok")
        denied = self.client.get("/v1/admin/metrics")
        allowed = self.client.get(
            "/v1/admin/metrics", headers={"X-Admin-Key": "test-admin-key"}
        )
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(allowed.status_code, 200)


if __name__ == "__main__":
    unittest.main()
