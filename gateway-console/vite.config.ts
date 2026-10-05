import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

// Proxy de desenvolvimento: a API do gateway (console em /api e rotas OpenAI em /v1)
// fica atrás do mesmo host do console, como no nginx de produção.
export default defineConfig(({ mode }) => {
  const env = { ...loadEnv(mode, process.cwd(), ''), ...process.env };
  const apiTarget = env.LLMGW_API_UPSTREAM || 'http://localhost:8000';
  const proxy = {
    '/api': { target: apiTarget, changeOrigin: true },
    '/v1': { target: apiTarget, changeOrigin: true },
  };
  return {
    plugins: [react()],
    server: { port: 5174, proxy },
    preview: { port: 4174, proxy },
    build: { sourcemap: false, chunkSizeWarningLimit: 900 },
  };
});
