import asyncio
from contextlib import asynccontextmanager, suppress
import logging

from fastapi import FastAPI

from agents.bus import process_next_message
from agents.orchestrator.service import expire_stuck_runs, process_next_run
from api.routes import router
from config.settings import openai_api_key, validate_token_pricing
from core.llm.gpt4o import create_client
from database.session import get_session_factory, initialize_database
from monitoring.health import PROCESS_HEARTBEAT_NAMES, heartbeat

logger = logging.getLogger(__name__)


async def _heartbeat_worker(openai_configured: bool) -> None:
    while True:
        session = get_session_factory()()
        try:
            for name in PROCESS_HEARTBEAT_NAMES:
                state = (
                    "alive"
                    if name != "gpt4o" or openai_configured
                    else "unconfigured"
                )
                heartbeat(session, name, state)
            session.commit()
        except Exception:
            session.rollback()
        finally:
            session.close()
        await asyncio.sleep(30)


async def _queue_worker(client) -> None:
    while True:
        session = get_session_factory()()
        try:
            expire_stuck_runs(session)
            processed = await process_next_run(session, client)
        except Exception as error:
            session.rollback()
            logger.warning("Agent worker iteration failed: %s", type(error).__name__)
            processed = False
        finally:
            session.close()
        if not processed:
            await asyncio.sleep(1)


async def _message_worker() -> None:
    while True:
        session = get_session_factory()()
        try:
            delivered = process_next_message(session)
            heartbeat(session, "message_bus", "alive")
            session.commit()
        except Exception as error:
            session.rollback()
            heartbeat(session, "message_bus", "unhealthy", type(error).__name__)
            session.commit()
            logger.warning(
                "Agent message worker iteration failed: %s", type(error).__name__
            )
            delivered = False
        finally:
            session.close()
        if not delivered:
            await asyncio.sleep(0.5)


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    api_key = openai_api_key()
    if api_key:
        validate_token_pricing()
    client = create_client(api_key) if api_key else None
    heartbeat_task = asyncio.create_task(_heartbeat_worker(bool(client)))
    worker_task = asyncio.create_task(_queue_worker(client)) if client else None
    message_task = asyncio.create_task(_message_worker())
    try:
        yield
    finally:
        heartbeat_task.cancel()
        if worker_task:
            worker_task.cancel()
        message_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task
        if worker_task:
            with suppress(asyncio.CancelledError):
                await worker_task
        with suppress(asyncio.CancelledError):
            await message_task
        if client:
            await client.close()


app = FastAPI(
    title="YIMI Autonomous Agent Platform",
    description="Tenant-scoped SaaS API with metering, billing safeguards, and agent health.",
    version="0.2.0",
    lifespan=lifespan,
)
app.include_router(router)
