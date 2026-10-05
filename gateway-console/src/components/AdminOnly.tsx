import type { ReactNode } from 'react';
import { NotFoundPage } from '../pages/NotFoundPage';
import { useCurrentUser } from './context';

/** Telas de administração: quem não é administrador vê o 404. */
export function AdminOnly({ children }: { children: ReactNode }) {
  const me = useCurrentUser();
  return me.is_admin ? <>{children}</> : <NotFoundPage inShell />;
}
