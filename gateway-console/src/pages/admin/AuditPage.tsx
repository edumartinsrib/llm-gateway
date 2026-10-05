import {
  Card,
  CellTitle,
  DataTable,
  EmptyState,
  FilterBar,
  JsonView,
  Page,
  PageHeader,
  Pagination,
  SearchInput,
  Select,
  Tag,
  formatDateTime,
  formatNumber,
  useDebounced,
  type Column,
} from 'broto-ui';
import { ScrollText } from 'lucide-react';
import { useState } from 'react';
import { useAudit } from '../../api/queries';
import type { AuditEntry } from '../../api/types';
import { AdminOnly } from '../../components/AdminOnly';

const ACTIONS: Record<string, string> = {
  '': 'Todas as ações',
  'auth.': 'Logins',
  'key.': 'Chaves de API',
  'user.': 'Usuários',
  'model.': 'Modelos',
  'provider.': 'Provedores',
  'budget.': 'Alertas de orçamento',
  'cache.': 'Cache',
};

const LABELS: Record<string, string> = {
  'auth.login': 'Entrou no console',
  'key.create': 'Criou chave',
  'key.update': 'Alterou chave',
  'key.revoke': 'Revogou chave',
  'user.create': 'Pré-cadastrou usuário',
  'user.update': 'Alterou usuário',
  'user.disabled': 'Usuário perdeu acesso (LDAP)',
  'user.reactivated': 'Usuário voltou a ter acesso',
  'model.create': 'Cadastrou modelo',
  'model.update': 'Alterou modelo',
  'model.delete': 'Excluiu modelo',
  'provider.create': 'Cadastrou provedor',
  'provider.update': 'Alterou provedor',
  'provider.delete': 'Excluiu provedor',
  'provider.secret_update': 'Atualizou credencial no Vault',
  'provider.secret_reloaded': 'Credencial rotacionada no Vault',
  'provider.sync': 'Sincronizou deployments',
  'budget.alert': 'Alerta de orçamento',
  'cache.clear': 'Limpou o cache',
};

function diff(before: Record<string, unknown> | null, after: Record<string, unknown> | null) {
  if (!before || !after) return after ?? before;
  const out: Record<string, { antes: unknown; depois: unknown }> = {};
  for (const key of new Set([...Object.keys(before), ...Object.keys(after)])) {
    if (JSON.stringify(before[key]) !== JSON.stringify(after[key])) out[key] = { antes: before[key], depois: after[key] };
  }
  return out;
}

function AuditContent() {
  const [action, setAction] = useState('');
  const [search, setSearch] = useState('');
  const q = useDebounced(search, 300);
  const [offset, setOffset] = useState(0);
  const [open, setOpen] = useState<number | null>(null);
  const limit = 50;
  const audit = useAudit({ action: action || undefined, q: q || undefined, limit, offset });

  const columns: Column<AuditEntry>[] = [
    { key: 'ts', header: 'Quando', nowrap: true, cell: (a) => formatDateTime(a.ts, { seconds: true }) },
    {
      key: 'actor',
      header: 'Quem',
      cell: (a) => (a.actor_type === 'system' ? <Tag>sistema</Tag> : <span className="bt-strong">{a.actor}</span>),
    },
    { key: 'action', header: 'Ação', cell: (a) => <CellTitle title={LABELS[a.action] ?? a.action} sub={<span className="bt-mono">{a.action}</span>} /> },
    {
      key: 'detail',
      header: 'Detalhe',
      cell: (a) => (
        <div className="bt-stack-sm">
          <span className="bt-line-clamp">{a.detail ?? '—'}</span>
          {(a.before || a.after) && <span className="bt-small gw-link">{open === a.id ? 'Ocultar alterações' : 'Ver alterações'}</span>}
        </div>
      ),
    },
  ];

  const total = audit.data?.total ?? 0;
  return (
    <Page>
      <PageHeader
        crumbs={[{ label: 'Início', href: '/' }, { label: 'Administração' }, { label: 'Auditoria' }]}
        title="Auditoria"
        description="Toda mudança feita no console ou pelo próprio gateway (sincronização, LDAP, orçamento). Credenciais nunca aparecem aqui."
      />
      <Card flush>
        <FilterBar>
          <SearchInput
            controlSize="sm"
            placeholder="Usuário ou texto do detalhe"
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setOffset(0);
            }}
            aria-label="Buscar na auditoria"
          />
          <Select
            controlSize="sm"
            aria-label="Ação"
            value={action}
            onChange={(e) => {
              setAction(e.target.value);
              setOffset(0);
            }}
          >
            {Object.entries(ACTIONS).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </Select>
          <span className="bt-grow" />
          <span className="bt-muted bt-small">{formatNumber(total)} registro(s)</span>
        </FilterBar>
        <DataTable
          columns={columns}
          rows={audit.data?.items}
          loading={audit.isPending}
          getRowId={(a) => String(a.id)}
          onRowClick={(a) => setOpen(open === a.id ? null : a.id)}
          activeRowId={open !== null ? String(open) : null}
          renderExpanded={(a) => (open === a.id && (a.before || a.after) ? <JsonView value={diff(a.before, a.after)} /> : null)}
          empty={<EmptyState compact icon={<ScrollText size={22} />} title="Nada registrado com esses filtros" />}
          footer={total > limit ? <Pagination total={total} limit={limit} offset={offset} onChange={setOffset} noun="registros" /> : undefined}
          aria-label="Auditoria"
        />
      </Card>
    </Page>
  );
}

export function AuditPage() {
  return (
    <AdminOnly>
      <AuditContent />
    </AdminOnly>
  );
}
