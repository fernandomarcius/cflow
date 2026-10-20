// Test of the login screen (src/cflow/surfaces/Login.tsx) and of the client logout, without a browser:
//     node --import ./scripts/resolver-ts.mjs scripts/teste-login.mjs   (or: npm run test:login)
//
// The screen really runs (the resolver transpiles the .tsx), with a fake React only for the hooks
// (`useState`, `useEffect`, `useRef`, `useDispatch`): each "render" calls the component, runs the
// effects whose dependencies changed and returns the element tree, from which the test takes the
// handlers (`onClick`, `onSubmit`, `onChange`) and the text. The network is a fake `fetch`.
//
// Proves:
// - the password form is shown and the recovery link answers with the generic notice (also when the
//   network is down: it never reveals whether the e-mail has an account);
// - recovery without an e-mail asks for the e-mail first;
// - logout calls the server, clears the local token and tolerates a network failure.
import { registerHooks } from "node:module";

let falhas = 0;
function igual(obtido, esperado, oque) {
  if (JSON.stringify(obtido) !== JSON.stringify(esperado)) {
    falhas++;
    console.error(`FALHA: ${oque}: esperado ${JSON.stringify(esperado)}, veio ${JSON.stringify(obtido)}`);
  }
}

// --- React de mentira (só para os módulos de src/) ------------------------------------------------
const modulo = (codigo) => `data:text/javascript,${encodeURIComponent(codigo)}`;
const REACT_FALSO = modulo(`
  const g = () => globalThis.__ganchos;
  export const useState = (i) => g().useState(i);
  export const useEffect = (f, d) => g().useEffect(f, d);
  export const useRef = (i) => g().useRef(i);
  export default { useState, useEffect, useRef };
`);
const REDUX_FALSO = modulo(`export const useDispatch = () => globalThis.__ganchos.despachar;`);
// Só quem importa é código do app (`<repo>/src/`), comparado pelo caminho absoluto: o React de
// verdade, que o `react/jsx-runtime` carrega, não pode cair no falso.
const SRC = new URL("../src/", import.meta.url).href;
registerHooks({
  resolve(especificador, contexto, proximo) {
    const deSrc = contexto?.parentURL?.startsWith(SRC) === true;
    if (deSrc && especificador === "react") return { url: REACT_FALSO, shortCircuit: true };
    if (deSrc && especificador === "react-redux") return { url: REDUX_FALSO, shortCircuit: true };
    return proximo(especificador, contexto);
  },
});

function criarGanchos() {
  const h = { estados: [], refs: [], efeitos: [], fila: [], i: 0, despachos: [] };
  h.useState = (inicial) => {
    const k = h.i++;
    if (!(k in h.estados)) h.estados[k] = typeof inicial === "function" ? inicial() : inicial;
    return [h.estados[k], (v) => (h.estados[k] = typeof v === "function" ? v(h.estados[k]) : v)];
  };
  h.useRef = (inicial) => {
    const k = h.i++;
    h.refs[k] ??= { current: inicial };
    return h.refs[k];
  };
  h.useEffect = (efeito, deps) => {
    const k = h.i++;
    const anterior = h.efeitos[k];
    if (!anterior || deps.some((d, j) => d !== anterior.deps[j])) h.fila.push([k, efeito, deps]);
  };
  h.despachar = (acao) => h.despachos.push(acao);
  return h;
}

function montar(Componente) {
  const h = criarGanchos();
  const tela = {
    arvore: null,
    render() {
      globalThis.__ganchos = h;
      h.i = 0;
      h.fila = [];
      tela.arvore = Componente();
      for (const [k, efeito, deps] of h.fila) {
        h.efeitos[k]?.limpar?.();
        h.efeitos[k] = { deps, limpar: efeito() };
      }
      return tela;
    },
    desmontar() {
      for (const e of h.efeitos) e?.limpar?.();
    },
    // Espera as promessas pendentes (consulta ao backend, recuperação) e renderiza de novo.
    async assentar() {
      await new Promise((r) => setTimeout(r, 0));
      return tela.render();
    },
    elementos() {
      const todos = [];
      const visitar = (no) => {
        if (Array.isArray(no)) no.forEach(visitar);
        else if (no && typeof no === "object" && no.props) {
          todos.push(no);
          visitar(no.props.children);
        }
      };
      visitar(tela.arvore);
      return todos;
    },
    achar(testid, dentro) {
      const raiz = dentro ? tela.elementos().find((e) => e.props["data-testid"] === dentro) : null;
      const lista = raiz ? elementosDe(raiz) : tela.elementos();
      return lista.find((e) => e.props["data-testid"] === testid) ?? null;
    },
    texto() {
      const partes = [];
      const visitar = (no) => {
        if (Array.isArray(no)) no.forEach(visitar);
        else if (typeof no === "string" || typeof no === "number") partes.push(String(no));
        else if (no && typeof no === "object" && no.props) visitar(no.props.children);
      };
      visitar(tela.arvore);
      return partes.join(" ");
    },
  };
  return tela.render();
}

function elementosDe(raiz) {
  const todos = [];
  const visitar = (no) => {
    if (Array.isArray(no)) no.forEach(visitar);
    else if (no && typeof no === "object" && no.props) {
      todos.push(no);
      visitar(no.props.children);
    }
  };
  visitar(raiz);
  return todos;
}

function armazenamento(inicial = {}) {
  const m = new Map(Object.entries(inicial));
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: (k) => m.delete(k),
    mapa: m,
  };
}

const idas = [];
function janela({ pathname = "/", search = "", hash = "", sessao = armazenamento() } = {}) {
  globalThis.window = {
    location: { pathname, search, hash, assign: (u) => idas.push(u) },
    history: { replaceState: () => {} },
    sessionStorage: sessao,
    localStorage: local,
    crypto: globalThis.crypto,
  };
  idas.length = 0;
  return globalThis.window;
}

const pedidos = [];
function rede(rotas) {
  globalThis.fetch = async (url, init = {}) => {
    pedidos.push({ url, metodo: init.method, autorizacao: init.headers?.Authorization ?? null });
    const r = rotas[`${init.method} ${url}`];
    if (r === undefined) throw new Error(`rota inesperada: ${init.method} ${url}`);
    if (r === "rede-fora") throw new TypeError("rede fora");
    if (typeof r === "function") return r();
    const [status, corpo] = r;
    return { ok: status >= 200 && status < 300, status, json: async () => corpo };
  };
  pedidos.length = 0;
}

const RECUPERACAO = "POST /api/v1/auth/recuperacao";
const local = armazenamento();
Object.defineProperty(globalThis, "localStorage", { configurable: true, value: local });
janela();
const { default: Login } = await import("../src/cflow/surfaces/Login.tsx");
const api = await import("../src/api/client.ts");

// --- the screen -----------------------------------------------------------------------------------
{
  janela();
  rede({ [RECUPERACAO]: [202, null] });
  const tela = montar(Login).render();
  igual(Boolean(tela.achar("senha")), true, "formulário de senha");
  igual(idas, [], "nenhum redirecionamento");

  tela.achar("link-recuperar").props.onClick();
  await tela.assentar();
  igual(tela.texto().includes("Informe seu e-mail"), true, "recuperação sem e-mail pede o e-mail");

  tela.achar("email").props.onChange({ target: { value: " Ana@Example.com " } });
  tela.render().achar("link-recuperar").props.onClick();
  await tela.assentar();
  igual(pedidos.at(-1).url, "/api/v1/auth/recuperacao", "recuperação pela rota versionada");
  igual(tela.texto().includes("enviamos um link para definir a senha"), true, "aviso genérico");

  rede({ [RECUPERACAO]: "rede-fora" });
  tela.achar("link-recuperar").props.onClick();
  await tela.assentar();
  igual(tela.texto().includes("enviamos um link para definir a senha"), true, "rede fora segue genérico");
  tela.desmontar();
}

// --- logout ---------------------------------------------------------------------------------------
{
  janela();
  api.definirToken("t".repeat(43));
  rede({ "POST /api/v1/auth/logout": [204, null] });
  await api.logout();
  igual(pedidos.at(-1).autorizacao, `Bearer ${"t".repeat(43)}`, "logout: envia o token da sessão");
  igual(local.getItem("cflow_token"), null, "logout: limpa o token local");

  api.definirToken("u".repeat(43));
  rede({ "POST /api/v1/auth/logout": "rede-fora" });
  await api.logout();
  igual(local.getItem("cflow_token"), null, "logout: rede fora limpa o token mesmo assim");
}

if (falhas) {
  console.error(`${falhas} falha(s)`);
  process.exit(1);
}
console.log("ok — login: formulário de senha, recuperação genérica e logout.");
