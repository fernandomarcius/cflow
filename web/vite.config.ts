import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// /api → FastAPI backend (avoids CORS in dev). The versioned path /api/v1/... is forwarded as is.
// Perspective (WASM/threads) requires cross-origin isolation (SharedArrayBuffer), hence COOP/COEP.
const HEADERS = { "Cross-Origin-Opener-Policy": "same-origin", "Cross-Origin-Embedder-Policy": "require-corp" };
// Where `/api` goes: the compose API (:8000) by default. `CFLOW_API_PROXY` points somewhere else.
const ALVO_API = process.env.CFLOW_API_PROXY ?? "http://localhost:8000";
const PROXY = { "/api": { target: ALVO_API, changeOrigin: true } };

// `base: "./"`: the build does not fix the path it will be served from. The same `dist/` runs at the
// root and under any sub-path: the JS resolves chunks, WASM and fonts from its own module URL, and
// the bundled `nginx.conf` rewrites the `="./` of index.html to absolute paths at the root. Behind a
// reverse proxy mounted on a sub-path, the proxy's nginx must rewrite them accordingly (otherwise
// opening a deep route such as `/execucoes/1` directly would request `/execucoes/assets/...`).
// See src/prefixo.ts.
export default defineConfig({
  base: "./",
  plugins: [react()],
  optimizeDeps: { exclude: ["@finos/perspective", "@finos/perspective-viewer", "@finos/perspective-viewer-datagrid"] },
  build: {
    rollupOptions: {
      output: {
        // Bibliotecas pesadas em chunks próprios, com nome estável: mudar o código de uma tela não
        // invalida o cache delas no navegador. CodeMirror e React Flow só são usados por telas
        // carregadas sob demanda (Pipeline, detalhe de execução, mapa da corrida) e ficam FORA do
        // carregamento inicial. O núcleo do React tem chunk próprio por um motivo de mecânica: o
        // Rollup põe no chunk manual as dependências dos módulos dele que não têm outro dono — sem
        // `react`, React e ReactDOM iriam parar dentro do chunk do CodeMirror e o index passaria a
        // importá-lo (os dois chunks pesados voltariam ao carregamento inicial).
        // CSS fica de fora: o do React Flow é importado pelo main.tsx para manter a ordem da cascata.
        manualChunks(id) {
          if (!id.includes("/node_modules/") || id.endsWith(".css")) return undefined;
          if (/\/node_modules\/(@codemirror|@lezer|@uiw|@marijn|codemirror|crelt|style-mod|w3c-keyname)\//.test(id))
            return "codemirror";
          if (/\/node_modules\/(reactflow|@reactflow|d3-[^/]+|zustand|classcat)\//.test(id)) return "reactflow";
          if (
            /\/node_modules\/(react|react-dom|scheduler|use-sync-external-store|react-redux|@reduxjs|redux|redux-thunk|immer|reselect|react-router|react-router-dom|@remix-run)\//.test(id)
          )
            return "react";
          return undefined;
        },
      },
    },
  },
  server: { port: 5173, headers: HEADERS, proxy: PROXY },
  preview: { port: 4173, headers: HEADERS, proxy: PROXY },
});
