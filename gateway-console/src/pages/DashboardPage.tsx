import {
  Card,
  CellTitle,
  DataTable,
  EmptyState,
  Grid,
  Hero,
  Kpi,
  Legend,
  LinkButton,
  MetricList,
  Page,
  ProgressBar,
  RelativeTime,
  Segmented,
  StackedBarChart,
  Tag,
  formatNumber,
  type Column,
} from 'broto-ui';
import { Activity, Coins, DatabaseZap, KeyRound, MessagesSquare, Sparkles, Timer, TriangleAlert } from 'lucide-react';
import { useMemo, useState } from 'react';
import { useSummary, useUsage } from '../api/queries';
import type { Summary, UsageRow } from '../api/types';
import { HttpStatus } from '../components/badges';
import { useCurrentUser } from '../components/context';
import {
  budgetPct,
  firstName,
  formatDay,
  formatMs,
  formatPct,
  formatPeriod,
  formatTokens,
  formatUsd,
} from '../lib/format';

const SERIES_COLORS = [
  'var(--bt-green-700)',
  'var(--bt-green-500)',
  'var(--bt-green-200)',
  'var(--bt-blue-500)',
  'var(--bt-yellow-500)',
  'var(--bt-neutral-400)',
];

function dayKeys(days: number): string[] {
  const out: string[] = [];
  const now = new Date();
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(now.getTime() - i * 86_400_000);
    const parts = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/Sao_Paulo' }).format(d);
    out.push(parts);
  }
  return out;
}

function chartData(summary: Summary) {
  const ranked = summary.by_model.map((m) => m.key);
  const top = ranked.slice(0, 5);
  const series = top.map((key, i) => ({ key, label: key, color: SERIES_COLORS[i] }));
  if (ranked.length > 5) series.push({ key: '__outros', label: 'Outros', color: SERIES_COLORS[5] });
  const byDay = new Map<string, Record<string, number>>();
  for (const point of summary.by_day) {
    const key = top.includes(point.model) ? point.model : '__outros';
    const values = byDay.get(point.day) ?? {};
    values[key] = (values[key] ?? 0) + point.cost_usd;
    byDay.set(point.day, values);
  }
  // O gráfico arredonda o eixo para no mínimo 4 unidades: valores em dólar abaixo
  // disso são multiplicados (centavos, décimos de centavo…) e os rótulos convertem de volta.
  const maxDay = Math.max(0, ...[...byDay.values()].map((v) => Object.values(v).reduce((a, b) => a + b, 0)));
  let scale = 1;
  while (maxDay > 0 && maxDay * scale < 4 && scale < 1e9) scale *= 10;
  const data = dayKeys(summary.days).map((day) => ({
    label: formatDay(day),
    title: new Date(`${day}T12:00:00`).toLocaleDateString('pt-BR', { weekday: 'short', day: '2-digit', month: '2-digit' }),
    values: Object.fromEntries(series.map((s) => [s.key, (byDay.get(day)?.[s.key] ?? 0) * scale])),
  }));
  return { data, series, format: (v: number) => formatUsd(v / scale) };
}

const recentColumns: Column<UsageRow>[] = [
  { key: 'model', header: 'Modelo', cell: (r) => <CellTitle title={r.model_name} sub={r.api_key_name ?? undefined} /> },
  { key: 'user', header: 'Usuário', cell: (r) => r.username },
  { key: 'status', header: 'Status', cell: (r) => <HttpStatus code={r.status_code} /> },
  { key: 'tokens', header: 'Tokens', align: 'right', cell: (r) => <span className="bt-num">{formatTokens(r.total_tokens)}</span> },
  {
    key: 'cost',
    header: 'Custo',
    align: 'right',
    cell: (r) => (r.cache_hit ? <Tag variant="green">cache</Tag> : <span className="bt-num">{formatUsd(r.cost_usd)}</span>),
  },
  { key: 'when', header: 'Quando', align: 'right', nowrap: true, cell: (r) => <RelativeTime value={r.ts} /> },
];

export function DashboardPage() {
  const me = useCurrentUser();
  const [days, setDays] = useState<'7' | '30' | '90'>('30');
  const [scope, setScope] = useState<'me' | 'all'>(me.is_admin ? 'all' : 'me');
  const query = { days: Number(days), mine: scope === 'me' ? true : undefined };
  const summary = useSummary(query);
  const recent = useUsage({ limit: 6, user_id: scope === 'me' ? me.id : undefined });
  const s = summary.data;
  const chart = useMemo(() => (s ? chartData(s) : null), [s]);

  const month = s?.month;
  const pct = month ? budgetPct(month.spent_usd, month.budget_usd) : null;
  const hitRate = s && s.totals.requests ? (s.totals.cache_hits / s.totals.requests) * 100 : null;
  const errorRate = s && s.totals.requests ? (s.totals.errors / s.totals.requests) * 100 : null;
  const maxModelCost = Math.max(...(s?.by_model.map((m) => m.cost_usd) ?? [0]), 0.000001);
  const maxUserCost = Math.max(...(s?.by_user.map((m) => m.cost_usd) ?? [0]), 0.000001);

  return (
    <Page>
      <Hero
        eyebrow="Gateway LLM"
        title={`Olá, ${firstName(me.display_name, me.username)}`}
        description="Uso, custo e cache das chamadas aos modelos do Foundry feitas pelo gateway."
        actions={
          <>
            <LinkButton href="/chaves" variant="hero" icon={<KeyRound size={16} />}>
              Minhas chaves
            </LinkButton>
            <LinkButton href="/playground" variant="hero" icon={<MessagesSquare size={16} />}>
              Testar um modelo
            </LinkButton>
          </>
        }
      />

      <div className="bt-row-between gw-toolbar">
        {me.is_admin ? (
          <Segmented
            aria-label="Escopo"
            value={scope}
            onChange={setScope}
            options={[
              { value: 'all', label: 'Gateway inteiro' },
              { value: 'me', label: 'Meu uso' },
            ]}
          />
        ) : (
          <span className="bt-muted">Seu uso</span>
        )}
        <Segmented
          aria-label="Período"
          value={days}
          onChange={setDays}
          options={[
            { value: '7', label: '7 dias' },
            { value: '30', label: '30 dias' },
            { value: '90', label: '90 dias' },
          ]}
        />
      </div>

      <Grid layout="kpi-row" cols={6}>
        <Kpi
          variant={pct !== null && pct >= 100 ? 'danger' : 'featured'}
          icon={<Coins size={18} />}
          loading={summary.isPending}
          label={month ? `Gasto em ${formatPeriod(month.period).split(' de ')[0]}` : 'Gasto no mês'}
          value={month ? formatUsd(month.spent_usd) : '—'}
          unit={month?.budget_usd != null ? `de ${formatUsd(month.budget_usd)}` : undefined}
          foot={
            pct !== null ? (
              <ProgressBar value={pct} tone={pct >= 100 ? 'danger' : pct >= 80 ? 'warning' : 'default'} label="Orçamento do mês" />
            ) : scope === 'all' ? (
              'Gateway sem limite mensal'
            ) : (
              'Sem limite mensal definido'
            )
          }
        />
        <Kpi
          icon={<Activity size={18} />}
          loading={summary.isPending}
          label="Chamadas"
          value={formatNumber(s?.totals.requests ?? 0)}
          foot={[`últimos ${days} dias`, `${formatNumber(Math.round((s?.totals.requests ?? 0) / Number(days)))} por dia`]}
          href="/uso"
        />
        <Kpi
          variant={errorRate !== null && errorRate >= 5 ? 'danger' : 'default'}
          icon={<TriangleAlert size={18} />}
          loading={summary.isPending}
          label="Chamadas com erro"
          value={errorRate === null ? '—' : formatPct(errorRate, 1)}
          foot={`${formatNumber(s?.totals.errors ?? 0)} no período (filtro, limite, provedor)`}
          href="/uso?resultado=error"
        />
        <Kpi
          icon={<Sparkles size={18} />}
          loading={summary.isPending}
          label="Tokens"
          value={formatTokens(s?.totals.total_tokens ?? 0)}
          foot={[`${formatTokens(s?.totals.prompt_tokens ?? 0)} entrada`, `${formatTokens(s?.totals.completion_tokens ?? 0)} saída`]}
        />
        <Kpi
          variant={hitRate ? 'success' : 'default'}
          icon={<DatabaseZap size={18} />}
          loading={summary.isPending}
          label="Respostas do cache"
          value={hitRate === null ? '—' : formatPct(hitRate, 1)}
          foot={`economia de ${formatUsd(s?.totals.saved_usd ?? 0)}`}
        />
        <Kpi
          icon={<Timer size={18} />}
          loading={summary.isPending}
          label="Latência p95"
          value={formatMs(s?.totals.p95_latency_ms)}
          foot={`média ${formatMs(s?.totals.avg_latency_ms)}`}
        />
      </Grid>

      {s && s.totals.requests === 0 ? (
        <Card>
          <EmptyState
            icon={<KeyRound size={24} />}
            title="Nenhuma chamada no período"
            description="Crie uma chave de API e aponte o seu cliente OpenAI para o gateway. As chamadas aparecem aqui com custo e tokens."
            action={
              <div className="bt-row-wrap">
                <LinkButton href="/chaves" variant="primary" icon={<KeyRound size={16} />}>
                  Criar chave
                </LinkButton>
                <LinkButton href="/como-usar" variant="secondary">
                  Como usar
                </LinkButton>
              </div>
            }
          />
        </Card>
      ) : (
        <Grid layout="main-side">
          <Card
            title="Custo por dia"
            subtitle={`Por modelo, últimos ${days} dias (US$)`}
          >
            {chart && (
              <div className="bt-stack-sm">
                <Legend items={chart.series.map((x) => ({ label: x.label, color: x.color }))} />
                <StackedBarChart
                aria-label={`Custo por dia nos últimos ${days} dias`}
                data={chart.data}
                series={chart.series}
                formatValue={chart.format}
                labelEvery={days === '7' ? 1 : days === '30' ? 5 : 15}
                />
              </div>
            )}
          </Card>
          <Card title="Modelos mais usados" subtitle="Por custo no período">
            {s && s.by_model.length ? (
              <MetricList
                items={s.by_model.slice(0, 6).map((m) => ({
                  key: m.key,
                  title: <span className="bt-mono">{m.label}</span>,
                  sub: `${formatNumber(m.requests)} chamadas · ${formatTokens(m.tokens)} tokens`,
                  value: m.cost_usd,
                  display: formatUsd(m.cost_usd),
                  progress: (m.cost_usd / maxModelCost) * 100,
                }))}
              />
            ) : (
              <EmptyState compact title="Sem dados" />
            )}
          </Card>
        </Grid>
      )}

      {(() => {
        const recentCard = (
          <Card
            title="Últimas chamadas"
            subtitle="Atualiza a cada 15 segundos"
            actions={
              <LinkButton href="/uso" variant="ghost" size="sm">
                Ver todas
              </LinkButton>
            }
            flush
          >
            <DataTable
              columns={recentColumns.filter((c) => c.key !== 'user' || scope === 'all')}
              rows={recent.data?.items}
              loading={recent.isPending}
              getRowId={(r) => String(r.id)}
              empty={<EmptyState compact title="Nenhuma chamada ainda" />}
              aria-label="Últimas chamadas"
            />
          </Card>
        );
        if (!(scope === 'all' && s?.by_user.length)) return recentCard;
        return (
          <Grid layout="main-side">
            {recentCard}
            <Card title="Quem mais gastou" subtitle="Por custo no período">
              <MetricList
                items={s.by_user.slice(0, 6).map((u) => ({
                  key: u.key,
                  title: u.label,
                  sub: `${formatNumber(u.requests)} chamadas`,
                  value: u.cost_usd,
                  display: formatUsd(u.cost_usd),
                  progress: (u.cost_usd / maxUserCost) * 100,
                }))}
              />
            </Card>
          </Grid>
        );
      })()}
    </Page>
  );
}
