"""Teste de interface do console (Playwright) contra a pilha do compose.

Pré-requisitos: `docker compose up -d --build` e `uv run python scripts/demo.py`.

    uv sync --all-packages --group ui
    uv run playwright install chromium
    LLMGW_UI_SCREENSHOTS=docs/screenshots uv run pytest tests/ui -v

Variáveis: LLMGW_UI_URL (padrão http://localhost:8090), LLMGW_UI_PASSWORD (senha dos
usuários de exemplo do LDAP), LLMGW_UI_SCREENSHOTS (pasta dos prints) e LLMGW_UI_CHROMIUM
(executável do navegador, quando não for o baixado pelo Playwright).

Percorre o que um usuário e um administrador fazem: login negado para quem não está no
grupo do LDAP, criar uma chave restrita e usá-la pela API OpenAI (através do nginx do
console), conversar no playground, revogar a chave, as telas de administração e o
layout no celular (sem rolagem horizontal).
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

pytest.importorskip("playwright")
from playwright.sync_api import Browser, Page, expect, sync_playwright  # noqa: E402

BASE = os.environ.get("LLMGW_UI_URL", "http://localhost:8090").rstrip("/")
PASSWORD = os.environ.get("LLMGW_UI_PASSWORD", "Senha@123")
SHOTS = os.environ.get("LLMGW_UI_SCREENSHOTS")


def _stack_up() -> bool:
    try:
        return httpx.get(f"{BASE}/api/v1/config", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


if not _stack_up():
    if "LLMGW_UI_URL" in os.environ:
        raise RuntimeError(f"console não responde em {BASE}")
    pytest.skip(f"console não responde em {BASE} (suba o compose)", allow_module_level=True)


def shot(page: Page, name: str, *, full_page: bool = False) -> None:
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.wait_for_timeout(400)  # animações de diálogo e gaveta
        page.screenshot(path=f"{SHOTS}/{name}.png", full_page=full_page)


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as p:
        executable = os.environ.get("LLMGW_UI_CHROMIUM")
        b = p.chromium.launch(executable_path=executable) if executable else p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def errors() -> list[str]:
    return []


def new_page(browser: Browser, errors: list[str], *, phone: bool = False) -> Page:
    if phone:
        ctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2)
    else:
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))
    return page


def login(page: Page, user: str) -> None:
    page.goto(f"{BASE}/login")
    page.fill("input[autocomplete=username]", user)
    page.fill("input[type=password]", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(f"{BASE}/")
    page.wait_for_load_state("networkidle")


def test_login_rules(browser, errors):
    page = new_page(browser, errors)
    page.goto(f"{BASE}/login")
    page.wait_for_load_state("networkidle")
    shot(page, "01-login")
    page.fill("input[autocomplete=username]", "ana")
    page.fill("input[type=password]", "senha-errada")
    page.click("button[type=submit]")
    expect(page.get_by_text("usuário ou senha inválidos")).to_be_visible()
    # carla existe no LDAP mas não está em nenhum grupo com acesso.
    page.fill("input[autocomplete=username]", "carla")
    page.fill("input[type=password]", PASSWORD)
    page.click("button[type=submit]")
    expect(page.get_by_text("nenhum grupo com acesso")).to_be_visible()
    shot(page, "02-login-negado")
    # Sem sessão, qualquer página volta ao login guardando o destino.
    page.goto(f"{BASE}/uso")
    page.wait_for_url(f"{BASE}/login?next=%2Fuso")
    assert not errors


def test_user_key_lifecycle_and_playground(browser, errors):
    page = new_page(browser, errors)
    login(page, "ana")
    expect(page.get_by_text("Olá, Ana")).to_be_visible()
    assert page.locator("nav").get_by_text("Usuários").count() == 0
    page.goto(f"{BASE}/admin/usuarios")
    expect(page.get_by_text("não existe ou você não tem acesso")).to_be_visible()

    # Chave restrita a um modelo.
    name = f"ui-{int(time.time())}"
    page.goto(f"{BASE}/chaves")
    page.get_by_role("button", name="Nova chave").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("Nome").fill(name)
    dialog.get_by_text("Restringir a alguns modelos").click()
    dialog.get_by_text("gpt-4o-mini").click()
    shot(page, "07-chave-nova")
    dialog.get_by_role("button", name="Criar chave").click()
    created = page.get_by_role("dialog")
    expect(created.get_by_text("Chave criada")).to_be_visible()
    page.wait_for_timeout(300)
    key = created.locator("code, .bt-secret-value").first.inner_text().strip()
    assert key.startswith("llmgw_")
    shot(page, "08-chave-criada")
    created.get_by_role("button", name="Já copiei").click()
    expect(page.get_by_role("row").filter(has_text=name)).to_be_visible()

    # A chave funciona pela API OpenAI atrás do nginx do console, só no modelo liberado.
    headers = {"Authorization": f"Bearer {key}"}
    chat = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": f"olá, {name}"}]}
    ok = httpx.post(f"{BASE}/v1/chat/completions", headers=headers, json=chat, timeout=60)
    assert ok.status_code == 200, ok.text
    assert ok.headers["x-llmgw-cache"] == "miss"
    assert float(ok.headers["x-llmgw-cost-usd"]) > 0
    # A mesma pergunta de novo sai do cache, sem custo.
    again = httpx.post(f"{BASE}/v1/chat/completions", headers=headers, json=chat, timeout=60)
    assert again.headers["x-llmgw-cache"] == "hit"
    assert float(again.headers["x-llmgw-cost-usd"]) == 0
    assert again.json()["choices"] == ok.json()["choices"]
    denied = httpx.post(
        f"{BASE}/v1/chat/completions",
        headers=headers,
        json={"model": "gpt-4.1", "messages": [{"role": "user", "content": "olá"}]},
        timeout=60,
    )
    assert denied.status_code == 403, denied.text
    models = httpx.get(f"{BASE}/v1/models", headers=headers, timeout=30).json()
    assert [m["id"] for m in models["data"]] == ["gpt-4o-mini"]

    # Playground com streaming (sessão do console, sem chave).
    page.goto(f"{BASE}/playground")
    page.get_by_label("Mensagem").fill("Quais são as boas práticas para revisar código?")
    page.get_by_role("button", name="Enviar").click()
    expect(page.locator(".gw-turn-assistant .gw-turn-meta")).to_be_visible(timeout=30000)
    page.get_by_label("Mensagem").fill("E para testes automatizados?")
    page.keyboard.press("Control+Enter")
    expect(page.locator(".gw-turn-assistant .gw-turn-meta")).to_have_count(2, timeout=30000)
    shot(page, "05-playground")

    # Revogar pelo console corta a chave na hora.
    page.goto(f"{BASE}/chaves")
    page.get_by_role("button", name=f"Ações da chave {name}").click()
    page.get_by_role("menuitem", name="Revogar").click()
    page.get_by_role("button", name="Revogar chave").click()
    expect(page.get_by_text("Chave revogada")).to_be_visible()
    gone = httpx.get(f"{BASE}/v1/models", headers=headers, timeout=30)
    assert gone.status_code == 401
    assert not errors


def test_admin_screens(browser, errors):
    page = new_page(browser, errors)
    login(page, "bruno")
    page.wait_for_timeout(800)
    shot(page, "03-visao-geral", full_page=True)

    page.goto(f"{BASE}/modelos")
    page.wait_for_load_state("networkidle")
    shot(page, "04-modelos", full_page=True)
    page.get_by_role("button", name="Ações do modelo gpt-4.1").click()
    page.get_by_role("menuitem", name="Editar").click()
    expect(page.get_by_role("dialog")).to_be_visible()
    shot(page, "06-modelo-editar")
    page.keyboard.press("Escape")

    page.goto(f"{BASE}/chaves")
    page.wait_for_load_state("networkidle")
    shot(page, "09-chaves", full_page=True)

    page.goto(f"{BASE}/uso")
    page.wait_for_load_state("networkidle")
    shot(page, "10-uso")
    page.locator("tbody tr").first.click()
    expect(page.get_by_role("dialog")).to_be_visible()
    shot(page, "11-uso-detalhe")
    page.keyboard.press("Escape")

    page.goto(f"{BASE}/como-usar")
    page.wait_for_load_state("networkidle")
    shot(page, "12-como-usar", full_page=True)

    page.goto(f"{BASE}/admin/usuarios")
    page.wait_for_load_state("networkidle")
    shot(page, "13-usuarios", full_page=True)
    page.get_by_text("Ana Souza").click()
    drawer = page.get_by_role("dialog")
    expect(drawer.get_by_text("Chaves de API")).to_be_visible()
    shot(page, "14-usuario-detalhe")
    page.keyboard.press("Escape")

    page.goto(f"{BASE}/admin/provedores")
    page.get_by_role("button", name="Testar conexão").click()
    expect(page.get_by_text("Conexão ok")).to_be_visible(timeout=15000)
    shot(page, "15-provedores", full_page=True)
    page.get_by_role("button", name="Novo provedor").click()
    expect(page.get_by_role("dialog")).to_be_visible()
    shot(page, "16-provedor-novo")
    page.keyboard.press("Escape")

    page.goto(f"{BASE}/admin/auditoria")
    row = page.locator("tbody tr").filter(has_text="Alterou modelo").first
    row.click()
    expect(page.get_by_text("Ocultar alterações")).to_be_visible()
    row.evaluate("el => el.scrollIntoView({block: 'center'})")
    shot(page, "17-auditoria")
    assert not errors


def test_phone_layout(browser, errors):
    page = new_page(browser, errors, phone=True)
    login(page, "bruno")
    page.wait_for_timeout(800)
    shot(page, "18-celular-visao-geral", full_page=True)
    wide = []
    for path in (
        "/",
        "/modelos",
        "/uso",
        "/chaves",
        "/playground",
        "/como-usar",
        "/admin/usuarios",
        "/admin/provedores",
        "/admin/auditoria",
    ):
        page.goto(f"{BASE}{path}")
        page.wait_for_load_state("networkidle")
        scroll, inner = page.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
        if scroll > inner:
            wide.append(f"{path} ({scroll}px > {inner}px)")
    assert not wide, f"rolagem horizontal no celular: {wide}"
    assert not errors
