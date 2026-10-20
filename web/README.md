# cFlow web

Studio, Model and Portal for [cFlow](https://github.com/fernandomarcius/cflow): React 18 + Vite +
TypeScript. It is a client of the cFlow API (`/api/v1`) and never talks to Temporal, DuckDB or
Postgres directly.

- **Studio**: pipeline graph editor (React Flow), per-node preview, SQL and Python editors
  (CodeMirror), guided operations, ingestion sources, schedules, executions and run maps.
- **Model**: cubes and views (measures governed on the server; the UI never computes numbers).
- **Portal**: published pages (blocks that reference views), pivot (Perspective, with a plain React
  table fallback) and report exports.

> The UI is written in Brazilian Portuguese (pt-BR). Code identifiers follow the same vocabulary as
> the API and the core (`pipelines`, `execucoes`, `camada`, ...). Comments are mostly in Portuguese
> and are being translated incrementally.

## Quick start

```bash
npm ci
npm run dev        # http://localhost:5173, proxies /api to http://localhost:8000
npm run build      # production bundle in dist/
npm test           # pure unit tests (no browser, no backend)
npm run typecheck  # tsc --noEmit  (npm run lint runs the same check)
```

`CFLOW_API_PROXY` points the dev server at another API (default `http://localhost:8000`). All API
calls go to `/api/v1/...` on the same origin, so no CORS configuration is needed.

## Docker

```bash
docker build -t ghcr.io/fernandomarcius/cflow-web web
docker run --rm -p 8080:80 \
  -e CFLOW_API_UPSTREAM=http://host.docker.internal:8000 \
  ghcr.io/fernandomarcius/cflow-web
```

The image serves the build with nginx (see `nginx.conf`, a template processed by `envsubst`):

| Variable              | Default            | Meaning                                                  |
| --------------------- | ------------------ | -------------------------------------------------------- |
| `CFLOW_API_UPSTREAM`  | `http://api:8000`  | Full URL of the API; `/api/` is proxied to it unchanged |
| `CFLOW_DNS_RESOLVER`  | `127.0.0.11`       | DNS used by nginx to re-resolve the upstream            |

nginx also sets `Cross-Origin-Opener-Policy`/`Cross-Origin-Embedder-Policy` (required by Perspective's
WASM threads).

## Layout

- `src/api/client.ts`: every API call and the error envelope (types are written by hand here).
- `src/cflow/surfaces/`: one screen per file; pure tab/list logic lives next to it in `*.ts` files and
  is tested without a browser (`scripts/teste-*.mjs`).
- `src/cflow/StudioGraph.tsx` and `src/cflow/layout.ts`: the graph and its automatic layout.
- `src/cflow/execucao/`: execution screens. `src/store/`: Redux store (session token, previews).
- `src/prefixo.ts`: where the app is mounted (root or any sub-path) and the `/api/v1` URL builder.

## Tests

`npm test` runs the pure tests in `scripts/` (layout, definition round trip, ingestion form, run
map, polling, login screen, URL prefix, ...). They need neither a browser nor a backend.
End-to-end tests are not included in this repository yet: the previous suite depended on a seeded
environment.

## License

Apache-2.0.
