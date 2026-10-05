import {
  Button,
  Callout,
  Card,
  CellTitle,
  CodeBlock,
  CopyButton,
  DataTable,
  Dialog,
  EmptyState,
  Field,
  FormGrid,
  InlineCode,
  Input,
  Menu,
  Page,
  PageHeader,
  RelativeTime,
  SecretBox,
  Select,
  Switch,
  Tag,
  formatDate,
  toast,
  useConfirm,
  type Column,
} from 'broto-ui';
import { KeyRound, Pencil, Plus, Trash2 } from 'lucide-react';
import { useState } from 'react';
import { useCreateKey, useKeys, useModels, useRevokeKey, useUpdateKey, type KeyInput } from '../api/queries';
import type { ApiKey, ApiKeyCreated, Model } from '../api/types';
import { KeyStatusBadge } from '../components/badges';
import { useConfig, useCurrentUser } from '../components/context';
import { formatUsd } from '../lib/format';

// ---------------------------------------------------------------- formulário
interface KeyForm {
  name: string;
  expires: string;
  budget: string;
  rpm: string;
  restrict: boolean;
  models: string[];
}

function toForm(key?: ApiKey): KeyForm {
  return {
    name: key?.name ?? '',
    expires: '90',
    budget: key?.monthly_budget_usd != null ? String(key.monthly_budget_usd) : '',
    rpm: key?.rpm_limit != null ? String(key.rpm_limit) : '',
    restrict: !!key?.allowed_models,
    models: key?.allowed_models ?? [],
  };
}

function parseOptional(value: string): number | null {
  const n = Number(value.replace(',', '.'));
  return value.trim() === '' || Number.isNaN(n) ? null : n;
}

export function ModelPicker({
  models,
  value,
  onChange,
}: {
  models: Model[];
  value: string[];
  onChange: (value: string[]) => void;
}) {
  return (
    <div className="gw-model-picker">
      {models.map((m) => (
        <label key={m.id} className="gw-model-option">
          <input
            type="checkbox"
            checked={value.includes(m.name)}
            onChange={(e) => onChange(e.target.checked ? [...value, m.name] : value.filter((v) => v !== m.name))}
          />
          <span className="bt-mono">{m.name}</span>
          <span className="bt-subtle bt-small">{m.kind === 'embeddings' ? 'embeddings' : 'chat'}</span>
        </label>
      ))}
    </div>
  );
}

export function KeyDialog({
  open,
  onClose,
  editing,
  userId,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  editing?: ApiKey | null;
  userId?: string;
  onCreated?: (key: ApiKeyCreated) => void;
}) {
  const [form, setForm] = useState<KeyForm>(() => toForm(editing ?? undefined));
  const [touched, setTouched] = useState(false);
  const models = useModels();
  const create = useCreateKey(userId);
  const update = useUpdateKey();
  const set = <K extends keyof KeyForm>(k: K, v: KeyForm[K]) => setForm((f) => ({ ...f, [k]: v }));
  const nameError = touched && !form.name.trim() ? 'Dê um nome para lembrar onde a chave é usada.' : undefined;
  const modelsError = touched && form.restrict && !form.models.length ? 'Escolha ao menos um modelo.' : undefined;

  const submit = async () => {
    setTouched(true);
    if (!form.name.trim() || (form.restrict && !form.models.length)) return;
    const body: KeyInput = {
      name: form.name.trim(),
      monthly_budget_usd: parseOptional(form.budget),
      rpm_limit: parseOptional(form.rpm),
      allowed_models: form.restrict ? form.models : null,
    };
    if (editing) {
      await update.mutateAsync({ id: editing.id, ...body });
      toast.success('Chave atualizada');
      onClose();
    } else {
      const created = await create.mutateAsync({
        ...body,
        expires_in_days: form.expires === 'never' ? null : Number(form.expires),
      });
      onCreated?.(created);
    }
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      size="md"
      title={editing ? 'Editar chave' : 'Nova chave de API'}
      description={editing ? editing.display : 'Use uma chave por ferramenta ou projeto: fica fácil ver o custo de cada uma e revogar só a que vazou.'}
      onSubmit={() => void submit()}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancelar
          </Button>
          <Button type="submit" variant="primary" loading={create.isPending || update.isPending}>
            {editing ? 'Salvar' : 'Criar chave'}
          </Button>
        </>
      }
    >
      <FormGrid>
        <Field label="Nome" required error={nameError} span={2}>
          <Input value={form.name} placeholder="ex.: notebook, pipeline-relatorios, pi" onChange={(e) => set('name', e.target.value)} />
        </Field>
        {!editing && (
          <Field label="Validade">
            <Select value={form.expires} onChange={(e) => set('expires', e.target.value)}>
              <option value="30">30 dias</option>
              <option value="90">90 dias</option>
              <option value="180">180 dias</option>
              <option value="365">1 ano</option>
              <option value="never">Sem expiração</option>
            </Select>
          </Field>
        )}
        <Field label="Limite de gasto por mês" hint="Em US$. Vazio: vale só o limite do seu usuário.">
          <Input inputMode="decimal" value={form.budget} placeholder="sem limite" onChange={(e) => set('budget', e.target.value)} />
        </Field>
        <Field label="Requisições por minuto" hint="Vazio: sem limite próprio.">
          <Input inputMode="numeric" value={form.rpm} placeholder="sem limite" onChange={(e) => set('rpm', e.target.value)} />
        </Field>
        <Field label="Modelos" span={2} error={modelsError} hint={form.restrict ? undefined : 'A chave usa todos os modelos liberados para você.'}>
          <div className="bt-stack-sm">
            <Switch checked={form.restrict} onCheckedChange={(v) => set('restrict', v)} label="Restringir a alguns modelos" />
            {form.restrict && <ModelPicker models={models.data ?? []} value={form.models} onChange={(v) => set('models', v)} />}
          </div>
        </Field>
      </FormGrid>
    </Dialog>
  );
}

export function KeyCreatedDialog({ created, onClose }: { created: ApiKeyCreated | null; onClose: () => void }) {
  const config = useConfig();
  if (!created) return null;
  const snippet = `export OPENAI_BASE_URL="${config.api_base_url}"\nexport OPENAI_API_KEY="${created.key}"`;
  return (
    <Dialog
      open
      onClose={onClose}
      size="lg"
      title="Chave criada"
      description={`"${created.name}" de ${created.username}`}
      footer={
        <Button variant="primary" onClick={onClose}>
          Já copiei
        </Button>
      }
    >
      <div className="bt-stack">
        <Callout tone="warning" title="Copie agora">
          A chave aparece só desta vez. O gateway guarda apenas um resumo dela e não consegue mostrá-la de novo.
        </Callout>
        <SecretBox value={created.key} copyLabel="Copiar chave" />
        <div className="bt-stack-sm">
          <span className="bt-strong">Para usar com qualquer cliente OpenAI</span>
          <CodeBlock copyValue={snippet}>{snippet}</CodeBlock>
        </div>
      </div>
    </Dialog>
  );
}

// ---------------------------------------------------------------- tabela
export function keyColumns(
  onEdit: (k: ApiKey) => void,
  onRevoke: (k: ApiKey) => void,
  showUser = false,
  isAdmin = true,
): Column<ApiKey>[] {
  return [
    { key: 'name', header: 'Nome', cell: (k) => <CellTitle title={k.name} sub={<span className="bt-mono">{k.display}</span>} /> },
    { key: 'user', header: 'Usuário', cell: (k) => k.username, hidden: !showUser },
    { key: 'status', header: 'Status', cell: (k) => <KeyStatusBadge status={k.status} /> },
    {
      key: 'spent',
      header: 'Gasto no mês',
      align: 'right',
      nowrap: true,
      cell: (k) => (
        <span className="bt-num">
          {formatUsd(k.spent_month_usd)}
          {k.monthly_budget_usd != null && <span className="bt-subtle"> / {formatUsd(k.monthly_budget_usd)}</span>}
        </span>
      ),
    },
    {
      key: 'models',
      header: 'Modelos',
      cell: (k) =>
        k.allowed_models ? (
          <div className="bt-row-wrap">
            {k.allowed_models.slice(0, 3).map((m) => (
              <Tag key={m} mono>
                {m}
              </Tag>
            ))}
            {k.allowed_models.length > 3 && <span className="bt-subtle bt-small">+{k.allowed_models.length - 3}</span>}
          </div>
        ) : (
          <span className="bt-muted">Todos liberados</span>
        ),
    },
    { key: 'used', header: 'Último uso', nowrap: true, cell: (k) => <RelativeTime value={k.last_used_at} fallback="Nunca" /> },
    { key: 'expires', header: 'Expira', nowrap: true, cell: (k) => (k.expires_at ? formatDate(k.expires_at) : <span className="bt-subtle">Não expira</span>) },
    {
      key: 'actions',
      header: '',
      actions: true,
      cell: (k) =>
        k.status === 'revoked' ? null : (
          <Menu
            label={`Ações da chave ${k.name}`}
            items={[
              {
                label: k.limits_locked && !isAdmin ? 'Limites definidos pelo administrador' : 'Editar limites',
                icon: <Pencil size={16} />,
                disabled: k.limits_locked && !isAdmin,
                onSelect: () => onEdit(k),
              },
              'separator',
              { label: 'Revogar', icon: <Trash2 size={16} />, danger: true, onSelect: () => onRevoke(k) },
            ]}
          />
        ),
    },
  ];
}

export function useRevokeFlow() {
  const confirm = useConfirm();
  const revoke = useRevokeKey();
  return async (k: ApiKey) => {
    const { confirmed } = await confirm({
      title: `Revogar a chave "${k.name}"?`,
      description: 'As chamadas com esta chave passam a ser recusadas na hora. Não dá para desfazer.',
      confirmLabel: 'Revogar chave',
    });
    if (!confirmed) return;
    await revoke.mutateAsync(k.id);
    toast.success('Chave revogada', k.display);
  };
}

export function KeysPage() {
  const config = useConfig();
  const keys = useKeys();
  const [dialog, setDialog] = useState<{ open: boolean; editing: ApiKey | null }>({ open: false, editing: null });
  const [created, setCreated] = useState<ApiKeyCreated | null>(null);
  const revoke = useRevokeFlow();
  const me = useCurrentUser();
  const columns = keyColumns(
    (k) => setDialog({ open: true, editing: k }),
    (k) => void revoke(k),
    false,
    me.is_admin,
  );
  const active = keys.data?.filter((k) => k.status === 'active').length ?? 0;

  return (
    <Page>
      <PageHeader
        crumbs={[{ label: 'Início', href: '/' }, { label: 'Chaves de API' }]}
        title="Chaves de API"
        description="Cada chave identifica você nas chamadas ao gateway. O custo de cada uma aparece separado."
        actions={
          <Button variant="primary" icon={<Plus size={16} />} onClick={() => setDialog({ open: true, editing: null })}>
            Nova chave
          </Button>
        }
      />

      <Card>
        <div className="gw-endpoint">
          <div>
            <div className="bt-strong">Endereço da API</div>
            <div className="bt-muted bt-small">
              Compatível com a OpenAI: use como <InlineCode>base_url</InlineCode> e a chave como <InlineCode>api_key</InlineCode>.
            </div>
          </div>
          <div className="bt-row">
            <code className="gw-endpoint-url">{config.api_base_url}</code>
            <CopyButton value={config.api_base_url} label="Copiar endereço" />
          </div>
        </div>
      </Card>

      <Card title="Suas chaves" subtitle={`${active} ativa${active === 1 ? '' : 's'}`} flush>
        <DataTable
          columns={columns}
          rows={keys.data}
          loading={keys.isPending}
          getRowId={(k) => k.id}
          rowClassName={(k) => (k.status !== 'active' ? 'gw-row-muted' : undefined)}
          empty={
            <EmptyState
              icon={<KeyRound size={24} />}
              title="Você ainda não tem chaves"
              description="Crie uma para chamar os modelos pelo SDK da OpenAI, pelo LangChain, pelo Pi ou por qualquer ferramenta compatível."
              action={
                <Button variant="primary" icon={<Plus size={16} />} onClick={() => setDialog({ open: true, editing: null })}>
                  Criar a primeira chave
                </Button>
              }
            />
          }
          aria-label="Chaves de API"
        />
      </Card>

      {dialog.open && (
        <KeyDialog
          open
          editing={dialog.editing}
          onClose={() => setDialog({ open: false, editing: null })}
          onCreated={(k) => {
            setDialog({ open: false, editing: null });
            setCreated(k);
          }}
        />
      )}
      <KeyCreatedDialog created={created} onClose={() => setCreated(null)} />
    </Page>
  );
}
