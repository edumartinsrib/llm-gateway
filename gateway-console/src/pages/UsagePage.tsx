import {
  Button,
  Callout,
  Card,
  CellTitle,
  Chip,
  DataTable,
  Drawer,
  EmptyState,
  FilterBar,
  InlineCode,
  KeyValue,
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
import { ChartColumnBig, Download } from 'lucide-react';
import { useMemo, useState } from 'react';
import { useSearchParams } from 'react-router';
import { downloadFile, type Query } from '../api/client';
import { useModels, useUsage } from '../api/queries';
import type { UsageRow } from '../api/types';
import { HttpStatus } from '../components/badges';
import { useCurrentUser } from '../components/context';
import { formatMs, formatTokens, formatUsd } from '../lib/format';

const PERIODS: Record<string, { label: string; hours: number | null }> = {
  '24h': { label: 'Últimas 24 horas', hours: 24 },
  '7d': { label: 'Últimos 7 dias', hours: 24 * 7 },
  '30d': { label: 'Últimos 30 dias', hours: 24 * 30 },
  '90d': { label: 'Últimos 90 dias', hours: 24 * 90 },
  all: { label: 'Todo o histórico', hours: null },
};

const ENDPOINTS: Record<string, string> = {
  chat: 'chat/completions',
  embeddings: 'embeddings',
  responses: 'responses',
};

function UsageDetail({ row, onClose }: { row: UsageRow | null; onClose: () => void }) {
  if (!row) return null;
  return (
    <Drawer open onClose={onClose} title={row.model_name} description={formatDateTime(row.ts, { seconds: true })}>
      <div className="bt-stack">
        {row.error_message && (
          <Callout tone={row.status_code === 429 ? 'warning' : 'danger'} title={row.error_code ?? `HTTP ${row.status_code}`}>
            {row.error_message}
          </Callout>
        )}
        <KeyValue
          items={[
            ['Status', <HttpStatus key="s" code={row.status_code} />],
            ['Usuário', row.username],
            ['Chave', row.api_key_name ?? '—'],
            [
              'Rota',
              <InlineCode key="r">
                {row.api_key_id === null && row.api_key_name === 'console' ? '/api/v1/playground/' : '/v1/'}
                {ENDPOINTS[row.endpoint] ?? row.endpoint}
              </InlineCode>,
            ],
            ['Streaming', row.stream ? 'Sim' : 'Não'],
            ['Provedor', row.provider_name ? `${row.provider_name} · ${row.deployment}` : '—'],
            ['Cache do gateway', row.cache_hit ? <Tag key="c" variant="green">acerto</Tag> : 'Não'],
            ['Tokens de entrada', formatNumber(row.prompt_tokens)],
            ['  dos quais em cache no Azure', formatNumber(row.cached_tokens)],
            ['Tokens de saída', formatNumber(row.completion_tokens)],
            ['  dos quais de raciocínio', formatNumber(row.reasoning_tokens)],
            ['Custo', row.usage_estimated ? `≈ ${formatUsd(row.cost_usd)} (estimado: o stream terminou antes do Azure informar o uso)` : formatUsd(row.cost_usd)],
            { label: 'Economia (cache)', value: formatUsd(row.saved_usd), hidden: !row.cache_hit },
            ['Latência total', formatMs(row.latency_ms)],
            { label: 'Primeiro token', value: formatMs(row.ttft_ms), hidden: row.ttft_ms === null },
            ['Id da requisição', <span key="id" className="bt-mono">{row.request_id}</span>],
            ['Cliente', <span key="ua" className="bt-small">{[row.client_ip, row.user_agent].filter(Boolean).join(' · ') || '—'}</span>],
          ]}
        />
        <p className="bt-subtle bt-small">O gateway não guarda o conteúdo das mensagens, só os metadados da chamada.</p>
      </div>
    </Drawer>
  );
}

export function UsagePage() {
  const me = useCurrentUser();
  const [params, setParams] = useSearchParams();
  const [search, setSearch] = useState('');
  const q = useDebounced(search, 300);
  const [active, setActive] = useState<UsageRow | null>(null);
  const period = params.get('periodo') ?? '7d';
  const model = params.get('modelo') ?? '';
  const outcome = params.get('resultado') ?? '';
  const cache = params.get('cache') ?? '';
  const userId = params.get('usuario') ?? '';
  const offset = Number(params.get('offset') ?? 0);
  const limit = 50;
  const models = useModels(me.is_admin);

  const since = useMemo(() => {
    const hours = PERIODS[period]?.hours;
    return hours ? new Date(Date.now() - hours * 3_600_000).toISOString() : undefined;
    // Recalcula só quando o período muda (a lista atualiza sozinha a cada 15 s).
  }, [period]);

  const filters: Query = {
    since,
    model: model || undefined,
    outcome: outcome || undefined,
    cache: cache || undefined,
    user_id: userId || undefined,
    q: q || undefined,
  };
  const usage = useUsage({ ...filters, limit, offset });

  const update = (changes: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(changes)) {
      if (v) next.set(k, v);
      else next.delete(k);
    }
    if (!('offset' in changes)) next.delete('offset');
    setParams(next, { replace: true });
  };

  const columns: Column<UsageRow>[] = [
    { key: 'ts', header: 'Quando', nowrap: true, cell: (r) => formatDateTime(r.ts, { seconds: true }) },
    { key: 'user', header: 'Usuário', hidden: !me.is_admin, cell: (r) => <CellTitle title={r.username} sub={r.api_key_name ?? undefined} /> },
    { key: 'key', header: 'Chave', hidden: me.is_admin, cell: (r) => r.api_key_name ?? '—' },
    {
      key: 'model',
      header: 'Modelo',
      cell: (r) => (
        <div className="bt-row-wrap">
          <span className="bt-mono">{r.model_name}</span>
          {r.endpoint !== 'chat' && <Tag variant="outline">{r.endpoint}</Tag>}
          {r.stream && <Tag>stream</Tag>}
        </div>
      ),
    },
    { key: 'status', header: 'Status', cell: (r) => <HttpStatus code={r.status_code} /> },
    {
      key: 'tokens',
      header: 'Tokens (entrada / saída)',
      align: 'right',
      nowrap: true,
      cell: (r) => (
        <span className="bt-num">
          {formatTokens(r.prompt_tokens)} <span className="bt-subtle">/</span> {formatTokens(r.completion_tokens)}
        </span>
      ),
    },
    {
      key: 'cost',
      header: 'Custo',
      align: 'right',
      nowrap: true,
      cell: (r) =>
        r.cache_hit ? (
          <Tag variant="green">cache · {formatUsd(r.saved_usd)}</Tag>
        ) : (
          <span className="bt-num" title={r.usage_estimated ? 'Estimado: o stream terminou antes do Azure informar o uso' : undefined}>
            {r.usage_estimated ? '≈ ' : ''}
            {formatUsd(r.cost_usd)}
          </span>
        ),
    },
    { key: 'latency', header: 'Latência', align: 'right', nowrap: true, cell: (r) => <span className="bt-num">{formatMs(r.latency_ms)}</span> },
  ];

  const total = usage.data?.total ?? 0;
  const pageCost = usage.data?.items.reduce((acc, r) => acc + r.cost_usd, 0) ?? 0;

  return (
    <Page>
      <PageHeader
        crumbs={[{ label: 'Início', href: '/' }, { label: 'Uso e custos' }]}
        title="Uso e custos"
        description={
          me.is_admin
            ? 'Todas as chamadas feitas pelo gateway, com tokens, custo e latência. O conteúdo das mensagens não é guardado.'
            : 'As suas chamadas ao gateway, com tokens, custo e latência. O conteúdo das mensagens não é guardado.'
        }
        actions={
          <Button
            variant="secondary"
            icon={<Download size={16} />}
            onClick={() => void downloadFile('/usage/export', filters, 'uso-gateway.csv')}
          >
            Exportar CSV
          </Button>
        }
      />

      <Card flush>
        <FilterBar>
          {me.is_admin && (
            <SearchInput
              controlSize="sm"
              placeholder="Usuário ou id da requisição"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              aria-label="Buscar por usuário ou id"
            />
          )}
          <Select controlSize="sm" aria-label="Período" value={period} onChange={(e) => update({ periodo: e.target.value })}>
            {Object.entries(PERIODS).map(([k, v]) => (
              <option key={k} value={k}>
                {v.label}
              </option>
            ))}
          </Select>
          <Select controlSize="sm" aria-label="Modelo" value={model} onChange={(e) => update({ modelo: e.target.value })}>
            <option value="">Todos os modelos</option>
            {models.data?.map((m) => (
              <option key={m.id} value={m.name}>
                {m.name}
              </option>
            ))}
          </Select>
          <Chip pressed={outcome === 'error'} tone="danger" onPressedChange={(p) => update({ resultado: p ? 'error' : null })}>
            Com erro
          </Chip>
          <Chip pressed={cache === 'hit'} tone="success" onPressedChange={(p) => update({ cache: p ? 'hit' : null })}>
            Do cache
          </Chip>
          {userId && (
            <Chip pressed onPressedChange={() => update({ usuario: null })}>
              Só um usuário ✕
            </Chip>
          )}
          <span className="bt-grow" />
          <span className="bt-muted bt-small">
            {formatNumber(total)} chamada{total === 1 ? '' : 's'} · {formatUsd(pageCost)} nesta página
          </span>
        </FilterBar>
        <DataTable
          columns={columns}
          rows={usage.data?.items}
          loading={usage.isPending}
          getRowId={(r) => String(r.id)}
          onRowClick={setActive}
          activeRowId={active ? String(active.id) : null}
          empty={
            <EmptyState
              compact
              icon={<ChartColumnBig size={22} />}
              title="Nenhuma chamada encontrada"
              description="Mude o período ou os filtros."
            />
          }
          footer={
            total > limit ? (
              <Pagination total={total} limit={limit} offset={offset} onChange={(o) => update({ offset: String(o) })} noun="chamadas" />
            ) : undefined
          }
          aria-label="Chamadas"
        />
      </Card>
      <UsageDetail row={active} onClose={() => setActive(null)} />
    </Page>
  );
}
