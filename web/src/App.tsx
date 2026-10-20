import { Component, lazy, Suspense, useEffect, useState, type ReactNode } from "react";
import { useDispatch, useSelector } from "react-redux";
import { BrowserRouter, NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";

import * as api from "./api/client";
import { prefixo } from "./prefixo";
import { useTema } from "./cflow/tema";
import { Icon, type IconName } from "./cflow/ui";
import { Wordmark } from "./cflow/logo";
import { iniciais, rotuloPerfil } from "./cflow/perfil";
import Login from "./cflow/surfaces/Login";
import Redefinir from "./cflow/surfaces/Redefinir";
import { setToken, type RootState } from "./store/store";

// --- Telas sob demanda ---
// Cada tela vira um chunk próprio, baixado na primeira vez que a rota abre. Um bundle único
// levaria o código de TODAS as telas antes de pintar a primeira. Login, Redefinir e a casca (sidebar, topbar)
// ficam no bundle principal: são o que a pessoa vê antes de qualquer rota. Nada que a casca use pode
// vir de um arquivo de tela (ver `cflow/perfil.ts`), senão a tela inteira volta ao bundle principal.
const TELAS = {
  "/pipeline": () => import("./cflow/surfaces/Pipeline"),
  "/cubo": () => import("./cflow/surfaces/Cubo"),
  "/view": () => import("./cflow/surfaces/View"),
  "/pagina": () => import("./cflow/surfaces/Pagina"),
  "/funcoes": () => import("./cflow/surfaces/Funcoes"),
  "/conexoes": () => import("./cflow/surfaces/Conexoes"),
  "/ingestao": () => import("./cflow/surfaces/FontesIngestaoTela"),
  "/execucoes": () => import("./cflow/surfaces/Execucoes"),
  "/execucoes/:id": () => import("./cflow/surfaces/ExecucaoDetalhe"),
  "/corridas/:cadeiaId": () => import("./cflow/surfaces/CorridaMapa"),
  "/agendamentos": () => import("./cflow/surfaces/Agendamentos"),
  "/portal": () => import("./cflow/surfaces/Portal"),
  "/relatorios": () => import("./cflow/surfaces/Relatorios"),
  "/usuarios": () => import("./cflow/surfaces/Usuarios"),
  "/permissoes": () => import("./cflow/surfaces/Permissoes"),
  "/promocao": () => import("./cflow/surfaces/PromocaoTela"),
  "/conta": () => import("./cflow/surfaces/Conta"),
} as const;

const Pipeline = lazy(TELAS["/pipeline"]);
const Cubo = lazy(TELAS["/cubo"]);
const View = lazy(TELAS["/view"]);
const Pagina = lazy(TELAS["/pagina"]);
const Funcoes = lazy(TELAS["/funcoes"]);
const Conexoes = lazy(TELAS["/conexoes"]);
const FontesIngestaoTela = lazy(TELAS["/ingestao"]);
const Execucoes = lazy(TELAS["/execucoes"]);
const ExecucaoDetalhe = lazy(TELAS["/execucoes/:id"]);
const CorridaMapa = lazy(TELAS["/corridas/:cadeiaId"]);
const Agendamentos = lazy(TELAS["/agendamentos"]);
const Portal = lazy(TELAS["/portal"]);
const Relatorios = lazy(TELAS["/relatorios"]);
const Usuarios = lazy(TELAS["/usuarios"]);
const Permissoes = lazy(TELAS["/permissoes"]);
const Promocao = lazy(TELAS["/promocao"]);
const Conta = lazy(TELAS["/conta"]);

// Passar o mouse (ou o foco do teclado) num item do menu já começa a baixar a tela: o clique chega
// uns centésimos depois e encontra o download andando. `import()` repetido não baixa de novo.
function precarregar(to: string) {
  const carregar = (TELAS as Record<string, (() => Promise<unknown>) | undefined>)[to];
  void carregar?.().catch(() => {
    /* sem rede agora: a navegação de verdade tenta de novo e mostra o erro, se houver */
  });
}

// Enquanto o chunk da tela chega. Discreto e atrasado (CSS): na maioria das trocas o chunk já está
// no cache e o aviso nem chega a aparecer — piscar um "Carregando…" a cada clique seria pior.
function CarregandoTela() {
  return (
    <div className="carregando-tela" role="status" data-testid="carregando-tela">
      Carregando…
    </div>
  );
}

// O chunk de uma tela pode não chegar: rede caiu, ou um deploy novo trocou os arquivos hasheados
// enquanto a aba estava aberta (o nginx devolve o index.html no lugar do JS que sumiu). Sem esta
// borda o React desmontava o app inteiro e a tela ficava em branco. Recarregar busca o index.html
// novo, que aponta os chunks que existem. Trocar de rota limpa o erro (a outra tela pode carregar).
class FalhaAoCarregarTela extends Component<{ rota: string; children: ReactNode }, { falhou: boolean }> {
  state = { falhou: false };
  static getDerivedStateFromError() {
    return { falhou: true };
  }
  componentDidUpdate(anterior: { rota: string }) {
    if (anterior.rota !== this.props.rota && this.state.falhou) this.setState({ falhou: false });
  }
  render() {
    if (!this.state.falhou) return this.props.children;
    return (
      <div className="card lc-empty" role="alert" data-testid="falha-carregar-tela">
        <div className="big">Não foi possível abrir esta tela</div>
        <div>
          A conexão falhou ou o cFlow foi atualizado enquanto esta aba estava aberta.{" "}
          <button type="button" className="btn-sec" onClick={() => window.location.reload()}>
            Recarregar a página
          </button>
        </div>
      </div>
    );
  }
}

function Telas() {
  const { pathname } = useLocation();
  return (
    <FalhaAoCarregarTela rota={pathname}>
      <Suspense fallback={<CarregandoTela />}>
        <Routes>
          <Route path="/" element={<Navigate to="/pipeline" replace />} />
          <Route path="/pipeline" element={<Pipeline />} />
          <Route path="/cubo" element={<Cubo />} />
          <Route path="/view" element={<View />} />
          <Route path="/pagina" element={<Pagina />} />
          <Route path="/funcoes" element={<Funcoes />} />
          <Route path="/conexoes" element={<Conexoes />} />
          <Route path="/ingestao" element={<FontesIngestaoTela />} />
          <Route path="/execucoes" element={<Execucoes />} />
          <Route path="/execucoes/:id" element={<ExecucaoDetalhe />} />
          <Route path="/corridas/:cadeiaId" element={<CorridaMapa />} />
          <Route path="/agendamentos" element={<Agendamentos />} />
          <Route path="/portal" element={<Portal />} />
          <Route path="/relatorios" element={<Relatorios />} />
          <Route path="/usuarios" element={<Usuarios />} />
          <Route path="/permissoes" element={<Permissoes />} />
          <Route path="/promocao" element={<Promocao />} />
          <Route path="/conta" element={<Conta />} />
          <Route path="*" element={<Navigate to="/pipeline" replace />} />
        </Routes>
      </Suspense>
    </FalhaAoCarregarTela>
  );
}

interface NavDef {
  to: string;
  label: string;
  icon: IconName;
  testid?: string;
}

const NAV_CONSTRUIR: NavDef[] = [
  { to: "/pipeline", label: "Pipeline", icon: "studio" },
  { to: "/cubo", label: "Cubo", icon: "modelo" },
  { to: "/view", label: "View", icon: "grid", testid: "nav-modelo" },
  { to: "/pagina", label: "Página", icon: "layout" },
  { to: "/funcoes", label: "Funções", icon: "funcao", testid: "nav-funcoes" },
  { to: "/conexoes", label: "Conexões", icon: "plug", testid: "nav-conexoes" },
  { to: "/ingestao", label: "Fontes de ingestão", icon: "download", testid: "nav-ingestao" },
  { to: "/execucoes", label: "Execuções", icon: "runs" },
  { to: "/agendamentos", label: "Agendamentos", icon: "relogio", testid: "nav-agendamentos" },
];

const NAV_ACOMPANHAR: NavDef[] = [
  { to: "/portal", label: "Portal", icon: "portal", testid: "nav-portal" },
  { to: "/relatorios", label: "Relatórios", icon: "stamp", testid: "nav-relatorios" },
];

const NAV_ADMIN: NavDef[] = [
  { to: "/usuarios", label: "Usuários", icon: "pessoas" },
  { to: "/permissoes", label: "Permissões", icon: "chave" },
  { to: "/promocao", label: "Promover", icon: "download", testid: "nav-promocao" },
];

function ItemNav({ def }: { def: NavDef }) {
  return (
    <NavLink
      to={def.to}
      data-testid={def.testid}
      // Com a barra recolhida o rótulo é escondido por CSS e sobrava só um ícone sem nome.
      title={def.label}
      onMouseEnter={() => precarregar(def.to)}
      onFocus={() => precarregar(def.to)}
      className={({ isActive }) => "nav-item" + (isActive ? " active" : "")}
    >
      <Icon name={def.icon} cls="ic" /> <span className="nav-label">{def.label}</span>
    </NavLink>
  );
}

const GRUPOS_NAV: { grupo: string; itens: NavDef[] }[] = [
  { grupo: "Studio", itens: NAV_CONSTRUIR },
  { grupo: "Acompanhar", itens: NAV_ACOMPANHAR },
  { grupo: "Administração", itens: NAV_ADMIN },
];

// Breadcrumbs derivados da rota atual (grupo › tela).
function Breadcrumbs() {
  const { pathname } = useLocation();
  let grupo = "cFlow";
  let label = "";
  if (pathname.startsWith("/conta")) {
    grupo = "Conta";
    label = "Minha conta";
  } else {
    for (const g of GRUPOS_NAV) {
      // O mapa da corrida pertence a Execuções: rota própria, mesmo lugar na navegação.
      const rota = pathname.startsWith("/corridas") ? "/execucoes" : pathname;
      const it = g.itens.find((i) => rota.startsWith(i.to));
      if (it) {
        grupo = g.grupo;
        label = it.label;
        break;
      }
    }
  }
  return (
    <nav className="crumbs" data-testid="crumbs" aria-label="Trilha de navegação">
      <span>{grupo}</span>
      {label && <span className="sep">›</span>}
      {label && <b>{label}</b>}
    </nav>
  );
}

// Botão de alternância claro/escuro (sol/lua).
function ThemeToggle() {
  const [tema, alternar] = useTema();
  const escuro = tema === "dark";
  return (
    <button
      className="theme-toggle"
      data-testid="btn-tema"
      title={escuro ? "Tema claro" : "Tema escuro"}
      aria-label="Alternar tema"
      onClick={alternar}
    >
      {escuro ? (
        <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
          <circle cx="12" cy="12" r="4" />
          <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
        </svg>
      ) : (
        <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
        </svg>
      )}
    </button>
  );
}

// --- Shell (sidebar agrupada por fluxo + topbar) — portado do protótipo ---
function Shell() {
  const d = useDispatch();
  const [usuario, setUsuario] = useState<api.UsuarioAtual | null>(null);
  const [recolhida, setRecolhida] = useState(() => localStorage.getItem("cflow-sidebar-recolhida") === "1");
  useEffect(() => {
    api.meAtual().then(setUsuario).catch(() => setUsuario(null));
  }, []);
  // Sessão expirou (401 numa chamada autenticada) → volta automaticamente p/ a tela de login.
  useEffect(() => {
    api.registrarExpiracao(() => d(setToken(null)));
    return () => api.registrarExpiracao(null);
  }, [d]);
  const alternarRecolher = () =>
    setRecolhida((v) => {
      localStorage.setItem("cflow-sidebar-recolhida", v ? "0" : "1");
      return !v;
    });
  const sair = async () => {
    await api.logout();
    d(setToken(null));
  };
  return (
    <div className={"shell" + (recolhida ? " recolhida" : "")}>
      {/* Pular navegação: são 12 itens de menu antes do conteúdo em toda tela. */}
      <a className="pular-nav" href="#conteudo">
        Pular para o conteúdo
      </a>
      <aside className="sidebar">
        <div className="brand">
          <Wordmark height={30} className="brand-wordmark" />
          <button
            className="sidebar-toggle"
            data-testid="btn-recolher"
            title={recolhida ? "Expandir menu" : "Recolher menu"}
            aria-label={recolhida ? "Expandir menu" : "Recolher menu"}
            aria-expanded={!recolhida}
            onClick={alternarRecolher}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d={recolhida ? "M9 18l6-6-6-6" : "M15 18l-6-6 6-6"} />
            </svg>
          </button>
        </div>

        <div className="nav-group-label" id="grp-studio">Studio</div>
        <nav className="nav" aria-labelledby="grp-studio">
          {NAV_CONSTRUIR.map((n) => (
            <ItemNav key={n.to} def={n} />
          ))}
        </nav>

        <div className="nav-group-label" id="grp-acompanhar">Acompanhar</div>
        <nav className="nav" aria-labelledby="grp-acompanhar">
          {NAV_ACOMPANHAR.map((n) => (
            <ItemNav key={n.to} def={n} />
          ))}
        </nav>

        <div className="nav-group-label" id="grp-admin">Administração</div>
        <nav className="nav" aria-labelledby="grp-admin">
          {NAV_ADMIN.map((n) => (
            <ItemNav key={n.to} def={n} />
          ))}
        </nav>

        <div className="sidebar-foot">
          <NavLink
            to="/conta"
            data-testid="nav-conta"
            onMouseEnter={() => precarregar("/conta")}
            onFocus={() => precarregar("/conta")}
            title="Minha conta · trocar senha"
            className={({ isActive }) => "sidebar-user" + (isActive ? " active" : "")}
          >
            <span className="avatar">{usuario ? iniciais(usuario.nome) : "··"}</span>
            <span className="who">
              {usuario?.nome ?? "Minha conta"}
              <span>{usuario ? rotuloPerfil(usuario.perfil) : "trocar senha"}</span>
            </span>
          </NavLink>
          <button
            className="sidebar-logout"
            title="Sair"
            data-testid="btn-logout"
            onClick={() => void sair()}
          >
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3" />
              <path d="M10 17l-5-5 5-5" />
              <line x1="5" y1="12" x2="16" y2="12" />
            </svg>
          </button>
        </div>
      </aside>

      <main className="main">
        <div className="topbar">
          <Breadcrumbs />
          <ThemeToggle />
        </div>

        <div className="content" id="conteudo" tabIndex={-1}>
          <Telas />
        </div>
      </main>
    </div>
  );
}

// Under a reverse-proxy sub-path the app lives at that path; at the root (dev, compose) the prefix is
// empty. Computed once: o prefixo não muda sem recarregar a página.
const BASENAME = prefixo() || "/";

export default function App() {
  const token = useSelector((s: RootState) => s.studio.token);
  // Pré-login: /redefinir (definição de senha via token do e-mail) é acessível sem sessão;
  // qualquer outra rota cai no Login.
  if (!token) {
    return (
      <BrowserRouter basename={BASENAME}>
        <Routes>
          <Route path="/redefinir" element={<Redefinir />} />
          <Route path="*" element={<Login />} />
        </Routes>
      </BrowserRouter>
    );
  }
  return (
    <BrowserRouter basename={BASENAME}>
      <Shell />
    </BrowserRouter>
  );
}
