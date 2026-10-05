import { AuthCard, AuthLayout, BoxMark, Brand, Button, Callout, Field, Input } from 'broto-ui';
import { LogIn } from 'lucide-react';
import { useState } from 'react';
import { Navigate, useSearchParams } from 'react-router';
import { isApiError, request } from '../api/client';
import type { LoginResult } from '../api/types';
import { useConfig } from '../components/context';
import { getToken, safeNext, setToken } from '../lib/session';

export function LoginPage() {
  const config = useConfig();
  const [params] = useSearchParams();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const target = safeNext(params.get('next'));
  const ldap = config.auth.mode === 'ldap';

  if (getToken()) return <Navigate to={target} replace />;

  const submit = async () => {
    if (!username.trim() || (ldap && !password)) {
      setError(ldap ? 'Informe usuário e senha.' : 'Informe o usuário.');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await request<LoginResult>('POST', '/auth/login', {
        anonymous: true,
        body: { username: username.trim(), password: ldap ? password : null },
      });
      setToken(result.token);
      // Recarrega para iniciar caches limpos com o novo usuário.
      window.location.assign(target);
    } catch (e) {
      setError(isApiError(e) ? e.detail : 'Não foi possível entrar. Tente de novo.');
      setBusy(false);
    }
  };

  return (
    <AuthLayout
      brand={<Brand name="Gateway LLM" markSize={44} nameSize={28} />}
      tagline="Os modelos do Microsoft Foundry numa API compatível com a OpenAI, com chaves por pessoa, cache e controle de custos."
      version={`v${config.version}`}
      mobileBrand={<BoxMark size={36} />}
    >
      <AuthCard
        title="Entrar no console"
        description={ldap ? 'Use o seu usuário e a senha da rede.' : 'Escolha um nome de usuário para entrar.'}
        onSubmit={() => void submit()}
      >
        {!ldap && (
          <Callout tone="info" title="Modo de desenvolvimento">
            Sem LDAP: qualquer nome entra sem senha. Em produção, o login usa o diretório da empresa.
          </Callout>
        )}
        {error && <Callout tone="danger">{error}</Callout>}
        <Field label="Usuário" required>
          <Input
            autoFocus
            autoComplete="username"
            value={username}
            placeholder={ldap ? 'ex.: ana.souza' : 'ex.: admin'}
            onChange={(e) => setUsername(e.target.value)}
          />
        </Field>
        {ldap && (
          <Field label="Senha" required>
            <Input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
          </Field>
        )}
        <Button type="submit" variant="primary" size="lg" block loading={busy} icon={<LogIn size={18} />}>
          Entrar
        </Button>
      </AuthCard>
    </AuthLayout>
  );
}
