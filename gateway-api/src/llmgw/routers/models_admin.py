"""Catálogo de modelos: usuários veem os que podem usar; administradores gerenciam todos."""

from __future__ import annotations

import uuid

from fastapi import APIRouter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from llmgw import models as m
from llmgw import schemas as s
from llmgw.deps import Admin, Session, State, User
from llmgw.errors import conflict, not_found
from llmgw.services import audit
from llmgw.services.serializers import model_accessible, model_out

router = APIRouter(prefix="/api/v1", tags=["modelos"])

EDITABLE = [
    "description",
    "input_price",
    "cached_input_price",
    "output_price",
    "cache_ttl_s",
    "allowed_groups",
    "context_window",
    "max_output_tokens",
    "supports_responses",
    "supports_vision",
    "supports_reasoning",
]
SNAPSHOT = ["name", "kind", "enabled", *EDITABLE]
NULLABLE = {
    "description",
    "cached_input_price",
    "cache_ttl_s",
    "context_window",
    "max_output_tokens",
}


async def _model(session: AsyncSession, model_id: str) -> m.LlmModel:
    try:
        model = await session.get(m.LlmModel, uuid.UUID(model_id))
    except ValueError:
        model = None
    if model is None:
        raise not_found("modelo")
    return model


async def _name_free(session: AsyncSession, name: str, own_id: uuid.UUID | None = None) -> None:
    found = await session.scalar(select(m.LlmModel.id).where(m.LlmModel.name == name))
    if found is not None and found != own_id:
        raise conflict(f"já existe um modelo chamado '{name}'")


def _apply(model: m.LlmModel, body: s.ModelFields) -> None:
    for field in EDITABLE:
        if field not in body.model_fields_set:
            continue
        value = getattr(body, field)
        if value is None and field not in NULLABLE:
            continue
        if field == "allowed_groups":
            value = list(dict.fromkeys(g.strip() for g in (value or []) if g.strip()))
        setattr(model, field, value)


@router.get("/models", response_model=list[s.ModelOut])
async def list_models(
    session: Session, state: State, principal: User, all: bool = False
) -> list[s.ModelOut]:
    rows = (await session.scalars(select(m.LlmModel).order_by(m.LlmModel.name))).all()
    out = []
    for model in rows:
        routable = state.gateway.route(model.name) is not None
        accessible = routable and model_accessible(model, principal.user, principal.is_admin)
        if not (all and principal.is_admin) and not accessible:
            continue
        out.append(model_out(model, state.settings, routable=routable, accessible=accessible))
    return out


@router.post("/models", response_model=s.ModelOut, status_code=201)
async def create_model(body: s.ModelCreate, session: Session, state: State, principal: Admin):
    try:
        provider = await session.get(m.Provider, uuid.UUID(body.provider_id))
    except ValueError:
        provider = None
    if provider is None:
        raise not_found("provedor")
    deployment = body.deployment.strip()
    exists = await session.scalar(
        select(m.LlmModel.id).where(
            m.LlmModel.provider_id == provider.id, m.LlmModel.deployment == deployment
        )
    )
    if exists:
        raise conflict(f"o deployment '{deployment}' já está cadastrado neste provedor")
    name = (body.name or deployment).strip()
    await _name_free(session, name)
    model = m.LlmModel(
        name=name,
        provider_id=provider.id,
        deployment=deployment,
        kind=body.kind,
        upstream_model=deployment,
        source="manual",
        enabled=body.enabled,
        available=True,
        supports_responses=body.kind == "chat",
    )
    _apply(model, body)
    session.add(model)
    await session.flush()
    audit.record(
        session,
        actor=principal.username,
        action="model.create",
        resource_type="model",
        resource_id=model.id,
        detail=model.name,
        after=audit.snapshot(model, SNAPSHOT),
    )
    await session.commit()
    await state.gateway.notify_changed()
    await session.refresh(model)
    routable = state.gateway.route(model.name) is not None
    return model_out(model, state.settings, routable=routable, accessible=routable)


@router.patch("/models/{model_id}", response_model=s.ModelOut)
async def update_model(
    model_id: str, body: s.ModelUpdate, session: Session, state: State, principal: Admin
) -> s.ModelOut:
    model = await _model(session, model_id)
    before = audit.snapshot(model, SNAPSHOT)
    if "name" in body.model_fields_set and body.name and body.name.strip() != model.name:
        await _name_free(session, body.name.strip(), model.id)
        model.name = body.name.strip()
    if body.kind is not None:
        model.kind = body.kind
    if body.enabled is not None:
        model.enabled = body.enabled
    _apply(model, body)
    audit.record(
        session,
        actor=principal.username,
        action="model.update",
        resource_type="model",
        resource_id=model.id,
        detail=model.name,
        before=before,
        after=audit.snapshot(model, SNAPSHOT),
    )
    await session.commit()
    await state.gateway.notify_changed()
    await session.refresh(model)
    routable = state.gateway.route(model.name) is not None
    return model_out(model, state.settings, routable=routable, accessible=routable)


@router.delete("/models/{model_id}", status_code=204)
async def delete_model(model_id: str, session: Session, state: State, principal: Admin) -> None:
    model = await _model(session, model_id)
    audit.record(
        session,
        actor=principal.username,
        action="model.delete",
        resource_type="model",
        resource_id=model.id,
        detail=model.name,
        before=audit.snapshot(model, SNAPSHOT),
    )
    await session.delete(model)
    await session.commit()
    await state.gateway.notify_changed()


@router.post("/cache/clear")
async def clear_cache(session: Session, state: State, principal: Admin) -> dict:
    removed = await state.cache.clear()
    audit.record(
        session,
        actor=principal.username,
        action="cache.clear",
        resource_type="cache",
        detail=f"{removed} resposta(s) removida(s)",
    )
    await session.commit()
    return {"removed": removed}
