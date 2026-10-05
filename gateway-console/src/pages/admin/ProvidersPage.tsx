import {
  Button,
  Callout,
  Card,
  CodeBlock,
  EmptyState,
  Field,
  FormGrid,
  FormSection,
  Grid,
  IconTile,
  InlineCode,
  Input,
  KeyValue,
  Menu,
  Page,
  PageHeader,
  RelativeTime,
  Segmented,
  Select,
  Dialog,
  Tag,
  TagInput,
  toast,
  useConfirm,
} from 'broto-ui';
import { Cloud, KeyRound, Pencil, Plug, Plus, Power, RefreshCw, Trash2 } from 'lucide-react';
import { useState } from 'react';
import {
  useCheckProvider,
  useCreateProvider,
  useDeleteProvider,
  useProviders,
  useSyncProvider,
  useUpdateProvider,
  useUpdateProviderSecret,
} from '../../api/queries';
import type { Provider, ProviderSecretInput } from '../../api/types';
import { AdminOnly } from '../../components/AdminOnly';
import { ProviderStatusBadge, providerStatus } from '../../components/badges';

const DISCOVERY_LABEL: Record<string, string> = {
  project: 'Projeto do Foundry',
  arm: 'Azure Resource Manager',
  static: 'Lista fixa',
  none: 'Sem descoberta',
};

const AUTH_LABEL: Record<string, string> = {
  api_key: 'Chave de API',
  service_principal: 'Service principal (Entra ID)',
};

const VAULT_EXAMPLE = `vault kv put secret/llmgw/providers/foundry \\
  endpoint="https://<recurso>.services.ai.azure.com" \\
  api_key="<chave do recurso>" \\
  tenant_id="<tenant>" client_id="<app>" client_secret="<segredo>" \\
  project_endpoint="https://<recurso>.services.ai.azure.com/api/projects/<projeto>"`;

// ---------------------------------------------------------------- formulário da credencial
interface SecretForm {
  endpoint: string;
  auth: 'api_key' | 'service_principal';
  api_key: string;
  tenant_id: string;
  client_id: string;
  client_secret: string;
  discovery: 'project' | 'arm' | 'static' | 'none';
  project_endpoint: string;
  subscription_id: string;
  resource_group: string;
  account_name: string;
  deployments: string[];
  api_style: 'v1' | 'deployments';
  api_version: string;
}

function secretForm(p?: Provider | null): SecretForm {
  const c = p?.config;
  return {
    endpoint: c?.endpoint ?? '',
    auth: c?.auth ?? 'api_key',
    api_key: '',
    tenant_id: c?.tenant_id ?? '',
    client_id: c?.client_id ?? '',
    client_secret: '',
    discovery: c?.discovery ?? 'project',
    project_endpoint: c?.project_endpoint ?? '',
    subscription_id: c?.subscription_id ?? '',
    resource_group: c?.resource_group ?? '',
    account_name: c?.account_name ?? '',
    deployments: c?.deployments ?? [],
    api_style: c?.api_style ?? 'v1',
    api_version: '',
  };
}

function toSecret(f: SecretForm): ProviderSecretInput {
  const opt = (v: string) => v.trim() || null;
  return {
    endpoint: f.endpoint.trim(),
    auth: f.auth,
    api_key: opt(f.api_key),
    tenant_id: opt(f.tenant_id),
    client_id: opt(f.client_id),
    client_secret: opt(f.client_secret),
    discovery: f.discovery,
    project_endpoint: f.discovery === 'project' ? opt(f.project_endpoint) : null,
    subscription_id: f.discovery === 'arm' ? opt(f.subscription_id) : null,
    resource_group: f.discovery === 'arm' ? opt(f.resource_group) : null,
    account_name: f.discovery === 'arm' ? opt(f.account_name) : null,
    deployments: f.discovery === 'static' ? f.deployments : null,
    api_style: f.api_style,
    api_version: f.api_style === 'deployments' ? opt(f.api_version) : null,
    keep_existing_secrets: true,
  };
}

function SecretFields({ form, set, editing }: { form: SecretForm; set: <K extends keyof SecretForm>(k: K, v: SecretForm[K]) => void; editing: boolean }) {
  const needsSp = form.auth === 'service_principal' || form.discovery === 'project' || form.discovery === 'arm';
  const keep = editing ? 'Deixe vazio para manter o valor guardado no Vault.' : undefined;
  return (
    <>
      <Field label="Endpoint do recurso" required span={2} hint="https://<recurso>.services.ai.azure.com ou https://<recurso>.openai.azure.com">
        <Input value={form.endpoint} placeholder="https://meu-recurso.services.ai.azure.com" onChange={(e) => set('endpoint', e.target.value)} />
      </Field>
      <Field label="Autenticação nas chamadas" span={2}>
        <Segmented
          value={form.auth}
          onChange={(v) => set('auth', v)}
          options={[
            { value: 'api_key', label: 'Chave de API' },
            { value: 'service_principal', label: 'Service principal' },
          ]}
        />
      </Field>
      {form.auth === 'api_key' && (
        <Field label="Chave de API do recurso" required={!editing} span={2} hint={keep}>
          <Input type="password" autoComplete="off" value={form.api_key} onChange={(e) => set('api_key', e.target.value)} />
        </Field>
      )}

      <FormSection>Descoberta dos deployments</FormSection>
      <Field label="Como listar os modelos" span={2}>
        <Select value={form.discovery} onChange={(e) => set('discovery', e.target.value as SecretForm['discovery'])}>
          <option value="project">Projeto do Foundry (recomendado)</option>
          <option value="arm">Azure Resource Manager (conta de IA)</option>
          <option value="static">Lista fixa de deployments</option>
          <option value="none">Nenhuma: cadastro manual dos modelos</option>
        </Select>
      </Field>
      {form.discovery === 'project' && (
        <Field label="Endpoint do projeto" required span={2} hint="Na página do projeto no portal do Foundry. O service principal precisa de acesso ao projeto (ex.: papel Azure AI User).">
          <Input
            value={form.project_endpoint}
            placeholder="https://meu-recurso.services.ai.azure.com/api/projects/meu-projeto"
            onChange={(e) => set('project_endpoint', e.target.value)}
          />
        </Field>
      )}
      {form.discovery === 'arm' && (
        <>
          <Field label="Subscription" required>
            <Input value={form.subscription_id} onChange={(e) => set('subscription_id', e.target.value)} />
          </Field>
          <Field label="Resource group" required>
            <Input value={form.resource_group} onChange={(e) => set('resource_group', e.target.value)} />
          </Field>
          <Field label="Nome da conta" required span={2} hint="O service principal precisa de leitura na conta (ex.: papel Reader).">
            <Input value={form.account_name} onChange={(e) => set('account_name', e.target.value)} />
          </Field>
        </>
      )}
      {form.discovery === 'static' && (
        <Field label="Deployments" required span={2} hint="Nomes exatos dos deployments no Foundry.">
          <TagInput value={form.deployments} onChange={(v) => set('deployments', v)} placeholder="ex.: gpt-4.1" />
        </Field>
      )}
      {needsSp && (
        <>
          <FormSection>Service principal (Entra ID)</FormSection>
          <Field label="Tenant" required>
            <Input value={form.tenant_id} onChange={(e) => set('tenant_id', e.target.value)} />
          </Field>
          <Field label="Client id" required>
            <Input value={form.client_id} onChange={(e) => set('client_id', e.target.value)} />
          </Field>
          <Field label="Client secret" required={!editing} span={2} hint={keep}>
            <Input type="password" autoComplete="off" value={form.client_secret} onChange={(e) => set('client_secret', e.target.value)} />
          </Field>
        </>
      )}
      <FormSection>Avançado</FormSection>
      <Field label="Formato da API" hint="v1 dispensa api-version e atende modelos OpenAI e de outros fornecedores.">
        <Select value={form.api_style} onChange={(e) => set('api_style', e.target.value as SecretForm['api_style'])}>
          <option value="v1">v1 (/openai/v1)</option>
          <option value="deployments">Clássico (/openai/deployments/…)</option>
        </Select>
      </Field>
      {form.api_style === 'deployments' && (
        <Field label="api-version">
          <Input value={form.api_version} placeholder="2024-10-21" onChange={(e) => set('api_version', e.target.value)} />
        </Field>
      )}
    </>
  );
}

function ProviderDialog({ provider, onClose }: { provider: Provider | null; onClose: () => void }) {
  const editing = !!provider;
  const [name, setName] = useState('');
  const [displayName, setDisplayName] = useState(provider?.display_name ?? '');
  const [source, setSource] = useState<'form' | 'vault'>('form');
  const [vaultPath, setVaultPath] = useState('');
  const [form, setForm] = useState<SecretForm>(() => secretForm(provider));
  const [touched, setTouched] = useState(false);
  const create = useCreateProvider();
  const updateSecret = useUpdateProviderSecret();
  const update = useUpdateProvider();
  const set = <K extends keyof SecretForm>(k: K, v: SecretForm[K]) => setForm((f) => ({ ...f, [k]: v }));
  const nameError = touched && !/^[a-z0-9][a-z0-9-]{1,62}$/.test(name) ? 'Use letras minúsculas, números e hífen (ex.: foundry-eastus).' : undefined;

  const submit = async () => {
    setTouched(true);
    if (editing) {
      if (displayName !== (provider.display_name ?? '')) await update.mutateAsync({ id: provider.id, display_name: displayName || null });
      const result = await updateSecret.mutateAsync({ id: provider.id, secret: toSecret(form) });
      toast[result.status === 'ok' ? 'success' : 'warning'](
        'Credencial atualizada no Vault',
        result.status === 'ok' ? 'Conexão testada com sucesso.' : (result.last_error ?? 'Confira os dados.'),
      );
      onClose();
      return;
    }
    if (nameError || !/^[a-z0-9][a-z0-9-]{1,62}$/.test(name)) return;
    const created = await create.mutateAsync({
      name,
      display_name: displayName || null,
      ...(source === 'vault' ? { vault_path: vaultPath.trim() } : { secret: toSecret(form) }),
    });
    toast[created.status === 'ok' ? 'success' : 'warning'](
      'Provedor cadastrado',
      created.status === 'ok' ? 'Agora sincronize para trazer os deployments.' : (created.last_error ?? 'A conexão falhou.'),
    );
    onClose();
  };

  return (
    <Dialog
      open
      onClose={onClose}
      size="lg"
      title={editing ? `Credencial de ${provider.display_name || provider.name}` : 'Novo provedor do Foundry'}
      description={
        editing
          ? `Gravada no Vault em ${provider.vault_path}. O agente é recriado na hora.`
          : 'Cada provedor é um recurso do Microsoft Foundry. A credencial fica só no Vault.'
      }
      onSubmit={() => void submit()}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancelar
          </Button>
          <Button type="submit" variant="primary" loading={create.isPending || updateSecret.isPending}>
            {editing ? 'Salvar e testar' : 'Cadastrar e testar'}
          </Button>
        </>
      }
    >
      <FormGrid>
        {!editing && (
          <Field label="Identificador" required error={nameError} hint="Aparece nos nomes de modelos repetidos entre provedores.">
            <Input value={name} placeholder="foundry-eastus" onChange={(e) => setName(e.target.value.toLowerCase())} />
          </Field>
        )}
        <Field label="Nome de exibição">
          <Input value={displayName} placeholder="Foundry East US" onChange={(e) => setDisplayName(e.target.value)} />
        </Field>
        {!editing && (
          <Field label="Credencial" span={2}>
            <Segmented
              value={source}
              onChange={setSource}
              options={[
                { value: 'form', label: 'Preencher aqui (o gateway grava no Vault)' },
                { value: 'vault', label: 'Já está no Vault' },
              ]}
            />
          </Field>
        )}
        {!editing && source === 'vault' ? (
          <>
            <Field label="Caminho no Vault (KV v2)" required span={2} hint="Relativo ao mount configurado em LLMGW_VAULT_MOUNT.">
              <Input value={vaultPath} placeholder="llmgw/providers/foundry" onChange={(e) => setVaultPath(e.target.value)} />
            </Field>
            <div style={{ gridColumn: '1 / -1' }} className="bt-stack-sm">
              <span className="bt-small bt-muted">Formato esperado do segredo (campos de descoberta são opcionais):</span>
              <CodeBlock>{VAULT_EXAMPLE}</CodeBlock>
            </div>
          </>
        ) : (
          <SecretFields form={form} set={set} editing={editing} />
        )}
      </FormGrid>
    </Dialog>
  );
}

// ---------------------------------------------------------------- cartão
function ProviderCard({ provider, onEdit }: { provider: Provider; onEdit: () => void }) {
  const confirm = useConfirm();
  const check = useCheckProvider();
  const sync = useSyncProvider();
  const update = useUpdateProvider();
  const remove = useDeleteProvider();
  const status = providerStatus(provider);

  const runCheck = async () => {
    const r = await check.mutateAsync(provider.id);
    toast[r.ok ? 'success' : 'error'](r.ok ? 'Conexão ok' : 'Falha na conexão', r.message);
  };
  const runSync = async () => {
    const r = await sync.mutateAsync(provider.id);
    toast.success('Deployments sincronizados', `${r.found} encontrado(s): ${r.created} novo(s), ${r.missing} indisponível(is), ${r.skipped} ignorado(s) (imagem/áudio).`);
  };
  const del = async () => {
    const { confirmed } = await confirm({
      title: `Excluir o provedor ${provider.name}?`,
      description: provider.managed_secret
        ? `Os ${provider.models_total} modelo(s) dele saem do gateway e a credencial é apagada do Vault.`
        : `Os ${provider.models_total} modelo(s) dele saem do gateway. A credencial em ${provider.vault_path} continua no Vault.`,
      confirmLabel: 'Excluir provedor',
    });
    if (!confirmed) return;
    await remove.mutateAsync(provider.id);
    toast.success('Provedor excluído', provider.name);
  };

  return (
    <Card
      box
      title={
        <div className="bt-row">
          <IconTile size={36}>
            <Cloud size={18} />
          </IconTile>
          <div>
            <div>{provider.display_name || provider.name}</div>
            {provider.display_name && <div className="bt-mono bt-subtle bt-small">{provider.name}</div>}
          </div>
        </div>
      }
      actions={
        <Menu
          label={`Ações do provedor ${provider.name}`}
          items={[
            { label: 'Editar credencial', icon: <Pencil size={16} />, hidden: !provider.managed_secret, onSelect: onEdit },
            {
              label: provider.enabled ? 'Desabilitar' : 'Habilitar',
              icon: <Power size={16} />,
              onSelect: async () => {
                await update.mutateAsync({ id: provider.id, enabled: !provider.enabled });
                toast.success(provider.enabled ? 'Provedor desabilitado' : 'Provedor habilitado', provider.name);
              },
            },
            'separator',
            { label: 'Excluir', icon: <Trash2 size={16} />, danger: true, onSelect: () => void del() },
          ]}
        />
      }
      footer={
        <div className="bt-row-wrap">
          <Button size="sm" variant="secondary" icon={<Plug size={14} />} loading={check.isPending} onClick={() => void runCheck()}>
            Testar conexão
          </Button>
          <Button
            size="sm"
            variant="primary"
            icon={<RefreshCw size={14} />}
            loading={sync.isPending}
            disabled={!provider.enabled || provider.discovery === 'none'}
            onClick={() => void runSync()}
          >
            Sincronizar deployments
          </Button>
        </div>
      }
    >
      <div className="bt-stack">
        <div className="bt-row-wrap">
          <ProviderStatusBadge status={status} />
          <Tag variant={provider.managed_secret ? 'green' : 'outline'} icon={<KeyRound size={12} />}>
            {provider.managed_secret ? 'Credencial gravada pelo console' : 'Credencial provisionada no Vault'}
          </Tag>
        </div>
        {provider.last_error && status === 'error' && <Callout tone="danger">{provider.last_error}</Callout>}
        <KeyValue
          items={[
            ['Endpoint', <span key="e" className="bt-mono bt-small">{provider.endpoint ?? '—'}</span>],
            ['Autenticação', provider.auth_type ? AUTH_LABEL[provider.auth_type] ?? provider.auth_type : '—'],
            ['Descoberta', provider.discovery ? DISCOVERY_LABEL[provider.discovery] ?? provider.discovery : '—'],
            ['Vault', <InlineCode key="v">{provider.vault_path}</InlineCode>],
            ['Modelos', `${provider.models_enabled} habilitado(s) de ${provider.models_total}`],
            ['Última sincronização', <RelativeTime key="s" value={provider.last_sync_at} fallback="Nunca" />],
            ['Credencial lida em', <RelativeTime key="l" value={provider.secret_loaded_at} fallback="—" />],
          ]}
        />
      </div>
    </Card>
  );
}

function ProvidersContent() {
  const providers = useProviders();
  const [dialog, setDialog] = useState<{ open: boolean; provider: Provider | null }>({ open: false, provider: null });
  return (
    <Page>
      <PageHeader
        crumbs={[{ label: 'Início', href: '/' }, { label: 'Administração' }, { label: 'Provedores' }]}
        title="Provedores"
        description="Recursos do Microsoft Foundry ligados ao gateway. A credencial é lida do Vault e relida a cada minuto: uma rotação no Vault vale sem reiniciar."
        actions={
          <Button variant="primary" icon={<Plus size={16} />} onClick={() => setDialog({ open: true, provider: null })}>
            Novo provedor
          </Button>
        }
      />
      {providers.data && !providers.data.length ? (
        <Card>
          <EmptyState
            icon={<Cloud size={24} />}
            title="Nenhum provedor cadastrado"
            description="Cadastre um recurso do Foundry pelo console ou aponte LLMGW_BOOTSTRAP_PROVIDERS para um segredo no Vault."
            action={
              <Button variant="primary" icon={<Plus size={16} />} onClick={() => setDialog({ open: true, provider: null })}>
                Novo provedor
              </Button>
            }
          />
        </Card>
      ) : (
        <Grid layout="2">
          {providers.data?.map((p) => (
            <ProviderCard key={p.id} provider={p} onEdit={() => setDialog({ open: true, provider: p })} />
          ))}
        </Grid>
      )}
      {dialog.open && <ProviderDialog provider={dialog.provider} onClose={() => setDialog({ open: false, provider: null })} />}
    </Page>
  );
}

export function ProvidersPage() {
  return (
    <AdminOnly>
      <ProvidersContent />
    </AdminOnly>
  );
}
