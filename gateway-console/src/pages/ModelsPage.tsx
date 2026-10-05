import {
  Button,
  Callout,
  Card,
  CellTitle,
  Chip,
  DataTable,
  Dialog,
  EmptyState,
  FilterBar,
  Field,
  FormGrid,
  FormSection,
  Input,
  Menu,
  Page,
  PageHeader,
  SearchInput,
  Segmented,
  Select,
  Switch,
  Tag,
  TagInput,
  Textarea,
  formatNumber,
  toast,
  useConfirm,
  useDebounced,
  type Column,
} from 'broto-ui';
import { Boxes, MessagesSquare, Pencil, Plus, Power, RefreshCw, Trash2 } from 'lucide-react';
import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router';
import {
  useClearCache,
  useDeleteModel,
  useModels,
  useProviders,
  useSaveModel,
  useSyncProvider,
  type ModelInput,
} from '../api/queries';
import type { Model } from '../api/types';
import { KindTag, ModelStatusBadge } from '../components/badges';
import { useConfig, useCurrentUser } from '../components/context';
import { formatPrice, formatTtl } from '../lib/format';

// ---------------------------------------------------------------- edição (administrador)
interface ModelForm {
  name: string;
  provider_id: string;
  deployment: string;
  kind: 'chat' | 'embeddings';
  enabled: boolean;
  description: string;
  input_price: string;
  cached_input_price: string;
  output_price: string;
  cache_mode: 'default' | 'off' | 'custom';
  cache_ttl: string;
  allowed_groups: string[];
  context_window: string;
  max_output_tokens: string;
  supports_responses: boolean;
  supports_vision: boolean;
  supports_reasoning: boolean;
}

function toForm(m?: Model | null): ModelForm {
  return {
    name: m?.name ?? '',
    provider_id: m?.provider_id ?? '',
    deployment: m?.deployment ?? '',
    kind: m?.kind === 'embeddings' ? 'embeddings' : 'chat',
    enabled: m?.enabled ?? true,
    description: m?.description ?? '',
    input_price: m ? String(m.input_price) : '',
    cached_input_price: m?.cached_input_price != null ? String(m.cached_input_price) : '',
    output_price: m ? String(m.output_price) : '',
    cache_mode: m?.cache_ttl_s == null ? 'default' : m.cache_ttl_s === 0 ? 'off' : 'custom',
    cache_ttl: m?.cache_ttl_s ? String(m.cache_ttl_s) : '3600',
    allowed_groups: m?.allowed_groups ?? [],
    context_window: m?.context_window ? String(m.context_window) : '',
    max_output_tokens: m?.max_output_tokens ? String(m.max_output_tokens) : '',
    supports_responses: m?.supports_responses ?? true,
    supports_vision: m?.supports_vision ?? false,
    supports_reasoning: m?.supports_reasoning ?? false,
  };
}

function num(value: string): number | null {
  const n = Number(value.replace(',', '.'));
  return value.trim() === '' || Number.isNaN(n) ? null : n;
}

function ModelDialog({ model, onClose }: { model: Model | null; onClose: () => void }) {
  const config = useConfig();
  const [form, setForm] = useState<ModelForm>(() => toForm(model));
  const [touched, setTouched] = useState(false);
  const providers = useProviders();
  const save = useSaveModel();
  const set = <K extends keyof ModelForm>(k: K, v: ModelForm[K]) => setForm((f) => ({ ...f, [k]: v }));
  const creating = !model;
  const missingPrice = form.enabled && !num(form.input_price) && !num(form.output_price);
  const defaultTtl = form.kind === 'embeddings' ? config.cache.embeddings_ttl_s : config.cache.chat_ttl_s;

  const submit = async () => {
    setTouched(true);
    if (creating && (!form.provider_id || !form.deployment.trim())) return;
    const body: ModelInput = {
      name: form.name.trim() || undefined,
      kind: form.kind,
      enabled: form.enabled,
      description: form.description.trim() || null,
      input_price: num(form.input_price) ?? 0,
      cached_input_price: num(form.cached_input_price),
      output_price: num(form.output_price) ?? 0,
      cache_ttl_s: form.cache_mode === 'default' ? null : form.cache_mode === 'off' ? 0 : (num(form.cache_ttl) ?? 0),
      allowed_groups: form.allowed_groups,
      context_window: num(form.context_window),
      max_output_tokens: num(form.max_output_tokens),
      supports_responses: form.supports_responses,
      supports_vision: form.supports_vision,
      supports_reasoning: form.supports_reasoning,
    };
    if (creating) {
      await save.mutateAsync({ ...body, provider_id: form.provider_id, deployment: form.deployment.trim() });
      toast.success('Modelo cadastrado', form.name || form.deployment);
    } else {
      await save.mutateAsync({ id: model.id, ...body });
      toast.success('Modelo atualizado', form.name);
    }
    onClose();
  };

  return (
    <Dialog
      open
      onClose={onClose}
      size="lg"
      title={creating ? 'Adicionar modelo' : `Editar ${model.name}`}
      description={
        creating
          ? 'Para um deployment que a descoberta não encontra (ex.: provedor sem service principal).'
          : `Deployment ${model.deployment} em ${model.provider_name}`
      }
      onSubmit={() => void submit()}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancelar
          </Button>
          <Button type="submit" variant="primary" loading={save.isPending}>
            Salvar
          </Button>
        </>
      }
    >
      <FormGrid>
        {creating && (
          <>
            <Field label="Provedor" required error={touched && !form.provider_id ? 'Escolha o provedor.' : undefined}>
              <Select value={form.provider_id} onChange={(e) => set('provider_id', e.target.value)}>
                <option value="">Escolha…</option>
                {providers.data?.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.display_name || p.name}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Deployment no Foundry" required error={touched && !form.deployment.trim() ? 'Informe o nome do deployment.' : undefined}>
              <Input value={form.deployment} placeholder="ex.: gpt-4.1" onChange={(e) => set('deployment', e.target.value)} />
            </Field>
          </>
        )}
        <Field label="Nome no gateway" hint="É o valor de 'model' que os clientes enviam.">
          <Input value={form.name} placeholder={form.deployment || 'igual ao deployment'} onChange={(e) => set('name', e.target.value)} />
        </Field>
        <Field label="Tipo">
          <Segmented
            value={form.kind}
            onChange={(v) => set('kind', v)}
            options={[
              { value: 'chat', label: 'Chat' },
              { value: 'embeddings', label: 'Embeddings' },
            ]}
          />
        </Field>
        <Field label="Descrição" span={2}>
          <Textarea rows={2} value={form.description} placeholder="Para que serve, limites, recomendações" onChange={(e) => set('description', e.target.value)} />
        </Field>
        <Field label="Disponível para uso" span={2}>
          <Switch checked={form.enabled} onCheckedChange={(v) => set('enabled', v)} label={form.enabled ? 'Habilitado' : 'Desabilitado'} />
        </Field>

        <FormSection>Preço (US$ por 1 milhão de tokens)</FormSection>
        {missingPrice && (
          <div style={{ gridColumn: '1 / -1' }}>
            <Callout tone="warning">Sem preço, as chamadas a este modelo entram com custo zero e não contam no orçamento.</Callout>
          </div>
        )}
        <Field label="Entrada">
          <Input inputMode="decimal" value={form.input_price} placeholder="ex.: 2.00" onChange={(e) => set('input_price', e.target.value)} />
        </Field>
        <Field label="Saída">
          <Input inputMode="decimal" value={form.output_price} placeholder="ex.: 8.00" onChange={(e) => set('output_price', e.target.value)} />
        </Field>
        <Field label="Entrada em cache (Azure)" hint="Tokens do prompt servidos pelo cache do Azure. Vazio: preço de entrada.">
          <Input inputMode="decimal" value={form.cached_input_price} placeholder="ex.: 0.50" onChange={(e) => set('cached_input_price', e.target.value)} />
        </Field>

        <FormSection>Cache de respostas do gateway</FormSection>
        <Field label="Política" hint={form.cache_mode === 'default' ? `Padrão do gateway: ${formatTtl(defaultTtl)}.` : undefined}>
          <Select value={form.cache_mode} onChange={(e) => set('cache_mode', e.target.value as ModelForm['cache_mode'])}>
            <option value="default">Padrão do gateway</option>
            <option value="custom">Tempo próprio</option>
            <option value="off">Sem cache</option>
          </Select>
        </Field>
        {form.cache_mode === 'custom' && (
          <Field label="Guardar por (segundos)">
            <Input inputMode="numeric" value={form.cache_ttl} onChange={(e) => set('cache_ttl', e.target.value)} />
          </Field>
        )}

        <FormSection>Acesso</FormSection>
        <Field
          label="Grupos do LDAP"
          span={2}
          hint="Vazio: todos os usuários do gateway. Aceita o nome do grupo (CN) ou o DN completo."
        >
          <TagInput value={form.allowed_groups} onChange={(v) => set('allowed_groups', v)} placeholder="ex.: llm-usuarios" />
        </Field>

        <FormSection>Capacidades (exibidas aos usuários e no arquivo do Pi)</FormSection>
        <Field label="Janela de contexto (tokens)">
          <Input inputMode="numeric" value={form.context_window} placeholder="ex.: 128000" onChange={(e) => set('context_window', e.target.value)} />
        </Field>
        <Field label="Máximo de saída (tokens)">
          <Input inputMode="numeric" value={form.max_output_tokens} placeholder="ex.: 16384" onChange={(e) => set('max_output_tokens', e.target.value)} />
        </Field>
        <Field label="Recursos" span={2}>
          <div className="bt-row-wrap" style={{ gap: 20 }}>
            <Switch checked={form.supports_responses} onCheckedChange={(v) => set('supports_responses', v)} label="Responses API" />
            <Switch checked={form.supports_vision} onCheckedChange={(v) => set('supports_vision', v)} label="Imagens na entrada" />
            <Switch checked={form.supports_reasoning} onCheckedChange={(v) => set('supports_reasoning', v)} label="Raciocínio" />
          </div>
        </Field>
      </FormGrid>
    </Dialog>
  );
}

// ---------------------------------------------------------------- página
export function ModelsPage() {
  const me = useCurrentUser();
  const navigate = useNavigate();
  const confirm = useConfirm();
  const [showAll, setShowAll] = useState(me.is_admin);
  const [kind, setKind] = useState<'all' | 'chat' | 'embeddings'>('all');
  const [search, setSearch] = useState('');
  const q = useDebounced(search, 200).trim().toLowerCase();
  const [editing, setEditing] = useState<Model | 'new' | null>(null);
  const models = useModels(me.is_admin && showAll);
  const providers = useProviders(me.is_admin);
  const save = useSaveModel();
  const remove = useDeleteModel();
  const sync = useSyncProvider();
  const clearCache = useClearCache();

  const rows = useMemo(
    () =>
      (models.data ?? []).filter(
        (m) =>
          (kind === 'all' || m.kind === kind) &&
          (!q || m.name.toLowerCase().includes(q) || (m.upstream_model ?? '').toLowerCase().includes(q)),
      ),
    [models.data, kind, q],
  );

  const syncAll = async () => {
    const list = (providers.data ?? []).filter((p) => p.enabled && p.discovery && p.discovery !== 'none');
    if (!list.length) {
      toast.info('Nenhum provedor com descoberta', 'Cadastre os deployments manualmente ou configure a descoberta no provedor.');
      return;
    }
    let created = 0;
    let missing = 0;
    for (const p of list) {
      const r = await sync.mutateAsync(p.id);
      created += r.created;
      missing += r.missing;
    }
    toast.success('Modelos sincronizados', `${created} novo(s), ${missing} indisponível(is). Novos entram desabilitados.`);
  };

  const toggle = async (m: Model) => {
    await save.mutateAsync({ id: m.id, enabled: !m.enabled });
    toast.success(m.enabled ? 'Modelo desabilitado' : 'Modelo habilitado', m.name);
  };

  const del = async (m: Model) => {
    const { confirmed } = await confirm({
      title: `Excluir ${m.name}?`,
      description:
        m.source === 'discovered' && m.available
          ? 'O deployment ainda existe no Foundry: a próxima sincronização cadastra de novo (desabilitado). Para tirar do ar, prefira desabilitar.'
          : 'O histórico de uso continua guardado.',
      confirmLabel: 'Excluir',
    });
    if (!confirmed) return;
    await remove.mutateAsync(m.id);
    toast.success('Modelo excluído', m.name);
  };

  const columns: Column<Model>[] = [
    {
      key: 'name',
      header: 'Modelo',
      cell: (m) => (
        <CellTitle
          title={<span className="bt-mono">{m.name}</span>}
          sub={m.description || [m.upstream_model, m.upstream_version].filter(Boolean).join(' · ') || m.deployment}
        />
      ),
    },
    { key: 'kind', header: 'Tipo', cell: (m) => <KindTag kind={m.kind} /> },
    {
      key: 'provider',
      header: 'Provedor',
      hidden: !me.is_admin,
      cell: (m) => <CellTitle title={m.provider_name} sub={m.deployment !== m.name ? `deployment ${m.deployment}` : undefined} />,
    },
    { key: 'status', header: 'Status', hidden: !me.is_admin, cell: (m) => <ModelStatusBadge model={m} /> },
    {
      key: 'price',
      header: 'Entrada / saída',
      align: 'right',
      nowrap: true,
      cell: (m) =>
        m.input_price || m.output_price ? (
          <span className="bt-num">
            {formatPrice(m.input_price)} <span className="bt-subtle">/</span> {formatPrice(m.output_price)}
          </span>
        ) : (
          <Tag variant="outline">sem preço</Tag>
        ),
    },
    {
      key: 'cache',
      header: 'Cache',
      nowrap: true,
      cell: (m) => <span className={m.effective_cache_ttl_s ? undefined : 'bt-subtle'}>{formatTtl(m.effective_cache_ttl_s)}</span>,
    },
    {
      key: 'access',
      header: 'Acesso',
      cell: (m) =>
        m.allowed_groups.length ? (
          <div className="bt-row-wrap">
            {m.allowed_groups.map((g) => (
              <Tag key={g} variant="green">
                {g}
              </Tag>
            ))}
          </div>
        ) : (
          <span className="bt-muted">Todos</span>
        ),
    },
    {
      key: 'actions',
      header: '',
      actions: true,
      cell: (m) => (
        <Menu
          label={`Ações do modelo ${m.name}`}
          items={[
            {
              label: 'Testar no playground',
              icon: <MessagesSquare size={16} />,
              hidden: m.kind !== 'chat' || !m.accessible,
              onSelect: () => navigate(`/playground?modelo=${encodeURIComponent(m.name)}`),
            },
            { label: 'Editar', icon: <Pencil size={16} />, hidden: !me.is_admin, onSelect: () => setEditing(m) },
            {
              label: m.enabled ? 'Desabilitar' : 'Habilitar',
              icon: <Power size={16} />,
              hidden: !me.is_admin,
              onSelect: () => void toggle(m),
            },
            { label: 'Excluir', icon: <Trash2 size={16} />, danger: true, hidden: !me.is_admin, onSelect: () => void del(m) },
          ]}
        />
      ),
    },
  ];

  const enabled = models.data?.filter((m) => m.enabled && m.available).length ?? 0;

  return (
    <Page>
      <PageHeader
        crumbs={[{ label: 'Início', href: '/' }, { label: 'Modelos' }]}
        title="Modelos"
        description={
          me.is_admin
            ? 'Deployments do Foundry expostos pelo gateway: nome público, preço, cache e quem pode usar.'
            : 'Modelos que você pode usar, com o preço de cada um. Envie o nome em "model".'
        }
        actions={
          me.is_admin && (
            <>
              <Menu
                label="Mais ações"
                items={[
                  {
                    label: 'Limpar o cache de respostas',
                    icon: <Trash2 size={16} />,
                    onSelect: async () => {
                      const r = await clearCache.mutateAsync();
                      toast.success('Cache limpo', `${formatNumber(r.removed)} resposta(s) removida(s).`);
                    },
                  },
                ]}
              />
              <Button variant="secondary" icon={<RefreshCw size={16} />} loading={sync.isPending} onClick={() => void syncAll()}>
                Sincronizar com o Foundry
              </Button>
              <Button variant="primary" icon={<Plus size={16} />} onClick={() => setEditing('new')}>
                Adicionar modelo
              </Button>
            </>
          )
        }
      />

      <Card flush>
        <FilterBar>
          <SearchInput controlSize="sm" placeholder="Buscar modelo" value={search} onChange={(e) => setSearch(e.target.value)} aria-label="Buscar modelo" />
          <Segmented
            aria-label="Tipo"
            value={kind}
            onChange={setKind}
            options={[
              { value: 'all', label: 'Todos' },
              { value: 'chat', label: 'Chat' },
              { value: 'embeddings', label: 'Embeddings' },
            ]}
          />
          {me.is_admin && (
            <Chip pressed={!showAll} onPressedChange={(p) => setShowAll(!p)}>
              Só os disponíveis
            </Chip>
          )}
          <span className="bt-grow" />
          <span className="bt-muted bt-small">
            {enabled} disponíve{enabled === 1 ? 'l' : 'is'}
            {me.is_admin && models.data ? ` de ${models.data.length}` : ''}
          </span>
        </FilterBar>
        <DataTable
          columns={columns}
          rows={rows}
          loading={models.isPending}
          getRowId={(m) => m.id}
          rowClassName={(m) => (!m.enabled || !m.available ? 'gw-row-muted' : undefined)}
          empty={
            <EmptyState
              icon={<Boxes size={24} />}
              title={me.is_admin ? 'Nenhum modelo cadastrado' : 'Nenhum modelo liberado para você'}
              description={
                me.is_admin
                  ? 'Cadastre um provedor do Foundry e sincronize: os deployments aparecem aqui, desabilitados até você revisar preço e acesso.'
                  : 'Peça ao administrador do gateway para liberar um modelo para o seu grupo.'
              }
            />
          }
          aria-label="Modelos"
        />
      </Card>

      {editing && <ModelDialog model={editing === 'new' ? null : editing} onClose={() => setEditing(null)} />}
    </Page>
  );
}
