// Fontes servidas pelo próprio console (sem Google Fonts: funciona em rede fechada).
import '@fontsource-variable/exo-2';
import '@fontsource-variable/exo-2/wght-italic.css';
import '@fontsource-variable/nunito';
import 'broto-ui/styles.css';
import './styles/app.css';

import { Brand, Button, FullScreen, StatusCard } from 'broto-ui';
import { RefreshCw } from 'lucide-react';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { request } from './api/client';
import type { Config } from './api/types';
import { App } from './App';

function BootError({ error }: { error: unknown }) {
  return (
    <FullScreen>
      <StatusCard
        brand={<Brand name="Gateway LLM" tone="dark" />}
        tone="danger"
        title="Não foi possível iniciar o console"
        description={
          error instanceof Error
            ? `${error.message} Confira se a API do gateway está no ar e acessível em /api/v1.`
            : 'A API do gateway não respondeu.'
        }
        action={
          <Button variant="primary" icon={<RefreshCw size={16} />} onClick={() => window.location.reload()}>
            Tentar de novo
          </Button>
        }
      />
    </FullScreen>
  );
}

async function boot() {
  const container = document.getElementById('root');
  if (!container) throw new Error('#root ausente');
  const root = createRoot(container);
  let config: Config;
  try {
    config = await request<Config>('GET', '/config', { anonymous: true });
  } catch (error) {
    root.render(<BootError error={error} />);
    return;
  }
  root.render(
    <StrictMode>
      <App config={config} />
    </StrictMode>,
  );
}

void boot();
