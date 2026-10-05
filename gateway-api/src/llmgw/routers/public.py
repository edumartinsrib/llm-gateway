from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from llmgw import __version__
from llmgw import schemas as s
from llmgw.deps import Session, State

router = APIRouter()


@router.get("/api/v1/config", response_model=s.ConfigOut, tags=["console"])
async def config(state: State) -> s.ConfigOut:
    st = state.settings
    return s.ConfigOut(
        auth=s.AuthConfig(mode=st.auth_mode),
        version=__version__,
        api_base_url=st.public_url.rstrip("/") + "/v1",
        cache=s.CacheConfig(
            enabled=st.cache_enabled,
            scope=st.cache_scope,
            chat_ttl_s=st.cache_chat_ttl_s,
            embeddings_ttl_s=st.cache_embeddings_ttl_s,
        ),
        timezone=st.timezone,
        budget_alert_pct=st.budget_alert_pct,
    )


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict:
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
async def readyz(session: Session, state: State) -> dict:
    await session.execute(text("SELECT 1"))
    return {"status": "ready", "models_routable": len(state.gateway.routes())}
