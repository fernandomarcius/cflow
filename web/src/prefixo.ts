// Where the app is mounted. The same build can be served from several places:
//  - at the ROOT of a host (docker compose with the bundled nginx.conf, `vite dev`);
//  - under any sub-path of a reverse proxy placed in front of it (for example `/cflow`).
//
// For that reason no absolute URL of the app can be a literal: `/api/v1/x` and `/execucoes/1` would
// escape the prefix. The API URL comes from `apiUrl()`, links outside React Router (a real <a href>,
// `location.assign`, a file from `public/`) come from `rota()`, and the Router `basename` is
// `prefixo()`. Vite assets do not go through here: `base: "./"` in vite.config.ts and nginx rewrites
// the relative paths of index.html (see the comment there).
//
// Only the BROWSER knows the prefix: a proxy in front strips its own part of the path before
// forwarding to nginx, and no header carries it. The prefix is derived from the URL the module was
// loaded from (`import.meta.url`, `<prefix>/assets/<chunk>.js`), which the browser resolved against
// the page: it works for any mount path, with no list of known prefixes. Outside the build (`vite
// dev` serves `/src/...`, and the tests run in Node) the prefix is empty.
const BUNDLE = /^(.*)\/assets\/[^/]+$/;

/** Version prefix of the REST API. */
export const API_BASE = "/api/v1";

/** `""` at the root; `/cflow`, `/a/b`... elsewhere (no trailing slash). */
export function prefixo(_pathname: string = window.location.pathname, urlDoModulo: string = import.meta.url): string {
  const doBundle = BUNDLE.exec(new URL(urlDoModulo).pathname);
  return doBundle ? doBundle[1] : "";
}

/** App path (`/execucoes/1`) → URL the browser can open, with the prefix when there is one. */
export function rota(caminho: string, pathname?: string): string {
  const c = caminho.startsWith("/") ? caminho : `/${caminho}`;
  return `${prefixo(pathname)}${c}`;
}

/** API path as the backend knows it (`/auth/login`) → URL of the same-host `/api/v1` proxy. */
export function apiUrl(caminho: string, pathname?: string): string {
  return rota(`${API_BASE}/${caminho.replace(/^\/+/, "")}`, pathname);
}
