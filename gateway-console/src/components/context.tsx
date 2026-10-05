import { createContext, useContext } from 'react';
import type { Config, Me } from '../api/types';

export const ConfigContext = createContext<Config | null>(null);

export function useConfig(): Config {
  const ctx = useContext(ConfigContext);
  if (!ctx) throw new Error('ConfigContext ausente');
  return ctx;
}

export const MeContext = createContext<Me | null>(null);

/** Usuário logado (disponível dentro do layout autenticado). */
export function useCurrentUser(): Me {
  const ctx = useContext(MeContext);
  if (!ctx) throw new Error('MeContext ausente');
  return ctx;
}
