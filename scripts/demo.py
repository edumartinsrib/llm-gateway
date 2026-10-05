"""Popula o gateway com dados de demonstração, fazendo chamadas reais pela API.

1. entra como administrador, sincroniza os provedores e habilita os modelos com
   preços DE EXEMPLO (não são os preços do Azure; ajuste no console);
2. entra como cada usuário, cria uma chave e faz chamadas variadas com o SDK da
   OpenAI: chat, streaming, embeddings, Responses API, repetições (cache) e erros;
3. com `--espalhar-dias N`, redistribui o horário dessas chamadas pelos últimos N
   dias direto no Postgres e recalcula os contadores mensais (só para o gráfico da
   visão geral ficar com cara de uso real; não use em produção).

    uv run python scripts/demo.py                                   # compose (modo LDAP)
    uv run python scripts/demo.py --url http://localhost:8000 --dev # API direta, modo dev
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import time

import httpx
from openai import APIStatusError, OpenAI

# Preços de exemplo em US$ por 1 milhão de tokens (entrada, entrada em cache, saída).
EXAMPLE_PRICES = {
    "gpt-4o-mini": (0.15, 0.075, 0.60, 128_000, 16_384, "Rápido e barato para tarefas simples"),
    "gpt-4.1": (2.00, 0.50, 8.00, 1_047_576, 32_768, "Uso geral, contexto longo"),
    "o4-mini": (1.10, 0.275, 4.40, 200_000, 100_000, "Raciocínio (código, análise)"),
    "text-embedding-3-small": (0.02, None, 0.0, 8_191, None, "Embeddings para busca semântica"),
}

PROMPTS = [
    "Resuma em três tópicos o relatório trimestral de vendas",
    "Explique a diferença entre juros simples e compostos",
    "Escreva uma consulta SQL que some as vendas por mês",
    "Traduza para o inglês: o pagamento foi aprovado",
    "Quais são os riscos de um projeto sem testes automatizados?",
    "Sugira nomes para uma ferramenta interna de relatórios",
    "Revise este parágrafo para deixá-lo mais claro",
    "Liste boas práticas para revisar pull requests",
]


def login(base: str, username: str, password: str | None) -> httpx.Client:
    r = httpx.post(f"{base}/api/v1/auth/login", json={"username": username, "password": password})
    r.raise_for_status()
    return httpx.Client(
        base_url=base, headers={"Authorization": f"Bearer {r.json()['token']}"}, timeout=60
    )


def setup_catalog(admin: httpx.Client) -> None:
    for provider in admin.get("/api/v1/providers").json():
        if provider["enabled"] and provider.get("discovery") not in (None, "none"):
            r = admin.post(f"/api/v1/providers/{provider['id']}/sync")
            print(f"provedor {provider['name']}: {r.json()}")
    models = {m["name"]: m for m in admin.get("/api/v1/models", params={"all": True}).json()}
    for name, (inp, cached, out, ctx, max_out, desc) in EXAMPLE_PRICES.items():
        model = models.get(name)
        if not model:
            continue
        admin.patch(
            f"/api/v1/models/{model['id']}",
            json={
                "enabled": True,
                "input_price": inp,
                "cached_input_price": cached,
                "output_price": out,
                "context_window": ctx,
                "max_output_tokens": max_out,
                "description": desc,
            },
        ).raise_for_status()
        print(f"modelo {name} habilitado com preço de exemplo")


def new_key(console: httpx.Client, name: str) -> str:
    r = console.post("/api/v1/keys", json={"name": name, "expires_in_days": 90})
    r.raise_for_status()
    return r.json()["key"]


def traffic(base: str, key: str, calls: int, rng: random.Random) -> None:
    client = OpenAI(api_key=key, base_url=f"{base}/v1", max_retries=0)
    chat_models = [m for m in ("gpt-4o-mini", "gpt-4.1", "o4-mini") if m in EXAMPLE_PRICES]
    for i in range(calls):
        kind = rng.random()
        model = rng.choices(chat_models, weights=[5, 3, 2])[0]
        prompt = rng.choice(PROMPTS) + (
            "" if rng.random() < 0.3 else f" (caso {rng.randint(1, 500)})"
        )
        try:
            if kind < 0.12:
                client.embeddings.create(
                    model="text-embedding-3-small", input=[prompt, prompt[::-1]]
                )
            elif kind < 0.2:
                client.responses.create(model=model, input=prompt)
            elif kind < 0.55:
                for _ in client.chat.completions.create(
                    model=model, messages=[{"role": "user", "content": prompt}], stream=True
                ):
                    pass
            elif kind < 0.6:
                client.chat.completions.create(
                    model=model, messages=[{"role": "user", "content": f"{prompt} #filtro"}]
                )
            else:
                client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": "Responda em português. #cache-prompt"},
                        {"role": "user", "content": prompt},
                    ],
                )
        except APIStatusError as exc:
            print(f"  chamada {i}: HTTP {exc.status_code} ({exc.message[:60]})")


async def spread(database_url: str, days: int, rng: random.Random) -> None:
    import asyncpg

    url = database_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(url)
    try:
        rows = await conn.fetch("SELECT id FROM usage_log")
        for row in rows:
            back = rng.random() ** 1.6 * days
            await conn.execute(
                "UPDATE usage_log SET ts = now() - make_interval(secs => $2) WHERE id = $1",
                row["id"],
                back * 86400,
            )
        # Recalcula os contadores mensais a partir do histórico redistribuído.
        await conn.execute("DELETE FROM spend_counters")
        tz = "America/Sao_Paulo"
        for subject_sql, where in (
            ("'user:' || user_id::text", "user_id IS NOT NULL"),
            ("'key:' || api_key_id::text", "api_key_id IS NOT NULL"),
            ("'global'", "true"),
        ):
            await conn.execute(
                f"""
                INSERT INTO spend_counters (subject, period, cost_usd, requests, tokens, cache_hits,
                                            saved_usd, updated_at)
                SELECT {subject_sql}, to_char(ts AT TIME ZONE '{tz}', 'YYYY-MM'),
                       sum(cost_usd), count(*), sum(total_tokens),
                       sum(cache_hit::int), sum(saved_usd), now()
                FROM usage_log WHERE {where}
                GROUP BY 1, 2
                """
            )
        print(f"{len(rows)} chamadas redistribuídas nos últimos {days} dias")
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", default="http://localhost:8090", help="console/API do gateway")
    parser.add_argument("--dev", action="store_true", help="gateway em modo dev (sem senha)")
    parser.add_argument("--admin", default="bruno", help="administrador (no modo dev: admin)")
    parser.add_argument(
        "--senha", default="Senha@123", help="senha dos usuários de exemplo do LDAP"
    )
    parser.add_argument("--usuarios", default="ana,diego,bruno", help="usuários que fazem chamadas")
    parser.add_argument("--chamadas", type=int, default=40, help="chamadas por usuário")
    parser.add_argument("--espalhar-dias", type=int, default=0)
    parser.add_argument(
        "--database-url",
        default="postgresql://llmgw:llmgw@localhost:55432/llmgw",
        help="Postgres do compose (só para --espalhar-dias)",
    )
    parser.add_argument("--semente", type=int, default=7)
    args = parser.parse_args()

    rng = random.Random(args.semente)
    base = args.url.rstrip("/")
    password = None if args.dev else args.senha
    admin = login(base, "admin" if args.dev and args.admin == "bruno" else args.admin, password)
    setup_catalog(admin)
    for username in [u.strip() for u in args.usuarios.split(",") if u.strip()]:
        console = login(base, username, password)
        key = new_key(console, rng.choice(["notebook", "pipeline-relatorios", "pi", "assistente"]))
        print(f"{username}: chave criada; fazendo {args.chamadas} chamadas")
        traffic(base, key, args.chamadas, rng)
    if args.espalhar_dias > 0:
        time.sleep(2)  # o uso é gravado em segundo plano
        asyncio.run(spread(args.database_url, args.espalhar_dias, rng))
    return 0


if __name__ == "__main__":
    sys.exit(main())
