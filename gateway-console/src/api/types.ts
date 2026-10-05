// Tipos da API do console (ver docs/api.md e /api/v1/openapi.json).

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface Config {
  auth: { mode: 'dev' | 'ldap' };
  version: string;
  api_base_url: string;
  cache: { enabled: boolean; scope: 'user' | 'global'; chat_ttl_s: number; embeddings_ttl_s: number };
  timezone: string;
  budget_alert_pct: number;
}

export type Role = 'user' | 'admin';
export type UserStatus = 'active' | 'blocked' | 'disabled';

export interface Me {
  id: string;
  username: string;
  display_name: string | null;
  email: string | null;
  groups: string[];
  role: Role;
  is_admin: boolean;
  status: UserStatus;
  monthly_budget_usd: number | null;
  spent_month_usd: number;
  period: string;
  rpm_limit: number | null;
  tpm_limit: number | null;
  allowed_models: string[] | null;
  last_login_at: string | null;
}

export interface LoginResult {
  token: string;
  expires_at: string;
  user: Me;
}

export type KeyStatus = 'active' | 'revoked' | 'expired';

export interface ApiKey {
  id: string;
  name: string;
  display: string;
  user_id: string;
  username: string;
  status: KeyStatus;
  created_at: string;
  expires_at: string | null;
  revoked_at: string | null;
  revoked_by: string | null;
  last_used_at: string | null;
  monthly_budget_usd: number | null;
  spent_month_usd: number;
  rpm_limit: number | null;
  allowed_models: string[] | null;
  limits_locked: boolean;
}

export interface ApiKeyCreated extends ApiKey {
  key: string;
}

export type ModelKind = 'chat' | 'embeddings';

export interface Model {
  id: string;
  name: string;
  provider_id: string;
  provider_name: string;
  deployment: string;
  kind: ModelKind | string;
  upstream_model: string | null;
  upstream_version: string | null;
  publisher: string | null;
  sku: string | null;
  source: 'discovered' | 'manual' | string;
  enabled: boolean;
  available: boolean;
  routable: boolean;
  accessible: boolean;
  description: string | null;
  input_price: number;
  cached_input_price: number | null;
  output_price: number;
  cache_ttl_s: number | null;
  effective_cache_ttl_s: number;
  allowed_groups: string[];
  context_window: number | null;
  max_output_tokens: number | null;
  supports_responses: boolean;
  supports_vision: boolean;
  supports_reasoning: boolean;
  discovered_at: string | null;
  last_seen_at: string | null;
  created_at: string;
  updated_at: string | null;
}

export interface ProviderConfigView {
  endpoint: string;
  auth: 'api_key' | 'service_principal';
  discovery: 'project' | 'arm' | 'static' | 'none';
  api_style: 'v1' | 'deployments';
  project_endpoint: string | null;
  subscription_id: string | null;
  resource_group: string | null;
  account_name: string | null;
  tenant_id: string | null;
  client_id: string | null;
  deployments: string[];
  authority?: string;
  arm_endpoint?: string;
  scope?: string;
  api_version?: string;
  has_api_key: boolean;
  has_client_secret: boolean;
}

export interface Provider {
  id: string;
  name: string;
  display_name: string | null;
  kind: string;
  vault_path: string;
  managed_secret: boolean;
  enabled: boolean;
  endpoint: string | null;
  auth_type: string | null;
  discovery: string | null;
  status: 'ok' | 'error' | 'unknown' | string;
  last_error: string | null;
  last_checked_at: string | null;
  last_sync_at: string | null;
  secret_loaded_at: string | null;
  agent_loaded: boolean;
  models_total: number;
  models_enabled: number;
  config: ProviderConfigView | null;
  created_at: string;
  created_by: string | null;
}

export interface ProviderSecretInput {
  endpoint: string;
  auth: 'api_key' | 'service_principal';
  api_key?: string | null;
  tenant_id?: string | null;
  client_id?: string | null;
  client_secret?: string | null;
  discovery?: 'project' | 'arm' | 'static' | 'none' | null;
  project_endpoint?: string | null;
  subscription_id?: string | null;
  resource_group?: string | null;
  account_name?: string | null;
  deployments?: string[] | null;
  api_style?: 'v1' | 'deployments' | null;
  api_version?: string | null;
  keep_existing_secrets?: boolean;
}

export interface CheckResult {
  ok: boolean;
  message: string;
  deployments: number | null;
}

export interface SyncResult {
  found: number;
  created: number;
  updated: number;
  missing: number;
  skipped: number;
}

export interface User {
  id: string;
  username: string;
  display_name: string | null;
  email: string | null;
  groups: string[];
  role: Role;
  is_admin: boolean;
  status: UserStatus;
  status_reason: string | null;
  monthly_budget_usd: number | null;
  spent_month_usd: number;
  requests_month: number;
  rpm_limit: number | null;
  tpm_limit: number | null;
  allowed_models: string[] | null;
  keys_active: number;
  created_at: string;
  last_login_at: string | null;
  last_seen_at: string | null;
  ldap_synced_at: string | null;
}

export type Endpoint = 'chat' | 'embeddings' | 'responses';

export interface UsageRow {
  id: number;
  ts: string;
  request_id: string;
  user_id: string | null;
  username: string;
  api_key_id: string | null;
  api_key_name: string | null;
  model_name: string;
  provider_name: string | null;
  deployment: string | null;
  endpoint: Endpoint | string;
  stream: boolean;
  status_code: number;
  error_code: string | null;
  error_message: string | null;
  prompt_tokens: number;
  cached_tokens: number;
  completion_tokens: number;
  reasoning_tokens: number;
  total_tokens: number;
  cost_usd: number;
  cache_hit: boolean;
  usage_estimated: boolean;
  saved_usd: number;
  latency_ms: number | null;
  ttft_ms: number | null;
  client_ip: string | null;
  user_agent: string | null;
}

export interface Totals {
  requests: number;
  errors: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cost_usd: number;
  cache_hits: number;
  saved_usd: number;
  avg_latency_ms: number | null;
  p95_latency_ms: number | null;
}

export interface Ranked {
  key: string;
  label: string;
  requests: number;
  tokens: number;
  cost_usd: number;
  cache_hits: number;
}

export interface MonthSpend {
  period: string;
  spent_usd: number;
  budget_usd: number | null;
  requests: number;
  saved_usd: number;
}

export interface Summary {
  days: number;
  scope: 'me' | 'all' | 'user';
  since: string;
  totals: Totals;
  by_day: { day: string; model: string; requests: number; tokens: number; cost_usd: number }[];
  by_model: Ranked[];
  by_user: Ranked[];
  month: MonthSpend;
  global_month: MonthSpend | null;
}

export interface AuditEntry {
  id: number;
  ts: string;
  actor: string;
  actor_type: string;
  action: string;
  resource_type: string;
  resource_id: string | null;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  detail: string | null;
}
