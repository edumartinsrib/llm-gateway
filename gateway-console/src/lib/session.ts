// Sessão do console: JWT emitido pela API no login, guardado na aba (sessionStorage).

const TOKEN_KEY = 'llmgw.session';

function storage(): Storage | null {
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

export function getToken(): string | null {
  return storage()?.getItem(TOKEN_KEY) ?? null;
}

export function setToken(token: string): void {
  storage()?.setItem(TOKEN_KEY, token);
}

export function clearSession(): void {
  storage()?.removeItem(TOKEN_KEY);
}

/** Encerra a sessão no servidor (todas as abas) e volta ao login. */
export async function signOut(): Promise<void> {
  const token = getToken();
  if (token) {
    try {
      await fetch('/api/v1/auth/logout', { method: 'POST', headers: { Authorization: `Bearer ${token}` } });
    } catch {
      // Sem rede: a sessão expira sozinha; o importante é sair desta aba.
    }
  }
  clearSession();
  window.location.assign('/login');
}

/** Destino seguro depois do login: só caminhos deste mesmo site. */
export function safeNext(next: string | null): string {
  if (!next) return '/';
  try {
    const url = new URL(next, window.location.origin);
    if (url.origin !== window.location.origin) return '/';
    return url.pathname + url.search + url.hash;
  } catch {
    return '/';
  }
}
