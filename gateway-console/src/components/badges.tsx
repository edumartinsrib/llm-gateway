import { Badge, Tag, createStatusBadge } from 'broto-ui';
import type { Model, Provider } from '../api/types';

export const UserStatusBadge = createStatusBadge({
  active: { label: 'Ativo', tone: 'success' },
  blocked: { label: 'Bloqueado', tone: 'danger' },
  disabled: { label: 'Sem acesso no LDAP', tone: 'orange' },
});

export const KeyStatusBadge = createStatusBadge({
  active: { label: 'Ativa', tone: 'success' },
  revoked: { label: 'Revogada', tone: 'neutral' },
  expired: { label: 'Expirada', tone: 'pending' },
});

export const ProviderStatusBadge = createStatusBadge({
  ok: { label: 'Conectado', tone: 'live', pulse: true },
  error: { label: 'Com erro', tone: 'danger' },
  unknown: { label: 'Não verificado', tone: 'pending' },
  disabled: { label: 'Desabilitado', tone: 'neutral' },
});

export function providerStatus(p: Provider): 'ok' | 'error' | 'unknown' | 'disabled' {
  if (!p.enabled) return 'disabled';
  if (p.status === 'ok' || p.status === 'error') return p.status;
  return 'unknown';
}

export function ModelStatusBadge({ model }: { model: Model }) {
  if (!model.available) return <Badge tone="orange">Fora do provedor</Badge>;
  if (!model.enabled) return <Badge tone="neutral">Desabilitado</Badge>;
  if (!model.routable) return <Badge tone="danger">Provedor indisponível</Badge>;
  return <Badge tone="success">Habilitado</Badge>;
}

export function KindTag({ kind }: { kind: string }) {
  return kind === 'embeddings' ? (
    <Tag variant="outline">Embeddings</Tag>
  ) : (
    <Tag variant="green">Chat</Tag>
  );
}

export function HttpStatus({ code }: { code: number }) {
  const tone = code < 300 ? 'success' : code === 499 ? 'neutral' : code === 429 ? 'pending' : 'danger';
  return (
    <Badge tone={tone} dot={false} size="sm">
      <span className="bt-mono">{code}</span>
    </Badge>
  );
}
