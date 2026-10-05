import {
  Avatar,
  Button,
  Callout,
  Card,
  CellTitle,
  Chip,
  DataTable,
  Dialog,
  Drawer,
  EmptyState,
  Field,
  FilterBar,
  FormGrid,
  FormSection,
  Input,
  KeyValue,
  Menu,
  Page,
  PageHeader,
  Pagination,
  ProgressBar,
  RelativeTime,
  SearchInput,
  Segmented,
  Switch,
  Tag,
  TagInput,
  Textarea,
  formatDateTime,
  formatNumber,
  toast,
  useDebounced,
  type Column,
} from 'broto-ui';
import { ChartColumnBig, KeyRound, Lock, LockOpen, Pencil, Plus, RefreshCw, UserPlus, Users } from 'lucide-react';
import { useState } from 'react';
import { useNavigate } from 'react-router';
import { useKeys, useLdapSyncUser, useModels, useSaveUser, useUsers, type UserInput } from '../../api/queries';
import type { ApiKey, ApiKeyCreated, User } from '../../api/types';
import { AdminOnly } from '../../components/AdminOnly';
import { UserStatusBadge } from '../../components/badges';
import { useConfig, useCurrentUser } from '../../components/context';
import { budgetPct, formatUsd } from '../../lib/format';
import { KeyCreatedDialog, KeyDialog, ModelPicker, keyColumns, useRevokeFlow } from '../KeysPage';

// ---------------------------------------------------------------- edição
interface UserForm {
  username: string;
  display_name: string;
  role: 'user' | 'admin';
  limitBudget: boolean;
  budget: string;
  rpm: string;
  tpm: string;
  restrict: boolean;
  models: string[];
  groups: string[];
}

function toForm(u?: User | null, defaultBudget?: number | null): UserForm {
  const budget = u ? u.monthly_budget_usd : (defaultBudget ?? null);
  return {
    username: u?.username ?? '',
    display_name: u?.display_name ?? '',
    role: u?.role ?? 'user',
    limitBudget: budget !== null,
    budget: budget !== null ? String(budget) : '50',
    rpm: u?.rpm_limit != null ? String(u.rpm_limit) : '',
    tpm: u?.tpm_limit != null ? String(u.tpm_limit) : '',
    restrict: !!u?.allowed_models,
    models: u?.allowed_models ?? [],
    groups: u?.groups ?? [],
  };
}

function num(value: string): number | null {
  const n = Number(value.replace(',', '.'));
  return value.trim() === '' || Number.isNaN(n) ? null : n;
}

function UserDialog({ user, onClose }: { user: User | null; onClose: () => void }) {
  const config = useConfig();
  const [form, setForm] = useState<UserForm>(() => toForm(user));
  const [touched, setTouched] = useState(false);
  const models = useModels(true);
  const save = useSaveUser();
  const set = <K extends keyof UserForm>(k: K, v: UserForm[K]) => setForm((f) => ({ ...f, [k]: v }));
  const creating = !user;
  const dev = config.auth.mode === 'dev';

  const submit = async () => {
    setTouched(true);
    if (creating && form.username.trim().length < 2) return;
    const body: UserInput = {
      display_name: form.display_name.trim() || null,
      role: form.role,
      monthly_budget_usd: form.limitBudget ? (num(form.budget) ?? 0) : null,
      rpm_limit: num(form.rpm),
      tpm_limit: num(form.tpm),
      allowed_models: form.restrict ? form.models : null,
      ...(dev && !creating ? { groups: form.groups } : {}),
    };
    if (creating) {
      await save.mutateAsync({ ...body, username: form.username.trim() });
      toast.success('Usuário cadastrado', form.username.trim().toLowerCase());
    } else {
      await save.mutateAsync({ id: user.id, ...body });
      toast.success('Usuário atualizado', user.username);
    }
    onClose();
  };

  return (
    <Dialog
      open
      onClose={onClose}
      size="lg"
      title={creating ? 'Pré-cadastrar usuário' : `Editar ${user.display_name || user.username}`}
      description={
        creating
          ? 'Define limites antes do primeiro login. O acesso continua dependendo do LDAP.'
          : user.username
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
          <Field label="Usuário (login do LDAP)" required error={touched && form.username.trim().length < 2 ? 'Informe o login.' : undefined}>
            <Input value={form.username} placeholder="ex.: ana.souza" onChange={(e) => set('username', e.target.value)} />
          </Field>
        )}
        <Field label="Nome de exibição">
          <Input value={form.display_name} onChange={(e) => set('display_name', e.target.value)} />
        </Field>
        <Field label="Papel" hint="Membros dos grupos de administradores do LDAP são administradores de qualquer forma.">
          <Segmented
            value={form.role}
            onChange={(v) => set('role', v)}
            options={[
              { value: 'user', label: 'Usuário' },
              { value: 'admin', label: 'Administrador' },
            ]}
          />
        </Field>

        <FormSection>Custos e limites</FormSection>
        <Field label="Orçamento mensal (US$)" hint="Ao atingir, as chamadas são recusadas até o mês virar.">
          <div className="bt-stack-sm">
            <Switch checked={form.limitBudget} onCheckedChange={(v) => set('limitBudget', v)} label={form.limitBudget ? 'Com limite' : 'Sem limite'} />
            {form.limitBudget && <Input inputMode="decimal" value={form.budget} onChange={(e) => set('budget', e.target.value)} />}
          </div>
        </Field>
        <Field label="Requisições por minuto" hint="Vazio: padrão do gateway.">
          <Input inputMode="numeric" value={form.rpm} placeholder="padrão" onChange={(e) => set('rpm', e.target.value)} />
        </Field>
        <Field label="Tokens por minuto" hint="Vazio: padrão do gateway.">
          <Input inputMode="numeric" value={form.tpm} placeholder="padrão" onChange={(e) => set('tpm', e.target.value)} />
        </Field>

        <FormSection>Modelos</FormSection>
        <Field label="Modelos liberados" span={2} hint={form.restrict ? undefined : 'Todos os modelos liberados para os grupos do usuário.'}>
          <div className="bt-stack-sm">
            <Switch checked={form.restrict} onCheckedChange={(v) => set('restrict', v)} label="Restringir a uma lista" />
            {form.restrict && <ModelPicker models={models.data ?? []} value={form.models} onChange={(v) => set('models', v)} />}
          </div>
        </Field>
        {dev && !creating && (
          <Field label="Grupos" span={2} hint="Só no modo dev. Com LDAP, os grupos vêm do diretório.">
            <TagInput value={form.groups} onChange={(v) => set('groups', v)} placeholder="ex.: llm-usuarios" />
          </Field>
        )}
      </FormGrid>
    </Dialog>
  );
}

function BlockDialog({ user, onClose }: { user: User; onClose: () => void }) {
  const [reason, setReason] = useState('');
  const save = useSaveUser();
  return (
    <Dialog
      open
      onClose={onClose}
      size="sm"
      role="alertdialog"
      title={`Bloquear ${user.display_name || user.username}?`}
      description="As chaves param de funcionar na hora e as sessões no console são encerradas. Dá para desbloquear depois."
      onSubmit={async () => {
        await save.mutateAsync({ id: user.id, status: 'blocked', status_reason: reason.trim() || null });
        toast.success('Usuário bloqueado', user.username);
        onClose();
      }}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancelar
          </Button>
          <Button type="submit" variant="danger" loading={save.isPending} icon={<Lock size={16} />}>
            Bloquear
          </Button>
        </>
      }
    >
      <Field label="Motivo" hint="Aparece para o usuário e na auditoria.">
        <Textarea rows={3} value={reason} onChange={(e) => setReason(e.target.value)} />
      </Field>
    </Dialog>
  );
}

// ---------------------------------------------------------------- detalhe
function UserDrawer({ user, onClose, onEdit }: { user: User; onClose: () => void; onEdit: () => void }) {
  const navigate = useNavigate();
  const keys = useKeys(user.id);
  const revoke = useRevokeFlow();
  const [keyDialog, setKeyDialog] = useState<{ open: boolean; editing: ApiKey | null }>({ open: false, editing: null });
  const [created, setCreated] = useState<ApiKeyCreated | null>(null);
  const columns = keyColumns((k) => setKeyDialog({ open: true, editing: k }), (k) => void revoke(k)).filter((c) =>
    ['name', 'status', 'spent', 'actions'].includes(c.key),
  );
  const pct = budgetPct(user.spent_month_usd, user.monthly_budget_usd);

  return (
    <Drawer
      open
      onClose={onClose}
      size="lg"
      title={user.display_name || user.username}
      description={[user.username, user.email].filter(Boolean).join(' · ')}
      footer={
        <>
          <Button variant="secondary" icon={<ChartColumnBig size={16} />} onClick={() => navigate(`/uso?usuario=${user.id}&periodo=30d`)}>
            Ver uso
          </Button>
          <Button variant="primary" icon={<Pencil size={16} />} onClick={onEdit}>
            Editar
          </Button>
        </>
      }
    >
      <div className="bt-stack">
        {user.status !== 'active' && (
          <Callout tone={user.status === 'blocked' ? 'danger' : 'warning'} title={user.status === 'blocked' ? 'Bloqueado' : 'Sem acesso no LDAP'}>
            {user.status_reason || 'Sem motivo informado.'}
          </Callout>
        )}
        <KeyValue
          items={[
            ['Status', <UserStatusBadge key="s" status={user.status} />],
            ['Papel', user.is_admin ? 'Administrador' : 'Usuário'],
            [
              'Grupos',
              user.groups.length ? (
                <div key="g" className="bt-row-wrap">
                  {user.groups.map((g) => (
                    <Tag key={g} variant="green">
                      {g}
                    </Tag>
                  ))}
                </div>
              ) : (
                '—'
              ),
            ],
            [
              'Gasto no mês',
              <div key="b" className="bt-stack-sm">
                <span>
                  {formatUsd(user.spent_month_usd)} {user.monthly_budget_usd !== null ? `de ${formatUsd(user.monthly_budget_usd)}` : '(sem limite)'}
                </span>
                {pct !== null && <ProgressBar value={pct} tone={pct >= 100 ? 'danger' : pct >= 80 ? 'warning' : 'default'} />}
              </div>,
            ],
            ['Chamadas no mês', formatNumber(user.requests_month)],
            ['Limites por minuto', `${user.rpm_limit ?? 'padrão'} req · ${user.tpm_limit ?? 'padrão'} tokens`],
            ['Modelos', user.allowed_models ? user.allowed_models.join(', ') : 'Todos liberados para os grupos'],
            ['Primeiro acesso', formatDateTime(user.created_at)],
            ['Último login', user.last_login_at ? formatDateTime(user.last_login_at) : 'Nunca'],
            ['Última chamada', user.last_seen_at ? <RelativeTime key="l" value={user.last_seen_at} /> : '—'],
            { label: 'Sincronizado com o LDAP', value: user.ldap_synced_at ? <RelativeTime value={user.ldap_synced_at} /> : null, hidden: !user.ldap_synced_at },
          ]}
        />
        <Card
          title="Chaves de API"
          actions={
            <Button size="sm" variant="secondary" icon={<Plus size={14} />} disabled={user.status !== 'active'} onClick={() => setKeyDialog({ open: true, editing: null })}>
              Nova chave
            </Button>
          }
          flush
        >
          <DataTable
            compact
            columns={columns}
            rows={keys.data}
            loading={keys.isPending}
            getRowId={(k) => k.id}
            rowClassName={(k) => (k.status !== 'active' ? 'gw-row-muted' : undefined)}
            empty={<EmptyState compact icon={<KeyRound size={20} />} title="Nenhuma chave" />}
            aria-label="Chaves do usuário"
          />
        </Card>
      </div>
      {keyDialog.open && (
        <KeyDialog
          open
          userId={user.id}
          editing={keyDialog.editing}
          onClose={() => setKeyDialog({ open: false, editing: null })}
          onCreated={(k) => {
            setKeyDialog({ open: false, editing: null });
            setCreated(k);
          }}
        />
      )}
      <KeyCreatedDialog created={created} onClose={() => setCreated(null)} />
    </Drawer>
  );
}

// ---------------------------------------------------------------- página
function UsersContent() {
  const me = useCurrentUser();
  const config = useConfig();
  const [search, setSearch] = useState('');
  const q = useDebounced(search, 250);
  const [status, setStatus] = useState<string>('');
  const [offset, setOffset] = useState(0);
  const [active, setActive] = useState<string | null>(null);
  const [editing, setEditing] = useState<User | 'new' | null>(null);
  const [blocking, setBlocking] = useState<User | null>(null);
  const limit = 50;
  const users = useUsers({ q: q || undefined, status: status || undefined, limit, offset });
  const save = useSaveUser();
  const ldapSync = useLdapSyncUser();
  const selected = users.data?.items.find((u) => u.id === active) ?? null;

  const columns: Column<User>[] = [
    {
      key: 'user',
      header: 'Usuário',
      cell: (u) => (
        <div className="bt-row">
          <Avatar name={u.display_name || u.username} size="sm" />
          <CellTitle title={u.display_name || u.username} sub={u.email || u.username} />
        </div>
      ),
    },
    {
      key: 'groups',
      header: 'Grupos',
      cell: (u) =>
        u.groups.length ? (
          <div className="bt-row-wrap">
            {u.groups.slice(0, 2).map((g) => (
              <Tag key={g} variant="green">
                {g}
              </Tag>
            ))}
            {u.groups.length > 2 && <span className="bt-subtle bt-small">+{u.groups.length - 2}</span>}
          </div>
        ) : (
          <span className="bt-subtle">—</span>
        ),
    },
    { key: 'role', header: 'Papel', cell: (u) => (u.is_admin ? <Tag variant="solid">Admin</Tag> : <span className="bt-muted">Usuário</span>) },
    { key: 'status', header: 'Status', cell: (u) => <UserStatusBadge status={u.status} /> },
    {
      key: 'spent',
      header: 'Gasto no mês',
      width: 200,
      cell: (u) => {
        const pct = budgetPct(u.spent_month_usd, u.monthly_budget_usd);
        return (
          <div className="bt-stack-sm">
            <span className="bt-num bt-small">
              {formatUsd(u.spent_month_usd)}
              <span className="bt-subtle"> {u.monthly_budget_usd !== null ? `de ${formatUsd(u.monthly_budget_usd)}` : 'sem limite'}</span>
            </span>
            {pct !== null && <ProgressBar value={pct} height={4} tone={pct >= 100 ? 'danger' : pct >= 80 ? 'warning' : 'default'} />}
          </div>
        );
      },
    },
    { key: 'keys', header: 'Chaves', align: 'right', cell: (u) => <span className="bt-num">{u.keys_active}</span> },
    { key: 'seen', header: 'Última chamada', nowrap: true, cell: (u) => <RelativeTime value={u.last_seen_at} fallback="Nunca" /> },
    {
      key: 'actions',
      header: '',
      actions: true,
      cell: (u) => (
        <Menu
          label={`Ações de ${u.username}`}
          items={[
            { label: 'Editar', icon: <Pencil size={16} />, onSelect: () => setEditing(u) },
            {
              label: 'Sincronizar com o LDAP',
              icon: <RefreshCw size={16} />,
              hidden: config.auth.mode !== 'ldap',
              onSelect: async () => {
                const r = await ldapSync.mutateAsync(u.id);
                toast.success('Usuário sincronizado', `${r.username}: ${r.status === 'active' ? 'com acesso' : 'sem acesso'}`);
              },
            },
            'separator',
            u.status === 'blocked'
              ? {
                  label: 'Desbloquear',
                  icon: <LockOpen size={16} />,
                  onSelect: async () => {
                    await save.mutateAsync({ id: u.id, status: 'active' });
                    toast.success('Usuário desbloqueado', u.username);
                  },
                }
              : { label: 'Bloquear', icon: <Lock size={16} />, danger: true, disabled: u.id === me.id, onSelect: () => setBlocking(u) },
          ]}
        />
      ),
    },
  ];

  const total = users.data?.total ?? 0;
  return (
    <Page>
      <PageHeader
        crumbs={[{ label: 'Início', href: '/' }, { label: 'Administração' }, { label: 'Usuários' }]}
        title="Usuários"
        description={
          config.auth.mode === 'ldap'
            ? 'Quem entrou no console pelo LDAP. Quem sai dos grupos com acesso é desativado na sincronização periódica.'
            : 'Usuários do gateway (modo dev: sem LDAP).'
        }
        actions={
          <Button variant="primary" icon={<UserPlus size={16} />} onClick={() => setEditing('new')}>
            Pré-cadastrar
          </Button>
        }
      />

      <Card flush>
        <FilterBar>
          <SearchInput
            controlSize="sm"
            placeholder="Nome, login ou e-mail"
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setOffset(0);
            }}
            aria-label="Buscar usuário"
          />
          {(['active', 'blocked', 'disabled'] as const).map((s) => (
            <Chip
              key={s}
              pressed={status === s}
              tone={s === 'active' ? 'success' : s === 'blocked' ? 'danger' : 'orange'}
              onPressedChange={(p) => {
                setStatus(p ? s : '');
                setOffset(0);
              }}
            >
              {s === 'active' ? 'Ativos' : s === 'blocked' ? 'Bloqueados' : 'Sem acesso no LDAP'}
            </Chip>
          ))}
          <span className="bt-grow" />
          <span className="bt-muted bt-small">
            {formatNumber(total)} usuário{total === 1 ? '' : 's'}
          </span>
        </FilterBar>
        <DataTable
          columns={columns}
          rows={users.data?.items}
          loading={users.isPending}
          getRowId={(u) => u.id}
          onRowClick={(u) => setActive(u.id)}
          activeRowId={active}
          rowClassName={(u) => (u.status !== 'active' ? 'gw-row-muted' : undefined)}
          empty={<EmptyState compact icon={<Users size={22} />} title="Nenhum usuário encontrado" />}
          footer={total > limit ? <Pagination total={total} limit={limit} offset={offset} onChange={setOffset} noun="usuários" /> : undefined}
          aria-label="Usuários"
        />
      </Card>

      {selected && <UserDrawer user={selected} onClose={() => setActive(null)} onEdit={() => setEditing(selected)} />}
      {editing && <UserDialog user={editing === 'new' ? null : editing} onClose={() => setEditing(null)} />}
      {blocking && <BlockDialog user={blocking} onClose={() => setBlocking(null)} />}
    </Page>
  );
}

export function UsersPage() {
  return (
    <AdminOnly>
      <UsersContent />
    </AdminOnly>
  );
}
