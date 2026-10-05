"""Aliases de dependências usados pelas rotas."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from llmgw.db import get_session
from llmgw.security.auth import Principal, api_key_principal, get_principal, require_admin
from llmgw.state import AppState, get_state

Session = Annotated[AsyncSession, Depends(get_session)]
State = Annotated[AppState, Depends(get_state)]
User = Annotated[Principal, Depends(get_principal)]
Admin = Annotated[Principal, Depends(require_admin)]
ApiCaller = Annotated[Principal, Depends(api_key_principal)]
