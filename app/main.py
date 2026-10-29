import json
import os
import secrets
from datetime import datetime, timezone
from functools import lru_cache
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

app = FastAPI(
    title="YIMI Autonomous Agent Platform",
    description="Tenant-scoped SaaS API starter with a simulated agent workflow.",
    version="0.1.0",
)

bearer_scheme = HTTPBearer(auto_error=False)


@lru_cache
def tenant_api_keys() -> dict[str, str]:
    raw_keys = os.getenv("TENANT_API_KEYS", "{}")
    try:
        keys = json.loads(raw_keys)
    except json.JSONDecodeError as error:
        raise RuntimeError("TENANT_API_KEYS must contain a JSON object") from error
    if not isinstance(keys, dict) or any(
        not isinstance(tenant_id, str)
        or not tenant_id
        or not isinstance(api_key, str)
        or not api_key
        for tenant_id, api_key in keys.items()
    ):
        raise RuntimeError("TENANT_API_KEYS must map non-empty tenant IDs to API keys")
    return keys


class AgentRunRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=2000)


class AgentStep(BaseModel):
    agent: str
    status: str
    detail: str


class AgentRun(BaseModel):
    id: str
    status: str
    goal: str
    created_at: datetime
    steps: list[AgentStep]
    result: str


async def resolve_tenant(
    tenant_id: Annotated[str, Header(alias="X-Tenant-ID")],
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ],
) -> str:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        api_key = tenant_api_keys().get(tenant_id)
    except RuntimeError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Tenant authentication is not configured",
        ) from error
    if api_key is None or not secrets.compare_digest(
        api_key.encode("utf-8"), credentials.credentials.encode("utf-8")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid tenant credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return tenant_id


_runs: dict[str, tuple[str, AgentRun]] = {}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/v1/agent-runs",
    response_model=AgentRun,
    status_code=status.HTTP_201_CREATED,
)
async def create_agent_run(
    request: AgentRunRequest,
    tenant_id: Annotated[str, Depends(resolve_tenant)],
) -> AgentRun:
    now = datetime.now(timezone.utc)
    run = AgentRun(
        id=str(uuid4()),
        status="completed",
        goal=request.goal,
        created_at=now,
        steps=[
            AgentStep(agent="analysis", status="completed", detail="Goal analyzed."),
            AgentStep(agent="risk", status="completed", detail="No external action taken."),
            AgentStep(agent="execution", status="simulated", detail="Execution is disabled."),
        ],
        result="Simulation completed. No external systems were contacted.",
    )
    _runs[run.id] = (tenant_id, run)
    return run


@app.get("/v1/agent-runs/{run_id}", response_model=AgentRun)
async def get_agent_run(
    run_id: str,
    tenant_id: Annotated[str, Depends(resolve_tenant)],
) -> AgentRun:
    stored_run = _runs.get(run_id)
    if stored_run is None or not secrets.compare_digest(stored_run[0], tenant_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return stored_run[1]
