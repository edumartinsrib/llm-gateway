"""Erros da API em dois formatos.

- Console e administração (`/api/v1`): `{"detail": "...", "errors": [...]}`, como no Orquestrador.
- Rotas compatíveis com OpenAI (`/v1`): `{"error": {"message", "type", "code", "param"}}`,
  o formato que os SDKs e ferramentas da OpenAI esperam.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(self, status: int, detail: str, errors: list[str] | None = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.errors = errors


def not_found(what: str) -> ApiError:
    return ApiError(404, f"{what} não encontrado")


def conflict(detail: str) -> ApiError:
    return ApiError(409, detail)


def unprocessable(detail: str, errors: list[str] | None = None) -> ApiError:
    return ApiError(422, detail, errors)


class OpenAIError(Exception):
    """Erro no formato da OpenAI, usado nas rotas `/v1`."""

    def __init__(
        self,
        status: int,
        message: str,
        *,
        type: str = "invalid_request_error",
        code: str | None = None,
        param: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.message = message
        self.type = type
        self.code = code
        self.param = param
        self.headers = headers or {}

    def body(self) -> dict:
        return {
            "error": {
                "message": self.message,
                "type": self.type,
                "code": self.code,
                "param": self.param,
            }
        }

    def response(self) -> JSONResponse:
        return JSONResponse(self.body(), status_code=self.status, headers=self.headers)


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        body: dict = {"detail": exc.detail}
        if exc.errors:
            body["errors"] = exc.errors
        return JSONResponse(body, status_code=exc.status)

    @app.exception_handler(OpenAIError)
    async def _openai_error(_: Request, exc: OpenAIError) -> JSONResponse:
        return exc.response()

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"] if p not in ("body", "query", "path"))
            errors.append(f"{loc}: {err['msg']}" if loc else err["msg"])
        if request.url.path.startswith("/v1/"):
            return OpenAIError(422, "; ".join(errors) or "requisição inválida").response()
        return JSONResponse({"detail": "requisição inválida", "errors": errors}, status_code=422)
