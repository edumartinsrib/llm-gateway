import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ConfirmProvider, PageLoading, Toaster, UIProvider, toast } from 'broto-ui';
import { lazy, Suspense, useState, type ComponentType } from 'react';
import { BrowserRouter, Outlet, Route, Routes } from 'react-router';
import { isApiError } from './api/client';
import type { Config } from './api/types';
import { ConfigContext } from './components/context';
import { AuthenticatedLayout } from './components/Layout';
import { RouterLink } from './components/RouterLink';
import { LoginPage } from './pages/LoginPage';
import { NotFoundPage } from './pages/NotFoundPage';

declare module '@tanstack/react-query' {
  interface Register {
    queryMeta: { silent?: boolean };
    mutationMeta: { silent?: boolean };
  }
}

function page<T extends Record<string, unknown>>(loader: () => Promise<T>, name: keyof T) {
  return lazy(async () => ({ default: (await loader())[name] as ComponentType }));
}

const DashboardPage = page(() => import('./pages/DashboardPage'), 'DashboardPage');
const PlaygroundPage = page(() => import('./pages/PlaygroundPage'), 'PlaygroundPage');
const ModelsPage = page(() => import('./pages/ModelsPage'), 'ModelsPage');
const KeysPage = page(() => import('./pages/KeysPage'), 'KeysPage');
const UsagePage = page(() => import('./pages/UsagePage'), 'UsagePage');
const QuickstartPage = page(() => import('./pages/QuickstartPage'), 'QuickstartPage');
const UsersPage = page(() => import('./pages/admin/UsersPage'), 'UsersPage');
const ProvidersPage = page(() => import('./pages/admin/ProvidersPage'), 'ProvidersPage');
const AuditPage = page(() => import('./pages/admin/AuditPage'), 'AuditPage');

function errorMessage(error: unknown): { title: string; details?: string[] } {
  if (isApiError(error)) return { title: error.detail, details: error.errors.length ? error.errors : undefined };
  return { title: error instanceof Error ? error.message : 'Algo deu errado.' };
}

function makeQueryClient() {
  return new QueryClient({
    queryCache: new QueryCache({
      onError: (error, query) => {
        // Erros da primeira carga aparecem na própria tela; aqui só avisamos
        // quando uma atualização em segundo plano falha.
        if (query.meta?.silent || query.state.data === undefined) return;
        if (isApiError(error) && error.status === 401) return;
        toast.error('Falha ao atualizar os dados', errorMessage(error).title);
      },
    }),
    mutationCache: new MutationCache({
      onError: (error, _vars, _ctx, mutation) => {
        if (mutation.meta?.silent) return;
        if (isApiError(error) && error.status === 401) return;
        const { title, details } = errorMessage(error);
        toast.error(title, { details });
      },
    }),
    defaultOptions: {
      queries: {
        staleTime: 15_000,
        refetchOnWindowFocus: false,
        retry: (count, error) => {
          if (isApiError(error) && [400, 401, 403, 404, 422].includes(error.status)) return false;
          return count < 2;
        },
      },
    },
  });
}

function PageSuspense() {
  return (
    <Suspense fallback={<PageLoading label="Abrindo a tela…" />}>
      <Outlet />
    </Suspense>
  );
}

export function App({ config }: { config: Config }) {
  const [client] = useState(makeQueryClient);
  return (
    <ConfigContext.Provider value={config}>
      <QueryClientProvider client={client}>
        <UIProvider linkComponent={RouterLink}>
          <ConfirmProvider>
            <BrowserRouter>
              <Routes>
                <Route path="/login" element={<LoginPage />} />
                <Route element={<AuthenticatedLayout />}>
                  <Route element={<PageSuspense />}>
                    <Route index element={<DashboardPage />} />
                    <Route path="playground" element={<PlaygroundPage />} />
                    <Route path="modelos" element={<ModelsPage />} />
                    <Route path="chaves" element={<KeysPage />} />
                    <Route path="uso" element={<UsagePage />} />
                    <Route path="como-usar" element={<QuickstartPage />} />
                    <Route path="admin/usuarios" element={<UsersPage />} />
                    <Route path="admin/provedores" element={<ProvidersPage />} />
                    <Route path="admin/auditoria" element={<AuditPage />} />
                    <Route path="*" element={<NotFoundPage inShell />} />
                  </Route>
                </Route>
              </Routes>
            </BrowserRouter>
            <Toaster />
          </ConfirmProvider>
        </UIProvider>
      </QueryClientProvider>
    </ConfigContext.Provider>
  );
}
