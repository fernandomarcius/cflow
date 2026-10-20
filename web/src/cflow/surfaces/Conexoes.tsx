import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { AbasFiltro, ConfirmarAcao, GridLista, Icon, type ItemCadastro, MenuAcoes} from "../ui";
import * as api from "../../api/client";
import { abaValida, abasDeTipo, daAba, rotuloTipo } from "./tipos-conexao";

// ─────────────────────────────────────────────────────────────────────────
// Conexões — cadastro das fontes/destinos de dado.
//
// Duas regras de produto mandam nesta tela:
//   1. CREDENCIAL NUNCA APARECE. A senha/token vai para o cofre cifrado e não volta: o campo de
//      segredo abre sempre vazio (em branco = manter), nada é reexibido e nenhuma mensagem de erro
//      carrega o valor. O resumo do destino é montado por LISTA BRANCA de chaves (ver `destino`).
//   2. `permitir_escrita` é ação consciente de admin. Habilitar deixa um nó de efeito GRAVAR no
//      banco — não pode ser um checkbox solto na linha, é botão + confirmação com a consequência
//      escrita por extenso.
// Filtros na URL (linkável, sobrevive ao voltar do formulário), como na tela de Execuções.
// ─────────────────────────────────────────────────────────────────────────

// --- Tipos de conector ---
type Tipo = "sqlserver" | "postgres" | "api_rest" | "arquivo" | "api_query" | "smtp";
type AuthTipo = "none" | "bearer" | "header";

const TIPOS: { valor: Tipo; rotulo: string; meta: string }[] = [
  { valor: "sqlserver", rotulo: "SQL Server", meta: "banco relacional Microsoft" },
  { valor: "postgres", rotulo: "PostgreSQL", meta: "banco relacional Postgres" },
  { valor: "api_rest", rotulo: "API REST", meta: "endpoint HTTP que devolve JSON" },
  { valor: "arquivo", rotulo: "Arquivo", meta: "CSV, Excel ou Parquet" },
  { valor: "api_query", rotulo: "API de consulta (SQL via gateway)", meta: "gateway HTTP que executa SQL" },
  { valor: "smtp", rotulo: "Servidor de e-mail (SMTP)", meta: "destino do nó de e-mail" },
];

// Tipos que sempre exigem segredo (senha de banco).
const SEGREDO_SEMPRE = new Set<Tipo>(["sqlserver", "postgres", "api_query"]);

interface FormState {
  // bancos
  host: string;
  porta: string;
  base: string;
  usuario: string;
  // api_rest
  url: string;
  metodo: string;
  auth_tipo: AuthTipo;
  auth_header: string;
  json_path: string;
  // arquivo
  caminho: string;
  formato: "csv" | "xlsx" | "parquet";
  aba: string;
  // smtp (servidor de e-mail — 009)
  seguranca: "starttls" | "ssl" | "nenhuma";
  remetente: string;
  remetente_nome: string;
  tentativas: string;
  // api_query (SQL via gateway)
  login_path: string;
  query_path: string;
  token_campo: string;
  results_campo: string;
  sql_campo: string;
  // segredo (sempre fora do config)
  segredo: string;
}

function estadoInicial(tipo: Tipo): FormState {
  return {
    host: "",
    porta: tipo === "sqlserver" ? "1433" : tipo === "smtp" ? "587" : "5432",
    base: "",
    usuario: "",
    url: "",
    metodo: "GET",
    auth_tipo: "none",
    auth_header: "X-API-Key",
    json_path: "",
    caminho: "",
    formato: "csv",
    aba: "",
    seguranca: "starttls",
    remetente: "",
    remetente_nome: "",
    tentativas: "3",
    login_path: "/api/v1/auth/login",
    query_path: "/api/v1/query",
    token_campo: "token",
    results_campo: "results",
    sql_campo: "sql",
    segredo: "",
  };
}

// Reidrata o FormState a partir do `config` salvo (caminho inverso de montarConfig). O segredo
// NUNCA vem no config (fica no cofre) — o campo `segredo` começa vazio: em branco = manter.
function formDeConfig(tipo: Tipo, config: Record<string, unknown>): FormState {
  const f = estadoInicial(tipo);
  const s = (v: unknown) => (v == null ? "" : String(v));
  if (tipo === "sqlserver" || tipo === "postgres") {
    return { ...f, host: s(config.host), porta: s(config.porta) || f.porta, base: s(config.base), usuario: s(config.usuario) };
  }
  if (tipo === "api_rest") {
    return {
      ...f,
      url: s(config.url),
      metodo: s(config.metodo) || "GET",
      auth_tipo: (config.auth_tipo as AuthTipo) || "none",
      auth_header: s(config.auth_header) || f.auth_header,
      json_path: s(config.json_path),
    };
  }
  if (tipo === "smtp") {
    return {
      ...f,
      host: s(config.host),
      porta: s(config.porta) || f.porta,
      usuario: s(config.usuario),
      seguranca: (config.seguranca as FormState["seguranca"]) || "starttls",
      remetente: s(config.remetente),
      remetente_nome: s(config.remetente_nome),
      tentativas: s(config.tentativas) || f.tentativas,
    };
  }
  if (tipo === "api_query") {
    return {
      ...f,
      url: s(config.url),
      usuario: s(config.usuario),
      login_path: s(config.login_path) || f.login_path,
      query_path: s(config.query_path) || f.query_path,
      token_campo: s(config.token_campo) || f.token_campo,
      results_campo: s(config.results_campo) || f.results_campo,
      sql_campo: s(config.sql_campo) || f.sql_campo,
    };
  }
  // arquivo
  return { ...f, caminho: s(config.caminho), formato: (config.formato as FormState["formato"]) || "csv", aba: s(config.aba) };
}

// Monta o config a partir só dos campos relevantes ao tipo (segredo nunca entra aqui).
function montarConfig(tipo: Tipo, f: FormState): Record<string, unknown> {
  if (tipo === "sqlserver" || tipo === "postgres") {
    return {
      host: f.host.trim(),
      porta: Number(f.porta) || (tipo === "sqlserver" ? 1433 : 5432),
      base: f.base.trim(),
      usuario: f.usuario.trim(),
    };
  }
  if (tipo === "api_rest") {
    const cfg: Record<string, unknown> = {
      url: f.url.trim(),
      metodo: f.metodo || "GET",
      auth_tipo: f.auth_tipo,
    };
    if (f.auth_tipo === "header") cfg.auth_header = f.auth_header.trim() || "X-API-Key";
    if (f.json_path.trim()) cfg.json_path = f.json_path.trim();
    return cfg;
  }
  if (tipo === "smtp") {
    // Só o que o conector lê (smtp.py). `remetente` em branco = usa o usuário de autenticação —
    // é o comum em servidores SMTP, e evita um campo obrigatório redundante.
    const cfg: Record<string, unknown> = {
      host: f.host.trim(),
      porta: Number(f.porta) || 587,
      seguranca: f.seguranca,
      tentativas: Number(f.tentativas) || 3,
    };
    if (f.usuario.trim()) cfg.usuario = f.usuario.trim();
    if (f.remetente.trim()) cfg.remetente = f.remetente.trim();
    if (f.remetente_nome.trim()) cfg.remetente_nome = f.remetente_nome.trim();
    return cfg;
  }
  if (tipo === "api_query") {
    return {
      url: f.url.trim(),
      usuario: f.usuario.trim(),
      login_path: f.login_path.trim() || "/api/v1/auth/login",
      query_path: f.query_path.trim() || "/api/v1/query",
      token_campo: f.token_campo.trim() || "token",
      results_campo: f.results_campo.trim() || "results",
      sql_campo: f.sql_campo.trim() || "sql",
    };
  }
  // arquivo
  return {
    caminho: f.caminho.trim(),
    formato: f.formato,
    ...(f.formato === "xlsx" && f.aba.trim() ? { aba: f.aba.trim() } : {}),
  };
}

// Diz se o tipo + estado atual precisa de segredo no corpo.
function precisaSegredo(tipo: Tipo, f: FormState): boolean {
  if (SEGREDO_SEMPRE.has(tipo)) return true;
  if (tipo === "api_rest") return f.auth_tipo !== "none";
  // SMTP sem usuário = relay interno sem autenticação (existe): a senha só é exigida com usuário.
  if (tipo === "smtp") return f.usuario.trim() !== "";
  return false; // arquivo nunca
}

// Só estes tipos podem ser DESTINO de nó de efeito: `escrita_sql` grava em postgres/sqlserver, o
// nó `http` chama api_rest e o nó `email` envia por smtp (ver `cflow_core.pipelines.service`).
// Oferecer "permitir escrita" numa conexão de arquivo é oferecer um botão que nunca vai funcionar.
const TIPOS_ESCRITA = new Set<string>(["postgres", "sqlserver", "api_rest", "smtp"]);

// Tira `usuario:senha@` de uma URL. O `config` não deveria trazer credencial (ela vive no cofre),
// mas uma URL colada à mão pode trazer — e a lista não é lugar de exibir segredo.
function semCredencialNaUrl(url: string): string {
  return url.replace(/(\w+:\/\/)[^/@\s]*@/, "$1");
}

// Resumo do DESTINO da conexão — a informação que identifica a fonte (host/base, URL, caminho).
// LISTA BRANCA de chaves de propósito: assim um campo novo no `config` do backend não vaza para a
// tela por acidente.
function destino(c: api.Conexao): string {
  const cfg = (c.config ?? {}) as Record<string, unknown>;
  const s = (k: string) => (cfg[k] == null ? "" : String(cfg[k]));
  if (c.tipo === "sqlserver" || c.tipo === "postgres") {
    const hostPorta = [s("host"), s("porta")].filter(Boolean).join(":");
    return [hostPorta, s("base")].filter(Boolean).join(" / ");
  }
  if (c.tipo === "api_rest" || c.tipo === "api_query") return semCredencialNaUrl(s("url"));
  if (c.tipo === "smtp") {
    const hostPorta = [s("host"), s("porta")].filter(Boolean).join(":");
    return [hostPorta, s("remetente") || s("usuario")].filter(Boolean).join(" / ");
  }
  if (c.tipo === "arquivo") {
    const fmt = s("formato");
    return [s("caminho"), fmt ? fmt.toUpperCase() : ""].filter(Boolean).join(" · ");
  }
  return "";
}

interface ResultadoTeste {
  estado: "testando" | "ok" | "falha";
  texto: string;
}

// Erro da API: mensagem pt-BR do envelope + `request_id` guardado para suporte (quando o cliente
// da API o expõe no erro).
interface ErroTela {
  mensagem: string;
  requestId?: string;
}

function erroDe(e: unknown, fallback: string): ErroTela {
  // `ErroApi` expõe `requestId`; parte do cliente ainda anexa `request_id` (snake). Ler só um dos
  // dois deixava o código de suporte fora da tela justamente nos erros do outro caminho.
  const err = e as { requestId?: string; request_id?: string } | null;
  const req = err?.requestId ?? err?.request_id;
  return {
    mensagem: e instanceof Error && e.message ? e.message : fallback,
    requestId: typeof req === "string" ? req : undefined,
  };
}

function Aviso({ erro, onTentar }: { erro: ErroTela; onTentar?: () => void }) {
  return (
    <div className="exec-banner-falha" role="alert" style={{ marginBottom: 14 }}>
      <span className="dot" />
      <span>
        {erro.mensagem}
        {erro.requestId && (
          <span style={{ display: "block", fontSize: 11.5, opacity: 0.9 }}>
            Código para suporte: {erro.requestId}
          </span>
        )}
      </span>
      {onTentar && (
        <button type="button" className="pag-btn" onClick={onTentar}>
          Tentar de novo
        </button>
      )}
    </div>
  );
}

export default function Conexoes() {
  const [params, setParams] = useSearchParams();
  const [criando, setCriando] = useState(false);
  // Conexão sendo editada (abre o formulário pré-preenchido); null = lista.
  const [editando, setEditando] = useState<api.Conexao | null>(null);

  // lista (mantém as linhas completas p/ poder editar/testar por linha)
  const [conexoes, setConexoes] = useState<api.Conexao[]>([]);
  const [carregando, setCarregando] = useState(true);
  const [erroLista, setErroLista] = useState<ErroTela | null>(null);
  // Resultado do teste por linha (id → estado/causa).
  const [testes, setTestes] = useState<Record<string, ResultadoTeste>>({});
  const [testandoId, setTestandoId] = useState<string | null>(null);
  // Última notícia da tela (resultado do teste) numa ÚNICA região aria-live.
  const [anuncio, setAnuncio] = useState("");
  // `permitir_escrita` só é exibido/editável para admin (o backend também exige admin — SoAdmin).
  const [ehAdmin, setEhAdmin] = useState(false);
  // Linha com alternância de escrita em voo (desabilita a ação enquanto grava).
  const [escritaId, setEscritaId] = useState<string | null>(null);
  // Ação que aguarda confirmação no modal do design system (nunca window.confirm/prompt).
  const [pedindo, setPedindo] = useState<{
    tipo: "excluir" | "escrita" | "duplicar";
    c: api.Conexao;
  } | null>(null);

  const noFormulario = criando || editando !== null;

  // Filtros na URL: busca (q) + tipo (t) + só as habilitadas para escrita (w).
  const busca = params.get("q") ?? "";
  const tipoFiltro = params.get("t") ?? "";
  const soEscrita = params.get("w") === "1";
  const temFiltro = !!(busca || tipoFiltro || soEscrita);

  const trocarFiltro = useCallback(
    (chave: string, valor: string) => {
      const p = new URLSearchParams(params);
      if (valor) p.set(chave, valor);
      else p.delete(chave);
      setParams(p, { replace: true });
    },
    [params, setParams],
  );
  const limparFiltros = useCallback(
    () => setParams(new URLSearchParams(), { replace: true }),
    [setParams],
  );

  useEffect(() => {
    let vivo = true;
    api
      .meAtual()
      .then((u) => {
        if (vivo) setEhAdmin(u.perfil === "admin");
      })
      .catch(() => {
        if (vivo) setEhAdmin(false);
      });
    return () => {
      vivo = false;
    };
  }, []);

  async function recarregar() {
    setCarregando(true);
    setErroLista(null);
    try {
      setConexoes(await api.listarConexoes());
    } catch (e) {
      setErroLista(erroDe(e, "Falha ao carregar conexões."));
    } finally {
      setCarregando(false);
    }
  }
  useEffect(() => {
    if (!noFormulario) void recarregar();
  }, [noFormulario]);

  async function excluir(c: api.Conexao) {
    setErroLista(null);
    try {
      await api.excluirConexao(c.id);
      await recarregar();
    } catch (e) {
      setErroLista(erroDe(e, "Falha ao excluir conexão."));
    }
  }

  // Duplica com a MESMA credencial — copiada dentro do cofre, sem ninguém ver nem redigitar o
  // segredo. A cópia nasce sem permissão de escrita; o resultado vai para a região aria-live.
  async function duplicar(c: api.Conexao, nome: string) {
    setErroLista(null);
    try {
      await api.duplicarConexao(c.id, nome.trim());
      await recarregar();
      setAnuncio(
        `${c.nome} duplicada como ${nome.trim()} — mesma credencial, sem permissão de escrita.`,
      );
    } catch (e) {
      setErroLista(erroDe(e, "Falha ao duplicar a conexão."));
    }
  }

  // Testa a conexão na própria linha (probe leve no backend); pill + causa pt-BR.
  // O resultado também vai para UMA região aria-live (`anuncio`) — uma por linha faria o leitor de
  // tela concorrer consigo mesmo, como na lista de execuções.
  async function testar(id: string) {
    const nome = conexoes.find((c) => c.id === id)?.nome ?? "conexão";
    setTestandoId(id);
    setTestes((t) => ({ ...t, [id]: { estado: "testando", texto: "testando…" } }));
    setAnuncio(`${nome}: testando o acesso…`);
    try {
      const r = await api.testarConexao(id);
      const ok = r.ok === true;
      const causa = r.causa ? String(r.causa) : "sem acesso";
      setTestes((t) => ({
        ...t,
        [id]: { estado: ok ? "ok" : "falha", texto: ok ? "acesso OK" : causa },
      }));
      setAnuncio(ok ? `${nome}: acesso OK.` : `${nome}: sem acesso — ${causa}`);
    } catch (e) {
      const msg = erroDe(e, "falha ao testar").mensagem;
      setTestes((t) => ({ ...t, [id]: { estado: "falha", texto: msg } }));
      setAnuncio(`${nome}: ${msg}`);
    } finally {
      setTestandoId(null);
    }
  }

  // Liga/desliga `permitir_escrita` (habilita nós de escrita_sql/http a usar a conexão). Otimista:
  // reflete na hora e reverte se o backend recusar; erro pt-BR do envelope vai para `erroLista`.
  async function alternarEscrita(id: string, permitir: boolean) {
    setErroLista(null);
    setEscritaId(id);
    setConexoes((cs) => cs.map((c) => (c.id === id ? { ...c, permitir_escrita: permitir } : c)));
    try {
      const r = await api.definirEscritaConexao(id, permitir);
      setConexoes((cs) => cs.map((c) => (c.id === id ? { ...c, permitir_escrita: r.permitir_escrita } : c)));
    } catch (e) {
      // Reverte a alternância otimista e mostra a causa pt-BR.
      setConexoes((cs) => cs.map((c) => (c.id === id ? { ...c, permitir_escrita: !permitir } : c)));
      setErroLista(erroDe(e, "Falha ao alterar a permissão de escrita."));
    } finally {
      setEscritaId(null);
    }
  }

  // Abas por tipo — só os tipos PRESENTES no cadastro (a regra que o seletor substituído já
  // seguia). A lógica é pura e vive em `tipos-conexao.ts`, testada sem navegador.
  const abasTipo = useMemo(() => abasDeTipo(conexoes), [conexoes]);
  // Aba efetiva: valor estranho na querystring (`?t=xyz`, link antigo, tipo que sumiu do cadastro)
  // cai em "Todas" — nunca numa lista vazia sem explicação.
  const abaTipo = useMemo(
    () => abaValida(tipoFiltro, abasTipo.map((a) => a.chave)),
    [tipoFiltro, abasTipo],
  );

  const visiveis = useMemo(() => {
    const q = busca.trim().toLowerCase();
    const chaves = abasTipo.map((a) => a.chave);
    return conexoes.filter((c) => {
      if (!daAba(c, abaTipo, chaves)) return false;
      if (soEscrita && c.permitir_escrita !== true) return false;
      if (!q) return true;
      return (
        c.nome.toLowerCase().includes(q) ||
        rotuloTipo(c.tipo).toLowerCase().includes(q) ||
        destino(c).toLowerCase().includes(q)
      );
    });
  }, [conexoes, busca, abaTipo, abasTipo, soEscrita]);

  const comEscrita = useMemo(
    () => conexoes.filter((c) => c.permitir_escrita === true).length,
    [conexoes],
  );

  if (criando) {
    return <NovaConexao onVoltar={() => setCriando(false)} />;
  }
  if (editando) {
    return <NovaConexao editar={editando} onVoltar={() => setEditando(null)} />;
  }

  const total = conexoes.length;
  const contagem = temFiltro
    ? `${visiveis.length} de ${total} ${total === 1 ? "conexão" : "conexões"} (filtradas)`
    : total === 1
      ? "1 conexão"
      : `${total} conexões`;

  return (
    <>
      {pedindo?.tipo === "excluir" && (
        <ConfirmarAcao
          titulo="Excluir conexão"
          corpo={
            <>
              Remover <b>{pedindo.c.nome}</b> e a credencial guardada no cofre? Pipelines que usam
              esta conexão param de rodar. Esta ação não pode ser desfeita.
            </>
          }
          rotuloConfirmar="Excluir"
          perigo
          onConfirmar={() => {
            void excluir(pedindo.c);
            setPedindo(null);
          }}
          onCancelar={() => setPedindo(null)}
        />
      )}
      {pedindo?.tipo === "duplicar" && (
        <ConfirmarAcao
          titulo="Duplicar conexão"
          corpo={
            <>
              Cria uma conexão nova com o tipo, a configuração e a <b>mesma credencial</b> de{" "}
              <b>{pedindo.c.nome}</b> — o segredo é copiado dentro do cofre, sem aparecer aqui. A
              cópia nasce <b>sem permissão de escrita</b>.
            </>
          }
          rotuloCampo="Nome da cópia"
          valorInicial={`${pedindo.c.nome} (cópia)`}
          rotuloConfirmar="Duplicar"
          onConfirmar={(nome) => {
            void duplicar(pedindo.c, nome);
            setPedindo(null);
          }}
          onCancelar={() => setPedindo(null)}
        />
      )}
      {pedindo?.tipo === "escrita" && (
        <ConfirmarAcao
          titulo={
            pedindo.c.permitir_escrita ? "Desabilitar escrita" : "Habilitar escrita (irreversível no dado)"
          }
          corpo={
            pedindo.c.permitir_escrita ? (
              <>
                <b>{pedindo.c.nome}</b> volta a ser somente leitura. Pipelines com nó de escrita ou
                de chamada HTTP nesta conexão passam a ser recusados ao publicar.
              </>
            ) : (
              <>
                Nós de escrita passam a <b>gravar de verdade</b> em <b>{pedindo.c.nome}</b> (INSERT,
                UPDATE, DELETE e chamadas HTTP com efeito), com o dado completo, a cada execução —
                inclusive agendada. Habilite só em destino que pode receber gravação da plataforma.
                <span className="exec-aviso" style={{ display: "block", marginTop: 10 }}>
                  Gravação feita não se desfaz pela plataforma. A trilha de auditoria registra quem
                  habilitou.
                </span>
              </>
            )
          }
          rotuloConfirmar={pedindo.c.permitir_escrita ? "Desabilitar escrita" : "Habilitar escrita"}
          perigo={!pedindo.c.permitir_escrita}
          onConfirmar={() => {
            void alternarEscrita(pedindo.c.id, !pedindo.c.permitir_escrita);
            setPedindo(null);
          }}
          onCancelar={() => setPedindo(null)}
        />
      )}

      <div className="page-head">
        <h1 className="display">Conexões</h1>
        <p className="sub">
          Onde a plataforma busca (e, quando autorizado, grava) dado. A senha ou token fica no cofre
          cifrado — não aparece aqui nem volta em nenhuma tela.
        </p>
        {ehAdmin && total > 0 && (
          <p className="exec-resumo-hoje" data-testid="cx-resumo">
            {comEscrita === 0 ? (
              <span>Nenhuma conexão habilitada para escrita.</span>
            ) : (
              <button
                type="button"
                className="lk"
                data-testid="cx-resumo-escrita"
                onClick={() => trocarFiltro("w", soEscrita ? "" : "1")}
              >
                {comEscrita === 1
                  ? "1 conexão habilitada para escrita"
                  : `${comEscrita} conexões habilitadas para escrita`}
              </button>
            )}
          </p>
        )}
      </div>

      <div className="pad">
        {erroLista && <Aviso erro={erroLista} onTentar={() => void recarregar()} />}

        {/* Abas por TIPO, no lugar do seletor que existia aqui. Mesma régua da listagem de
            pipelines (`AbasFiltro`, um componente só) e o MESMO filtro `?t=` da querystring — link
            antigo com `?t=smtp` continua abrindo a aba certa. A aba só aparece para tipo PRESENTE:
            é a regra que o seletor já seguia, e abas vazias seriam ruído. */}
        <AbasFiltro
          abas={abasTipo}
          ativa={abaTipo}
          onAba={(chave) => trocarFiltro("t", chave)}
          rotulo="Filtrar conexões por tipo"
          idPainel="cx-painel"
          idPrefixo="cx-aba"
        />
        <div className="lc-bar" id="cx-painel">
          <span data-testid="cx-contagem">{contagem}</span>
          <button
            className="btn-stamp"
            style={{ marginLeft: "auto" }}
            data-testid="nova-conexao"
            onClick={() => setCriando(true)}
          >
            + Nova conexão
          </button>
        </div>

        {(total > 1 || temFiltro) && (
          <div
            data-testid="cx-filtros"
            style={{
              display: "flex",
              alignItems: "flex-end",
              gap: 14,
              flexWrap: "wrap",
              marginBottom: 14,
            }}
          >
            <label style={{ display: "flex", flexDirection: "column", gap: 5 }}>
              <span className="eyebrow">Buscar</span>
              <input
                className="nm"
                data-testid="cx-busca"
                type="search"
                value={busca}
                placeholder="nome, tipo ou destino"
                style={{ width: 240 }}
                onChange={(e) => trocarFiltro("q", e.target.value)}
              />
            </label>
            {ehAdmin && (
              <button
                type="button"
                className={"chip" + (soEscrita ? " on" : "")}
                aria-pressed={soEscrita}
                data-testid="cx-filtro-escrita"
                style={{ height: 37 }}
                onClick={() => trocarFiltro("w", soEscrita ? "" : "1")}
              >
                Só com escrita
              </button>
            )}
            {temFiltro && (
              <button
                type="button"
                className="pag-btn"
                data-testid="cx-limpar-filtros"
                style={{ height: 37 }}
                onClick={limparFiltros}
              >
                Limpar filtros
              </button>
            )}
            {/* Uma única região aria-live para o resultado do teste (uma por linha faria o leitor
                de tela concorrer consigo mesmo). */}
            <span
              className="exec-atualizado"
              role="status"
              aria-live="polite"
              data-testid="cx-anuncio"
              style={{ marginLeft: "auto", paddingBottom: 8, maxWidth: "34ch" }}
            >
              {anuncio}
            </span>
          </div>
        )}

        {carregando ? (
          <div className="card lc-empty" role="status">
            <div className="big">Carregando conexões…</div>
          </div>
        ) : total === 0 ? (
          <div className="card lc-empty">
            <div className="big">Nenhuma conexão cadastrada</div>
            <div>
              Clique <b>+ Nova conexão</b> para cadastrar a primeira fonte.
            </div>
          </div>
        ) : (
          // A linha tem muitas ações; em notebook (1180px) a última (Excluir) saía da tela e o
          // `.dgrid` — que tem overflow:hidden por causa do raio — a recortava em vez de deixar
          // alcançá-la. Este invólucro rola na horizontal, como faz a tabela de Execuções.
          <div style={{ overflowX: "auto" }} tabIndex={0} role="region" aria-label="Conexões cadastradas">
            <GridLista
              erro={!!erroLista}
              itens={visiveis.map((c): ItemCadastro => {
                const t = testes[c.id];
                const campos: NonNullable<ItemCadastro["campos"]> = [];
                const alvo = destino(c);
                campos.push({
                  rotulo: "Destino",
                  // Destino é o que identifica a fonte: o valor INTEIRO fica no `title` (e o
                  // truncamento é anunciado por `aria-label`, porque `title` não chega a quem usa
                  // leitor de tela). Só o excedente vira reticência.
                  valor: alvo ? (
                    <span className="cx-destino" title={alvo} aria-label={`Destino: ${alvo}`}>
                      {alvo}
                    </span>
                  ) : (
                    <span style={{ color: "var(--ink-2)" }} title="Esta conexão não declara destino.">
                      sem destino
                    </span>
                  ),
                });
                // Escrita: LEITURA na linha (a troca é ação deliberada, na coluna de ações). Só admin
                // vê — o backend também exige admin para alterar.
                if (ehAdmin) {
                  campos.push({
                    rotulo: "Escrita",
                    valor: c.permitir_escrita ? (
                      <span
                        className="statuspill skip"
                        data-testid={`escrita-linha-${c.id}`}
                        title="Nós de escrita/HTTP podem gravar nesta conexão."
                      >
                        <span className="d" />
                        habilitada
                      </span>
                    ) : TIPOS_ESCRITA.has(c.tipo) ? (
                      <span data-testid={`escrita-linha-${c.id}`} style={{ color: "var(--ink-2)" }}>
                        só leitura
                      </span>
                    ) : (
                      <span
                        data-testid={`escrita-linha-${c.id}`}
                        style={{ color: "var(--ink-2)" }}
                        title="Este tipo de conexão não recebe escrita da plataforma."
                      >
                        —
                      </span>
                    ),
                  });
                }
                campos.push({
                  // O resultado do teste NÃO é guardado no servidor: ele vale para esta sessão da
                  // tela. A coluna diz isso no cabeçalho, e a célula vazia diz "não testada" —
                  // "—" lia como "não se aplica", que é outra afirmação.
                  rotulo: "Teste (nesta sessão)",
                  valor: (
                    <span data-testid={`teste-linha-${c.id}`}>
                      {!t ? (
                        <span
                          style={{ color: "var(--ink-2)" }}
                          title="O acesso ainda não foi testado nesta sessão — o resultado do teste não é guardado entre visitas. Use Testar."
                        >
                          não testada
                        </span>
                      ) : t.estado === "testando" ? (
                        <span style={{ color: "var(--ink-2)" }}>testando…</span>
                      ) : (
                        <span
                          className={"statuspill " + (t.estado === "ok" ? "ok" : "falha")}
                          title={t.texto}
                        >
                          <span className="d" />
                          {t.estado === "ok" ? "acesso OK" : "sem acesso"}
                        </span>
                      )}
                      {t && t.estado === "falha" && (
                        <span className="exec-cel-erro" style={{ maxWidth: "30ch" }} title={t.texto}>
                          {t.texto}
                        </span>
                      )}
                    </span>
                  ),
                });
                return {
                  id: c.id,
                  nome: c.nome,
                  meta: rotuloTipo(c.tipo),
                  // Coluna de status SÓ existe quando há conexão inativa: com todas ativas, ela
                  // repetia "ativa" em cada linha e comia a largura de que as ações precisavam.
                  status: c.ativo === false ? "inativa" : undefined,
                  scls: "falha",
                  campos,
                };
              })}
              onAbrir={(id) => {
                const c = conexoes.find((x) => x.id === id);
                if (c) setEditando(c);
              }}
              acoes={(it) => {
                const c = conexoes.find((x) => x.id === it.id);
                if (!c) return null;
                // A ação de escrita só aparece onde faz sentido (tipo que recebe escrita) ou onde
                // já está habilitada — aí ela precisa existir para poder ser DESLIGADA.
                const mostrarEscrita = ehAdmin && (TIPOS_ESCRITA.has(c.tipo) || c.permitir_escrita);
                return (
                  <>
                    {/* Só EDITAR fora: quatro botões de mesmo peso faziam a ação corriqueira e a
                        irreversível disputarem o mesmo pixel. */}
                    <button data-testid={`editar-linha-${c.id}`} onClick={() => setEditando(c)}>
                      Editar
                    </button>
                    <MenuAcoes rotulo={`Mais ações de ${c.nome}`}>
                      <button
                        data-testid={`testar-linha-${c.id}`}
                        disabled={testandoId === c.id}
                        onClick={() => void testar(c.id)}
                      >
                        {testandoId === c.id ? "Testando…" : "Testar conexão"}
                      </button>
                      {mostrarEscrita && (
                        <button
                          data-testid={`escrita-acao-${c.id}`}
                          disabled={escritaId === c.id}
                          onClick={() => setPedindo({ tipo: "escrita", c })}
                        >
                          {c.permitir_escrita ? "Tirar permissão de escrita" : "Permitir escrita"}
                        </button>
                      )}
                      <button
                        data-testid={`duplicar-acao-${c.id}`}
                        onClick={() => setPedindo({ tipo: "duplicar", c })}
                      >
                        Duplicar…
                      </button>
                      <button className="del" onClick={() => setPedindo({ tipo: "excluir", c })}>
                        Excluir
                      </button>
                    </MenuAcoes>
                  </>
                );
              }}
              vazio={
                <div className="card lc-empty">
                  <div className="big">Nenhuma conexão com esses filtros</div>
                  <div>
                    Ajuste a busca ou o tipo.{" "}
                    <button
                      type="button"
                      className="lk"
                      data-testid="cx-vazio-limpar"
                      onClick={limparFiltros}
                    >
                      Limpar filtros
                    </button>
                  </div>
                </div>
              }
            />
          </div>
        )}
      </div>
    </>
  );
}

interface NovaConexaoProps {
  onVoltar: () => void;
  // Se presente, o formulário abre em modo EDIÇÃO (pré-preenchido; nome/tipo travados; PUT).
  editar?: api.Conexao;
}

function NovaConexao({ onVoltar, editar }: NovaConexaoProps) {
  const editando = !!editar;
  const tipoInicial = (editar?.tipo as Tipo) ?? "sqlserver";
  const [nome, setNome] = useState(editar?.nome ?? "");
  const [tipo, setTipo] = useState<Tipo>(tipoInicial);
  const [form, setForm] = useState<FormState>(() =>
    editar ? formDeConfig(tipoInicial, (editar.config ?? {}) as Record<string, unknown>) : estadoInicial("sqlserver"),
  );

  const [salvando, setSalvando] = useState(false);
  const [erroForm, setErroForm] = useState<ErroTela | null>(null);
  // Em edição, a conexão já existe → o id permite Testar de imediato; em criação, vira não-nulo ao salvar.
  const [criadaId, setCriadaId] = useState<string | null>(editar?.id ?? null);
  // Em edição, controla quando a config já foi gravada (PUT) — habilita "voltar à lista" com confiança.
  const [editado, setEditado] = useState(false);

  const [testando, setTestando] = useState(false);
  const [teste, setTeste] = useState<{ ok: boolean; texto: string } | null>(null);

  const [subindo, setSubindo] = useState(false);
  const [arquivoNome, setArquivoNome] = useState<string | null>(null);

  function trocarTipo(t: Tipo) {
    setTipo(t);
    setForm(estadoInicial(t));
    setErroForm(null);
    setArquivoNome(null);
  }

  async function subirArquivo(file: File) {
    setErroForm(null);
    setSubindo(true);
    try {
      const r = await api.uploadArquivo(file);
      set("caminho", r.caminho);
      setArquivoNome(r.nome_original);
    } catch (e) {
      setErroForm(erroDe(e, "Falha ao subir arquivo."));
    } finally {
      setSubindo(false);
    }
  }

  function set<K extends keyof FormState>(campo: K, valor: FormState[K]) {
    setForm((f) => ({ ...f, [campo]: valor }));
  }

  async function salvar() {
    setErroForm(null);
    setTeste(null);
    if (!nome.trim()) {
      setErroForm({ mensagem: "Informe um nome para a conexão." });
      return;
    }
    setSalvando(true);
    try {
      const config = montarConfig(tipo, form);
      if (editando && editar) {
        // Renomear é endpoint próprio (auditado à parte): sai ANTES da config, e se falhar a
        // gravação não acontece — nada de metade salva sem dizer qual metade.
        if (nome.trim() !== editar.nome) {
          await api.renomearConexao(editar.id, nome.trim());
        }
        // PUT: atualiza a config; credencial só se foi digitada (em branco = mantém a atual).
        const body: { config: Record<string, unknown>; credencial?: string } = { config };
        // Só envia credencial se o tipo usa segredo E o campo foi preenchido (rotaciona).
        if (precisaSegredo(tipo, form) && form.segredo.trim()) {
          body.credencial = form.segredo;
        }
        await api.editarConexao(editar.id, body);
        // Some com o valor digitado assim que ele vai para o cofre: nada de segredo parado na tela.
        set("segredo", "");
        setEditado(true);
      } else {
        const body: { nome: string; tipo: string; config: Record<string, unknown>; segredo?: string } = {
          nome: nome.trim(),
          tipo,
          config,
        };
        if (precisaSegredo(tipo, form)) {
          body.segredo = form.segredo;
        }
        const { id } = await api.criarConexao(body);
        set("segredo", "");
        setCriadaId(id);
      }
    } catch (e) {
      setErroForm(erroDe(e, editando ? "Falha ao editar conexão." : "Falha ao criar conexão."));
    } finally {
      setSalvando(false);
    }
  }

  async function testar() {
    if (!criadaId) return;
    setTestando(true);
    setTeste(null);
    try {
      const r = await api.testarConexao(criadaId);
      const ok = r.ok === true;
      setTeste({
        ok,
        texto: ok
          ? "Conexão estabelecida com sucesso."
          : `Falha na conexão${r.causa ? ` — ${r.causa}` : "."}`,
      });
    } catch (e) {
      setTeste({ ok: false, texto: erroDe(e, "Falha ao testar conexão.").mensagem });
    } finally {
      setTestando(false);
    }
  }

  const mostrarSegredo = precisaSegredo(tipo, form);
  const portaPadrao = tipo === "sqlserver" ? "1433" : "5432";
  // Em CRIAÇÃO, tudo trava após salvar (criadaId). Em EDIÇÃO, os campos de config ficam editáveis;
  // só nome/tipo são imutáveis (renomear é uma ação à parte; trocar de tipo recria a conexão).
  const camposBloqueados = !!criadaId && !editando;
  // O TIPO é imutável (trocar de tipo é cadastrar outra conexão); o NOME é editável na edição —
  // não faz sentido abrir o editor e não poder corrigir um erro de digitação no nome.
  const tipoBloqueado = !!criadaId || editando;

  return (
    <>
      <div className="page-head">
        <button className="link-back" onClick={onVoltar}>
          ← Conexões
        </button>
        <h1 className="display">{editando ? "Editar conexão" : "Nova conexão"}</h1>
        <p className="sub">
          {editando
            ? "Ajuste como chegar na fonte. Deixe a senha/token em branco para manter a credencial atual; preencha para rotacioná-la (vai para o cofre cifrado)."
            : "Escolha o tipo de fonte e preencha como chegar nela. A senha ou token vai para o cofre cifrado — não fica nos metadados nem aparece de volta."}
        </p>
      </div>

      <div className="pad">
        {erroForm && <Aviso erro={erroForm} />}

        {criadaId && !editando && (
          <div className="run-banner" role="status" style={{ marginTop: 0, marginBottom: 14 }}>
            <span className="dot" />
            <span>
              Conexão criada. Use <b>Testar</b> para validar o acesso ou volte à lista.
            </span>
          </div>
        )}

        {editando && editado && (
          <div
            className="run-banner"
            role="status"
            data-testid="conexao-editada"
            style={{ marginTop: 0, marginBottom: 14 }}
          >
            <span className="dot" />
            <span>
              Alterações salvas. Use <b>Testar</b> para validar o acesso ou volte à lista.
            </span>
          </div>
        )}

        <div className="card shelf" style={{ marginBottom: 14, padding: "13px 15px" }}>
          {/* O <h4> do card é o rótulo do campo (via aria-labelledby): repetir um segundo rótulo em
              caixa alta logo abaixo virava duas linhas berrando a mesma coisa. */}
          <h4 id="rot-nome">Nome</h4>
          <input
            className="nm"
            data-testid="conexao-nome"
            aria-labelledby="rot-nome"
            aria-describedby="dica-nome"
            value={nome}
            disabled={camposBloqueados}
            onChange={(e) => setNome(e.target.value)}
            placeholder="Ex.: SQL de produção"
          />
          <p id="dica-nome" style={{ fontSize: 12, color: "var(--ink-2)", marginTop: 6 }}>
            {editando ? (
              <>
                Como esta conexão aparece na lista e nos nós de fonte. Trocar o nome aqui já
                renomeia (fica na trilha de auditoria) — os nós continuam apontando para ela.
              </>
            ) : (
              "Como esta conexão aparece na lista e nos nós de fonte."
            )}
          </p>
        </div>

        <div className="card shelf" style={{ marginBottom: 14, padding: "13px 15px" }}>
          <h4 id="rot-tipo">Tipo</h4>
          <select
            className="nm"
            data-testid="conexao-tipo"
            aria-labelledby="rot-tipo"
            aria-describedby="dica-tipo"
            value={tipo}
            disabled={tipoBloqueado}
            onChange={(e) => trocarTipo(e.target.value as Tipo)}
          >
            {TIPOS.map((t) => (
              <option key={t.valor} value={t.valor}>
                {t.rotulo} — {t.meta}
              </option>
            ))}
          </select>
          <p id="dica-tipo" style={{ fontSize: 12, color: "var(--ink-2)", marginTop: 6 }}>
            {tipoBloqueado
              ? "O tipo não muda depois de criada — trocar de tipo é cadastrar outra conexão."
              : "Define os campos de acesso abaixo."}
          </p>
        </div>

        <div className="card shelf" style={{ marginBottom: 14, padding: "13px 15px" }}>
          <h4>Como conectar</h4>

          {(tipo === "sqlserver" || tipo === "postgres") && (
            <div style={{ display: "grid", gridTemplateColumns: "minmax(220px,1fr) 140px", gap: 12 }}>
              <Campo label="Host">
                <input
                  className="nm"
                  data-testid="conexao-host"
                  value={form.host}
                  disabled={camposBloqueados}
                  onChange={(e) => set("host", e.target.value)}
                  placeholder="servidor.interno"
                />
              </Campo>
              <Campo label="Porta">
                <input
                  className="nm"
                  data-testid="conexao-porta"
                  inputMode="numeric"
                  value={form.porta}
                  disabled={camposBloqueados}
                  onChange={(e) => set("porta", e.target.value)}
                  placeholder={portaPadrao}
                />
              </Campo>
              <Campo label="Base">
                <input
                  className="nm"
                  data-testid="conexao-base"
                  value={form.base}
                  disabled={camposBloqueados}
                  onChange={(e) => set("base", e.target.value)}
                  placeholder="nome do banco"
                />
              </Campo>
              <Campo label="Usuário">
                <input
                  className="nm"
                  data-testid="conexao-usuario"
                  autoComplete="off"
                  value={form.usuario}
                  disabled={camposBloqueados}
                  onChange={(e) => set("usuario", e.target.value)}
                  placeholder="usuário"
                />
              </Campo>
            </div>
          )}

          {tipo === "api_rest" && (
            <div style={{ display: "grid", gridTemplateColumns: "1fr", gap: 12 }}>
              <Campo label="URL">
                <input
                  className="nm"
                  data-testid="conexao-url"
                  value={form.url}
                  disabled={camposBloqueados}
                  onChange={(e) => set("url", e.target.value)}
                  placeholder="https://api.interna/recurso"
                />
              </Campo>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                <Campo label="Método">
                  <select
                    className="nm"
                    value={form.metodo}
                    disabled={camposBloqueados}
                    onChange={(e) => set("metodo", e.target.value)}
                  >
                    <option value="GET">GET</option>
                    <option value="POST">POST</option>
                  </select>
                </Campo>
                <Campo label="Autenticação">
                  <select
                    className="nm"
                    value={form.auth_tipo}
                    disabled={camposBloqueados}
                    onChange={(e) => set("auth_tipo", e.target.value as AuthTipo)}
                  >
                    <option value="none">Nenhuma</option>
                    <option value="bearer">Bearer (token)</option>
                    <option value="header">Header customizado</option>
                  </select>
                </Campo>
              </div>
              {form.auth_tipo === "header" && (
                <Campo label="Nome do header">
                  <input
                    className="nm"
                    value={form.auth_header}
                    disabled={camposBloqueados}
                    onChange={(e) => set("auth_header", e.target.value)}
                    placeholder="X-API-Key"
                  />
                </Campo>
              )}
              <Campo label="Caminho do JSON (opcional)">
                <input
                  className="nm"
                  value={form.json_path}
                  disabled={camposBloqueados}
                  onChange={(e) => set("json_path", e.target.value)}
                  placeholder="ex.: dados.itens"
                />
              </Campo>
            </div>
          )}

          {tipo === "api_query" && (
            <div style={{ display: "grid", gridTemplateColumns: "1fr", gap: 12 }}>
              <Campo label="URL">
                <input
                  className="nm"
                  data-testid="conexao-url"
                  value={form.url}
                  disabled={camposBloqueados}
                  onChange={(e) => set("url", e.target.value)}
                  placeholder="http://host:5000"
                />
              </Campo>
              <Campo label="Usuário">
                <input
                  className="nm"
                  data-testid="conexao-usuario"
                  autoComplete="off"
                  value={form.usuario}
                  disabled={camposBloqueados}
                  onChange={(e) => set("usuario", e.target.value)}
                  placeholder="usuário do login"
                />
              </Campo>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                <Campo label="Caminho do login">
                  <input
                    className="nm"
                    data-testid="conexao-login-path"
                    value={form.login_path}
                    disabled={camposBloqueados}
                    onChange={(e) => set("login_path", e.target.value)}
                    placeholder="/api/v1/auth/login"
                  />
                </Campo>
                <Campo label="Caminho da consulta">
                  <input
                    className="nm"
                    data-testid="conexao-query-path"
                    value={form.query_path}
                    disabled={camposBloqueados}
                    onChange={(e) => set("query_path", e.target.value)}
                    placeholder="/api/v1/query"
                  />
                </Campo>
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12 }}>
                <Campo label="Campo do token">
                  <input
                    className="nm"
                    data-testid="conexao-token-campo"
                    value={form.token_campo}
                    disabled={camposBloqueados}
                    onChange={(e) => set("token_campo", e.target.value)}
                    placeholder="token"
                  />
                </Campo>
                <Campo label="Campo dos resultados">
                  <input
                    className="nm"
                    data-testid="conexao-results-campo"
                    value={form.results_campo}
                    disabled={camposBloqueados}
                    onChange={(e) => set("results_campo", e.target.value)}
                    placeholder="results"
                  />
                </Campo>
                <Campo label="Campo do SQL">
                  <input
                    className="nm"
                    data-testid="conexao-sql-campo"
                    value={form.sql_campo}
                    disabled={camposBloqueados}
                    onChange={(e) => set("sql_campo", e.target.value)}
                    placeholder="sql"
                  />
                </Campo>
              </div>
            </div>
          )}

          {tipo === "arquivo" && (
            <div style={{ display: "grid", gridTemplateColumns: "1fr", gap: 12 }}>
              <Campo label="Subir arquivo">
                <input
                  className="nm"
                  type="file"
                  data-testid="conexao-arquivo-upload"
                  accept=".csv,.xlsx,.xls,.parquet"
                  disabled={camposBloqueados || subindo}
                  onChange={(e) => {
                    const file = e.target.files?.[0];
                    if (file) void subirArquivo(file);
                  }}
                />
                <p style={{ fontSize: 12, color: "var(--ink-2)", marginTop: 6 }}>
                  Suba um CSV/Excel ou informe um caminho no servidor.
                </p>
                {subindo && (
                  <p role="status" style={{ fontSize: 12, color: "var(--ink-2)", marginTop: 6 }}>
                    Enviando arquivo…
                  </p>
                )}
                {arquivoNome && !subindo && (
                  <p
                    role="status"
                    data-testid="conexao-arquivo-confirmado"
                    style={{ fontSize: 12, color: "var(--ink-2)", marginTop: 6 }}
                  >
                    Arquivo enviado: <b>{arquivoNome}</b>
                  </p>
                )}
              </Campo>
              <div style={{ display: "grid", gridTemplateColumns: "minmax(220px,1fr) 200px", gap: 12 }}>
                <Campo label="Caminho">
                  <input
                    className="nm"
                    data-testid="conexao-caminho"
                    value={form.caminho}
                    disabled={camposBloqueados}
                    onChange={(e) => set("caminho", e.target.value)}
                    placeholder="/dados/arquivo.csv"
                  />
                </Campo>
                <Campo label="Formato">
                  <select
                    className="nm"
                    value={form.formato}
                    disabled={camposBloqueados}
                    onChange={(e) => set("formato", e.target.value as FormState["formato"])}
                  >
                    <option value="csv">CSV</option>
                    <option value="xlsx">Excel (xlsx)</option>
                    <option value="parquet">Parquet</option>
                  </select>
                </Campo>
              </div>
              {form.formato === "xlsx" && (
                <Campo label="Aba (planilha)">
                  <input
                    className="nm"
                    data-testid="conexao-aba"
                    value={form.aba}
                    disabled={camposBloqueados}
                    onChange={(e) => set("aba", e.target.value)}
                    placeholder="vazio = primeira aba"
                  />
                </Campo>
              )}
            </div>
          )}

          {tipo === "smtp" && (
            <div style={{ display: "grid", gridTemplateColumns: "1fr", gap: 12 }}>
              <div style={{ display: "grid", gridTemplateColumns: "minmax(220px,1fr) 120px 180px", gap: 12 }}>
                <Campo label="Host">
                  <input
                    className="nm"
                    data-testid="conexao-host"
                    value={form.host}
                    disabled={camposBloqueados}
                    onChange={(e) => set("host", e.target.value)}
                    placeholder="smtp.provedor.com.br"
                  />
                </Campo>
                <Campo label="Porta">
                  <input
                    className="nm"
                    data-testid="conexao-porta"
                    inputMode="numeric"
                    value={form.porta}
                    disabled={camposBloqueados}
                    onChange={(e) => set("porta", e.target.value)}
                    placeholder="587"
                  />
                </Campo>
                <Campo label="Segurança">
                  <select
                    className="nm"
                    data-testid="conexao-seguranca"
                    value={form.seguranca}
                    disabled={camposBloqueados}
                    onChange={(e) => set("seguranca", e.target.value as FormState["seguranca"])}
                  >
                    <option value="starttls">STARTTLS (587)</option>
                    <option value="ssl">SSL/TLS (465)</option>
                    <option value="nenhuma">Sem criptografia (25)</option>
                  </select>
                </Campo>
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                <Campo label="Usuário (autenticação)">
                  <input
                    className="nm"
                    data-testid="conexao-usuario"
                    autoComplete="off"
                    value={form.usuario}
                    disabled={camposBloqueados}
                    onChange={(e) => set("usuario", e.target.value)}
                    placeholder="no-reply@example.com"
                  />
                </Campo>
                <Campo label="Remetente (opcional)">
                  <input
                    className="nm"
                    data-testid="conexao-remetente"
                    value={form.remetente}
                    disabled={camposBloqueados}
                    onChange={(e) => set("remetente", e.target.value)}
                    placeholder="em branco = o usuário acima"
                  />
                </Campo>
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 140px", gap: 12 }}>
                <Campo label="Nome exibido (opcional)">
                  <input
                    className="nm"
                    data-testid="conexao-remetente-nome"
                    value={form.remetente_nome}
                    disabled={camposBloqueados}
                    onChange={(e) => set("remetente_nome", e.target.value)}
                    placeholder="Acme"
                  />
                </Campo>
                <Campo label="Tentativas">
                  <input
                    className="nm"
                    data-testid="conexao-tentativas"
                    inputMode="numeric"
                    value={form.tentativas}
                    disabled={camposBloqueados}
                    onChange={(e) => set("tentativas", e.target.value)}
                    placeholder="3"
                  />
                </Campo>
              </div>
              <p style={{ fontSize: 12, color: "var(--ink-2)", margin: 0 }}>
                Cadastre um servidor por conexão: cada nó de e-mail escolhe por qual SMTP envia.
                Testar a conexão autentica e <b>não envia</b> mensagem. Para um nó usar esta conexão,
                um admin precisa habilitar <b>permitir escrita</b> na lista.
              </p>
            </div>
          )}
        </div>

        {mostrarSegredo && (
          <div className="card shelf" style={{ marginBottom: 14, padding: "13px 15px" }}>
            <h4 id="rot-segredo">
              {tipo === "api_rest"
                ? "Token / chave de acesso"
                : tipo === "api_query"
                  ? "Senha do login"
                  : tipo === "smtp"
                    ? "Senha do e-mail (SMTP)"
                    : "Senha"}
            </h4>
            <input
              className="nm"
              data-testid="conexao-segredo"
              aria-labelledby="rot-segredo"
              type="password"
              value={form.segredo}
              disabled={camposBloqueados}
              onChange={(e) => set("segredo", e.target.value)}
              placeholder={editando ? "em branco = manter a credencial atual" : "não será exibida de volta"}
              autoComplete="new-password"
              aria-describedby="conexao-segredo-nota"
            />
            <p id="conexao-segredo-nota" style={{ fontSize: 12, color: "var(--ink-2)", marginTop: 6 }}>
              {editando
                ? "Deixe em branco para manter a credencial atual; preencha para rotacionar. Guardada no cofre cifrado, separada dos metadados — a plataforma não a exibe de volta em nenhuma tela."
                : "Guardada no cofre cifrado, separada dos metadados. A plataforma não a exibe de volta em nenhuma tela — para trocar, é só digitar a nova."}
            </p>
          </div>
        )}

        <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          {/* Em CRIAÇÃO: botão Salvar some após criar (vira Testar/Voltar). Em EDIÇÃO: sempre disponível. */}
          {(editando || !criadaId) && (
            <button
              className="btn-stamp"
              data-testid="salvar-conexao"
              disabled={salvando}
              onClick={salvar}
            >
              <Icon name="stamp" cls="ic" />{" "}
              {salvando ? "Salvando…" : editando ? "Salvar alterações" : "Salvar conexão"}
            </button>
          )}
          {criadaId && (
            <>
              <button
                className="pag-btn"
                data-testid="testar-conexao"
                disabled={testando}
                onClick={testar}
              >
                {testando ? "Testando…" : "Testar acesso"}
              </button>
              <button className="pag-btn" onClick={onVoltar}>
                Voltar à lista
              </button>
            </>
          )}
          <span style={{ fontSize: 12, color: "var(--ink-2)" }}>
            {editando
              ? "Salve as alterações; teste o acesso ou volte à lista."
              : criadaId
                ? "Teste o acesso ou volte à lista."
                : "A credencial vai para o cofre cifrado."}
          </span>
        </div>

        {teste && (
          <div
            className={teste.ok ? "run-banner" : "exec-banner-falha"}
            role="status"
            style={{ marginTop: 14 }}
          >
            <span className="dot" />
            <span>{teste.texto}</span>
          </div>
        )}
      </div>
    </>
  );
}

// --- Campo rotulado (label .fld + controle .nm do design system) ---
function Campo({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label style={{ display: "block" }}>
      <span className="fld">{label}</span>
      {children}
    </label>
  );
}
