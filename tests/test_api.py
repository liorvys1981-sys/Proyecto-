import asyncio
import os
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app import main


class AgentApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api_keys = patch.dict(
            os.environ,
            {
                "TENANT_API_KEYS": (
                    '{"tenant-a":"test-key-a","tenant-b":"test-key-b"}'
                )
            },
        )
        self.api_keys.start()
        main.tenant_api_keys.cache_clear()
        main._runs.clear()
        self.addCleanup(self.api_keys.stop)
        self.addCleanup(main.tenant_api_keys.cache_clear)

    def test_create_and_retrieve_run_for_owning_tenant(self) -> None:
        tenant = asyncio.run(
            main.resolve_tenant(
                "tenant-a",
                HTTPAuthorizationCredentials(
                    scheme="Bearer", credentials="test-key-a"
                ),
            )
        )
        run = asyncio.run(
            main.create_agent_run(
                main.AgentRunRequest(goal="Review service health"), tenant
            )
        )

        retrieved = asyncio.run(main.get_agent_run(run.id, tenant))

        self.assertEqual(retrieved.id, run.id)
        self.assertEqual(retrieved.steps[-1].status, "simulated")

    def test_run_is_hidden_from_other_tenants(self) -> None:
        run = asyncio.run(
            main.create_agent_run(
                main.AgentRunRequest(goal="Review service health"), "tenant-a"
            )
        )

        with self.assertRaises(HTTPException) as error:
            asyncio.run(main.get_agent_run(run.id, "tenant-b"))

        self.assertEqual(error.exception.status_code, 404)

    def test_invalid_key_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as error:
            asyncio.run(
                main.resolve_tenant(
                    "tenant-a",
                    HTTPAuthorizationCredentials(
                        scheme="Bearer", credentials="wrong-key"
                    ),
                )
            )

        self.assertEqual(error.exception.status_code, 401)

    def test_missing_credentials_are_rejected(self) -> None:
        with self.assertRaises(HTTPException) as error:
            asyncio.run(main.resolve_tenant("tenant-a", None))

        self.assertEqual(error.exception.status_code, 401)

    def test_missing_tenant_configuration_is_unavailable(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            main.tenant_api_keys.cache_clear()
            with self.assertRaises(HTTPException) as error:
                asyncio.run(
                    main.resolve_tenant(
                        "tenant-a",
                        HTTPAuthorizationCredentials(
                            scheme="Bearer", credentials="test-key-a"
                        ),
                    )
                )

        self.assertEqual(error.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
