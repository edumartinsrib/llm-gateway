import {
  AppShell,
  Brand,
  Button,
  FullScreen,
  FullScreenLoader,
  ProgressBar,
  SidebarStatus,
  StatusCard,
  UserMenu,
  type NavEntry,
} from 'broto-ui';
import {
  BookOpen,
  Boxes,
  ChartColumnBig,
  Cloud,
  KeyRound,
  LayoutDashboard,
  LogOut,
  MessagesSquare,
  RefreshCw,
  ScrollText,
  Users,
} from 'lucide-react';
import { Navigate, Outlet, useLocation, useNavigate } from 'react-router';
import { isApiError } from '../api/client';
import { useMe } from '../api/queries';
import type { Me } from '../api/types';
import { budgetPct, formatPeriod, formatUsd } from '../lib/format';
import { getToken, signOut } from '../lib/session';
import { MeContext, useConfig } from './context';

function navFor(me: Me): NavEntry[] {
  const nav: NavEntry[] = [
    { label: 'Visão geral', href: '/', icon: <LayoutDashboard size={20} />, end: true },
    { label: 'Playground', href: '/playground', icon: <MessagesSquare size={20} /> },
    { label: 'Modelos', href: '/modelos', icon: <Boxes size={20} /> },
    { label: 'Chaves de API', href: '/chaves', icon: <KeyRound size={20} /> },
    { label: 'Uso e custos', href: '/uso', icon: <ChartColumnBig size={20} /> },
    { label: 'Como usar', href: '/como-usar', icon: <BookOpen size={20} /> },
  ];
  if (me.is_admin) {
    nav.push(
      { heading: 'Administração' },
      { label: 'Usuários', href: '/admin/usuarios', icon: <Users size={20} /> },
      { label: 'Provedores', href: '/admin/provedores', icon: <Cloud size={20} /> },
      { label: 'Auditoria', href: '/admin/auditoria', icon: <ScrollText size={20} /> },
    );
  }
  return nav;
}

function BudgetPill({ me }: { me: Me }) {
  const pct = budgetPct(me.spent_month_usd, me.monthly_budget_usd);
  return (
    <div className="gw-budget" title={`Gasto em ${formatPeriod(me.period)}`}>
      <span className="gw-budget-label">Gasto no mês</span>
      <span className="gw-budget-value">
        {formatUsd(me.spent_month_usd)}
        {me.monthly_budget_usd !== null && <span className="bt-subtle"> de {formatUsd(me.monthly_budget_usd)}</span>}
      </span>
      {pct !== null && (
        <ProgressBar value={pct} height={4} tone={pct >= 100 ? 'danger' : pct >= 80 ? 'warning' : 'default'} label="Orçamento do mês" />
      )}
    </div>
  );
}

export function AuthenticatedLayout() {
  const location = useLocation();
  const navigate = useNavigate();
  const config = useConfig();
  const token = getToken();
  const me = useMe();

  if (!token) {
    const next = location.pathname + location.search;
    return <Navigate to={`/login${next && next !== '/' ? `?next=${encodeURIComponent(next)}` : ''}`} replace />;
  }
  if (me.isPending) return <FullScreenLoader brand={<Brand name="Gateway LLM" tone="dark" />} label="Abrindo o console…" />;
  if (me.isError) {
    const forbidden = isApiError(me.error) && me.error.status === 403;
    return (
      <FullScreen>
        <StatusCard
          brand={<Brand name="Gateway LLM" tone="dark" />}
          tone="danger"
          title={forbidden ? 'Acesso suspenso' : 'Não foi possível abrir o console'}
          description={me.error instanceof Error ? me.error.message : 'Falha ao carregar o usuário.'}
          action={
            forbidden ? (
              <Button variant="primary" icon={<LogOut size={16} />} onClick={() => void signOut()}>
                Sair
              </Button>
            ) : (
              <Button variant="primary" icon={<RefreshCw size={16} />} onClick={() => void me.refetch()}>
                Tentar de novo
              </Button>
            )
          }
        />
      </FullScreen>
    );
  }

  const user = me.data;
  return (
    <MeContext.Provider value={user}>
      <AppShell
        brand={<Brand name="Gateway LLM" />}
        brandHref="/"
        nav={navFor(user)}
        currentPath={location.pathname}
        storageKey="llmgw.sidebar"
        sidebarFooter={<SidebarStatus live label="Gateway no ar" detail={`v${config.version}`} />}
        topbarStart={<BudgetPill me={user} />}
        topbarEnd={
          <UserMenu
            name={user.display_name || user.username}
            role={user.is_admin ? 'Administrador' : 'Usuário'}
            items={[
              { label: 'Minhas chaves', icon: <KeyRound size={16} />, onSelect: () => navigate('/chaves') },
              { label: 'Meu uso', icon: <ChartColumnBig size={16} />, onSelect: () => navigate('/uso') },
              'separator',
              { label: 'Sair', icon: <LogOut size={16} />, onSelect: () => void signOut() },
            ]}
          />
        }
      >
        <Outlet />
      </AppShell>
    </MeContext.Provider>
  );
}
