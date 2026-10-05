// Formatação específica do gateway (dinheiro, tokens, latência, TTL).

import { formatNumber } from 'broto-ui/utils';

const usd = new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** US$ 12,34. Valores pequenos mostram mais casas (US$ 0,0042) e zero vira "US$ 0,00". */
export function formatUsd(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  if (value !== 0 && Math.abs(value) < 0.01) {
    const digits = Math.abs(value) < 0.0001 ? 6 : 4;
    return new Intl.NumberFormat('pt-BR', {
      style: 'currency',
      currency: 'USD',
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    }).format(value);
  }
  return usd.format(value);
}

/** Preço por 1 milhão de tokens: "US$ 2,50". */
export function formatPrice(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  return new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 4 }).format(value);
}

/** 1.234 · 12,3 mil · 4,5 mi */
export function formatTokens(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  if (Math.abs(value) < 10_000) return formatNumber(value);
  return new Intl.NumberFormat('pt-BR', { notation: 'compact', maximumFractionDigits: 1 }).format(value);
}

export function formatMs(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  if (value < 1000) return `${Math.round(value)} ms`;
  return `${(value / 1000).toLocaleString('pt-BR', { maximumFractionDigits: 1 })} s`;
}

export function formatPct(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return `${value.toLocaleString('pt-BR', { maximumFractionDigits: digits })}%`;
}

export function formatTtl(seconds: number): string {
  if (seconds <= 0) return 'Sem cache';
  if (seconds % 86400 === 0) return `${seconds / 86400} d`;
  if (seconds % 3600 === 0) return `${seconds / 3600} h`;
  if (seconds % 60 === 0) return `${seconds / 60} min`;
  return `${seconds} s`;
}

/** "2026-10" → "outubro de 2026". */
export function formatPeriod(period: string): string {
  const [y, m] = period.split('-').map(Number);
  if (!y || !m) return period;
  return new Date(Date.UTC(y, m - 1, 15)).toLocaleDateString('pt-BR', { month: 'long', year: 'numeric', timeZone: 'UTC' });
}

/** "2026-10-04" → "04/10". */
export function formatDay(day: string): string {
  const [, m, d] = day.split('-');
  return d && m ? `${d}/${m}` : day;
}

export function budgetPct(spent: number, budget: number | null): number | null {
  if (budget === null || budget === undefined) return null;
  if (budget <= 0) return 100;
  return Math.min(100, (spent / budget) * 100);
}

export function firstName(name: string | null | undefined, fallback: string): string {
  const base = (name || fallback).trim();
  return base.split(/[\s.]+/)[0] || base;
}
