import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from api.routes import router
from database.session import get_session_factory, initialize_database
from monitoring.health import AGENT_NAMES, heartbeat


async def _heartbeat_worker() -> None:
    while True:
        session = get_session_factory()()
        try:
            for name in AGENT_NAMES:
                heartbeat(session, name, "alive")
            session.commit()
        except Exception:
            session.rollback()
        finally:
            session.close()
        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    heartbeat_task = asyncio.create_task(_heartbeat_worker())
    try:
        yield
    finally:
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task


app = FastAPI(
    title="YIMI Autonomous Agent Platform",
    description="Tenant-scoped SaaS API with metering, billing safeguards, and agent health.",
    version="0.2.0",
    lifespan=lifespan,
)
app.include_router(router)
