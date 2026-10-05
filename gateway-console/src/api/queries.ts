// Chaves e hooks de leitura/escrita (react-query) da API do console.

import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, type Query } from './client';
import type {
  ApiKey,
  ApiKeyCreated,
  AuditEntry,
  CheckResult,
  Me,
  Model,
  Page,
  Provider,
  ProviderSecretInput,
  Summary,
  SyncResult,
  UsageRow,
  User,
} from './types';

export const qk = {
  me: ['me'] as const,
  keys: (userId?: string) => ['keys', userId ?? 'me'] as const,
  models: (all: boolean) => ['models', all] as const,
  providers: ['providers'] as const,
  users: (q: Query) => ['users', q] as const,
  user: (id: string) => ['user', id] as const,
  usage: (q: Query) => ['usage', q] as const,
  summary: (q: Query) => ['summary', q] as const,
  audit: (q: Query) => ['audit', q] as const,
};

// ---------------------------------------------------------------- sessão
export function useMe() {
  return useQuery({ queryKey: qk.me, queryFn: () => api.get<Me>('/me'), staleTime: 30_000, retry: 1 });
}

// ---------------------------------------------------------------- chaves
export function useKeys(userId?: string) {
  return useQuery({
    queryKey: qk.keys(userId),
    queryFn: () => api.get<ApiKey[]>('/keys', userId ? { user_id: userId } : undefined),
  });
}

export interface KeyInput {
  name: string;
  expires_in_days?: number | null;
  monthly_budget_usd?: number | null;
  rpm_limit?: number | null;
  allowed_models?: string[] | null;
}

export function useCreateKey(userId?: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: KeyInput) =>
      api.post<ApiKeyCreated>(userId ? `/keys?user_id=${encodeURIComponent(userId)}` : '/keys', body),
    onSuccess: () => client.invalidateQueries({ queryKey: ['keys'] }),
  });
}

export function useUpdateKey() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...body }: Partial<KeyInput> & { id: string }) => api.patch<ApiKey>(`/keys/${id}`, body),
    onSuccess: () => client.invalidateQueries({ queryKey: ['keys'] }),
  });
}

export function useRevokeKey() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.post<ApiKey>(`/keys/${id}/revoke`),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['keys'] });
      void client.invalidateQueries({ queryKey: ['users'] });
    },
  });
}

// ---------------------------------------------------------------- modelos
export function useModels(all = false) {
  return useQuery({
    queryKey: qk.models(all),
    queryFn: () => api.get<Model[]>('/models', all ? { all: true } : undefined),
  });
}

export type ModelInput = Partial<
  Pick<
    Model,
    | 'name'
    | 'kind'
    | 'enabled'
    | 'description'
    | 'input_price'
    | 'cached_input_price'
    | 'output_price'
    | 'cache_ttl_s'
    | 'allowed_groups'
    | 'context_window'
    | 'max_output_tokens'
    | 'supports_responses'
    | 'supports_vision'
    | 'supports_reasoning'
  >
> & { provider_id?: string; deployment?: string };

export function useSaveModel() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...body }: ModelInput & { id?: string }) =>
      id ? api.patch<Model>(`/models/${id}`, body) : api.post<Model>('/models', body),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['models'] });
      void client.invalidateQueries({ queryKey: qk.providers });
    },
  });
}

export function useDeleteModel() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`/models/${id}`),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['models'] });
      void client.invalidateQueries({ queryKey: qk.providers });
    },
  });
}

export function useClearCache() {
  return useMutation({ mutationFn: () => api.post<{ removed: number }>('/cache/clear') });
}

// ---------------------------------------------------------------- provedores
export function useProviders(enabled = true) {
  return useQuery({ queryKey: qk.providers, queryFn: () => api.get<Provider[]>('/providers'), enabled });
}

export interface ProviderCreateInput {
  name: string;
  display_name?: string | null;
  vault_path?: string | null;
  secret?: ProviderSecretInput | null;
}

function invalidateProviders(client: ReturnType<typeof useQueryClient>) {
  void client.invalidateQueries({ queryKey: qk.providers });
  void client.invalidateQueries({ queryKey: ['models'] });
}

export function useCreateProvider() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: ProviderCreateInput) => api.post<Provider>('/providers', body),
    onSuccess: () => invalidateProviders(client),
  });
}

export function useUpdateProvider() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...body }: { id: string; display_name?: string | null; enabled?: boolean }) =>
      api.patch<Provider>(`/providers/${id}`, body),
    onSuccess: () => invalidateProviders(client),
  });
}

export function useUpdateProviderSecret() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, secret }: { id: string; secret: ProviderSecretInput }) =>
      api.put<Provider>(`/providers/${id}/secret`, secret),
    onSuccess: () => invalidateProviders(client),
  });
}

export function useDeleteProvider() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`/providers/${id}`),
    onSuccess: () => invalidateProviders(client),
  });
}

export function useCheckProvider() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.post<CheckResult>(`/providers/${id}/check`),
    onSettled: () => invalidateProviders(client),
  });
}

export function useSyncProvider() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.post<SyncResult>(`/providers/${id}/sync`),
    onSettled: () => invalidateProviders(client),
  });
}

// ---------------------------------------------------------------- usuários
export function useUsers(query: Query, enabled = true) {
  return useQuery({
    queryKey: qk.users(query),
    queryFn: () => api.get<Page<User>>('/users', query),
    placeholderData: keepPreviousData,
    enabled,
  });
}

export function useUser(id: string | null) {
  return useQuery({
    queryKey: qk.user(id ?? ''),
    queryFn: () => api.get<User>(`/users/${id}`),
    enabled: !!id,
  });
}

export interface UserInput {
  username?: string;
  display_name?: string | null;
  role?: 'user' | 'admin';
  status?: 'active' | 'blocked';
  status_reason?: string | null;
  monthly_budget_usd?: number | null;
  rpm_limit?: number | null;
  tpm_limit?: number | null;
  allowed_models?: string[] | null;
  groups?: string[];
}

function invalidateUsers(client: ReturnType<typeof useQueryClient>) {
  void client.invalidateQueries({ queryKey: ['users'] });
  void client.invalidateQueries({ queryKey: ['user'] });
  void client.invalidateQueries({ queryKey: qk.me });
}

export function useSaveUser() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...body }: UserInput & { id?: string }) =>
      id ? api.patch<User>(`/users/${id}`, body) : api.post<User>('/users', body),
    onSuccess: () => invalidateUsers(client),
  });
}

export function useLdapSyncUser() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.post<User>(`/users/${id}/ldap-sync`),
    onSuccess: () => invalidateUsers(client),
  });
}

// ---------------------------------------------------------------- uso
export function useUsage(query: Query) {
  return useQuery({
    queryKey: qk.usage(query),
    queryFn: () => api.get<Page<UsageRow>>('/usage', query),
    placeholderData: keepPreviousData,
    refetchInterval: 15_000,
  });
}

export function useSummary(query: Query) {
  return useQuery({
    queryKey: qk.summary(query),
    queryFn: () => api.get<Summary>('/usage/summary', query),
    placeholderData: keepPreviousData,
    refetchInterval: 30_000,
  });
}

// ---------------------------------------------------------------- auditoria
export function useAudit(query: Query) {
  return useQuery({
    queryKey: qk.audit(query),
    queryFn: () => api.get<Page<AuditEntry>>('/audit', query),
    placeholderData: keepPreviousData,
  });
}
