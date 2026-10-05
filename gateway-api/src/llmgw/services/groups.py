"""Comparação de grupos do LDAP.

Cada grupo configurado (administradores, acesso ao gateway, acesso a um modelo) pode ser:

- um **DN completo** (`CN=LLM Users,OU=Grupos,DC=corp,DC=local`): compara com o DN
  completo dos grupos do usuário. É o formato recomendado em produção, porque dois
  grupos com o mesmo nome em OUs diferentes não se confundem;
- só o **nome** (`llm-users`): compara com o CN dos grupos do usuário. Prático, mas
  qualquer grupo com esse nome, em qualquer OU, concede o acesso.

Maiúsculas, minúsculas e espaços em volta de `,` e `=` não importam.
"""

from __future__ import annotations

from collections.abc import Iterable


def is_dn(value: str) -> bool:
    text = value.strip()
    return "=" in text and "," in text


def normalize_dn(value: str) -> str:
    try:
        from ldap3.utils.dn import parse_dn

        parts = parse_dn(value.strip())
        return ",".join(f"{attr.strip().lower()}={val.strip().lower()}" for attr, val, _ in parts)
    except Exception:  # noqa: BLE001 - DN fora do padrão: comparação textual simples
        return ",".join(p.strip().lower() for p in value.split(","))


def group_key(value: str) -> str:
    """Nome do grupo para comparação: `CN=LLM Users,OU=…` → `llm users`."""
    text = value.strip()
    if "=" in text:
        first = text.split(",", 1)[0]
        _, _, rdn_value = first.partition("=")
        text = rdn_value
    return " ".join(text.replace("\\,", ",").split()).lower()


def group_keys(values: Iterable[str]) -> set[str]:
    return {group_key(v) for v in values if v and v.strip()}


class GroupRule:
    """Lista de grupos exigidos, pronta para comparar (pré-calculada uma vez)."""

    def __init__(self, wanted: Iterable[str]):
        values = [w for w in wanted if w and w.strip()]
        self.dns = frozenset(normalize_dn(w) for w in values if is_dn(w))
        self.names = frozenset(group_key(w) for w in values if not is_dn(w))

    def __bool__(self) -> bool:
        return bool(self.dns or self.names)

    def matches(self, names: Iterable[str], dns: Iterable[str] = ()) -> bool:
        if self.names and self.names & group_keys(names):
            return True
        if self.dns and self.dns & {normalize_dn(d) for d in dns if d}:
            return True
        return False


def intersects(names: Iterable[str], wanted: Iterable[str], dns: Iterable[str] = ()) -> bool:
    return GroupRule(wanted).matches(names, dns)
