import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSelector } from "react-redux";

import * as api from "../../api/client";
import {
  ConfirmarAcao,
  GridLista,
  Icon,
  MenuMaisAcoes,
  fecharMenuDoBotao,
  type ItemCadastro,
} from "../ui";
import { fmtMomento, fmtMomentoCurto } from "../execucao/formato";
import type { RootState } from "../../store/store";

// ─────────────────────────────────────────────────────────────────────────
// Usuários (admin) — quem entra na plataforma e com qual perfil.
//
// Duas ações desta tela mudam quem tem acesso ao produto (trocar o perfil e suspender/liberar o
// acesso). As duas são DELIBERADAS: escolher o perfil acontece dentro do diálogo que descreve o
// efeito, não num `<select>` na linha — no select, a seta do teclado troca o valor a cada opção
// percorrida, e a mudança de segurança virava um efeito colateral de navegar pela lista.
//
// ÚLTIMO ACESSO vem da TRILHA (evento de login), não do cadastro: a coluna `usuarios.ultimo_acesso`
// existe mas nada a preenche hoje, e a tela mostrava "aguardando 1º acesso" para TODO mundo —
// inclusive para o admin que estava logado naquele instante. Enquanto o backend não a preencher, a
// verdade está no `acao=login` da trilha; quando passar a preencher, o valor do cadastro vence.
//
// DUAS FAIXAS DE ALCANCE, UMA TELA. O admin administra todo mundo. O ESPECIALISTA cria e
// administra apenas OPERADOR (a API já devolve só esses no `GET /usuarios`) e não troca perfil de
// ninguém — é o que impede criar um operador e promovê-lo a admin no passo seguinte. Por isso a
// tela não mostra "Trocar perfil" a quem não é admin: controle que só existe para responder 403 é
// pior que controle ausente. Enquanto `eu` não chegou, o alcance é o MENOR (falha fechada).
//
// A senha NUNCA passa por esta tela: criar usuário emite um token de primeiro acesso entregue
// pelo canal de notificação configurado (fora da plataforma).
//
// A TELA NÃO AFIRMA QUE O E-MAIL SAIU. A entrega pelo canal acontece depois do commit do cadastro
// e pode falhar sozinha (relay fora do ar, domínio digitado errado). A API responde
// `email_entregue`; quando é `false` o aviso diz isso e aponta o conserto — antes a tela dizia
// "link enviado" mesmo quando nada saiu, e o usuário ficava criado, sem senha e sem como definir.
// Daí as duas ações de conserto: REENVIAR ACESSO (token novo, o antigo só existia em memória) e
// CORRIGIR E-MAIL (endereço de entrega E identificador de login; como a plataforma não exclui
// usuário, digitação errada só se resolvia suspendendo e recriando).
// ─────────────────────────────────────────────────────────────────────────

type Perfil = "admin" | "especialista" | "operador" | "auditor" | "servico";

interface Usuario {
  id: string;
  email: string;
  nome: string;
  perfil: Perfil;
  ativo: boolean;
  ultimo_acesso: string | null;
}

const PERFIS: { valor: Perfil; rotulo: string; oque: string }[] = [
  {
    valor: "admin",
    rotulo: "Administrador",
    oque: "Gere usuários, grupos e permissões, e passa em qualquer recurso (bypass).",
  },
  {
    valor: "especialista",
    rotulo: "Especialista",
    oque: "Constrói pipeline, cubo, view e página. Não gere identidade nem acesso.",
  },
  {
    valor: "operador",
    rotulo: "Operador",
    oque: "Usa o Portal: filtros liberados e geração de relatório. Não edita nada.",
  },
  {
    valor: "auditor",
    rotulo: "Auditor",
    oque: "Consulta a trilha de auditoria e o que foi carimbado. Somente leitura.",
  },
  {
    valor: "servico",
    rotulo: "Serviço (integração)",
    oque:
      "Conta de integração (ex.: um agendador externo): entra só por token de API, executa os " +
      "pipelines em que tem permissão e acompanha as execuções. Não cria nem edita nada, e não " +
      "tem senha.",
  },
];

function rotuloPerfil(p: Perfil): string {
  return PERFIS.find((x) => x.valor === p)?.rotulo ?? p;
}

// Cadeado (11px, altura da linha do texto) para a linha do próprio usuário. O conjunto
// compartilhado (`ui.tsx`) não tem cadeado; quando tiver, este SVG local sai daqui (ver relatório).
function IconeCadeado() {
  return (
    <svg width="11" height="11" viewBox="0 0 20 20" aria-hidden="true" focusable="false">
      <g fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round">
        <rect x="4.5" y="8.5" width="11" height="8" rx="1.6" />
        <path d="M7.2 8.5V6.4a2.8 2.8 0 0 1 5.6 0v2.1" />
      </g>
    </svg>
  );
}

// Por que a própria linha não tem ação (o texto do `title` e o do rodapé dizem a MESMA coisa).
const MOTIVO_SELF =
  "Segurança: ninguém troca o próprio perfil nem suspende o próprio acesso — é o que impede um " +
  "administrador de se trancar fora da plataforma. Outro administrador faz isso por você.";

// --- Cliente local (estes endpoints não estão em api/client.ts) ---
// Reusa o `apiFetch` do cliente compartilhado (mesmo Bearer, mesmo tratamento de sessão expirada).
function reqUsuarios(metodo: string, caminho: string, corpo?: unknown): Promise<Response> {
  return api.apiFetch(metodo, caminho, corpo);
}

interface FalhaApi {
  mensagem: string;
  requestId: string | null;
}

// Envelope de erro pt-BR (`{erro:{codigo,mensagem,request_id,detalhes}}`): mostra a `mensagem` e
// guarda o `request_id` — é o que o suporte pede quando a mensagem não basta.
// `contexto` existe porque um 403 aqui quer dizer duas coisas diferentes: na LISTA é "esta tela
// não é sua" (operador/auditor); numa AÇÃO é "este usuário não é seu" (especialista mirando quem
// não é operador). A mensagem antiga dizia "restrita a administradores" nos dois casos — e virou
// mentira no dia em que o especialista passou a administrar operador.
async function falhaDaResposta(
  r: Response,
  fallback: string,
  contexto: "lista" | "acao" = "acao",
): Promise<FalhaApi> {
  const j = await r.json().catch(() => null);
  const e = (j as any)?.erro;
  const requestId = e?.request_id ?? r.headers.get("X-Request-Id") ?? null;
  if (r.status === 403) {
    return {
      mensagem:
        contexto === "lista"
          ? "Esta tela é restrita a administradores e especialistas."
          : "Você não tem permissão para esta ação neste usuário. O especialista administra " +
            "apenas operadores; os demais perfis são com um administrador.",
      requestId,
    };
  }
  if (!e?.mensagem) return { mensagem: fallback, requestId };
  const det = (e.detalhes ?? [])
    .map((d: any) => d?.mensagem)
    .filter(Boolean)
    .join(" · ");
  return { mensagem: det ? `${e.mensagem} — ${det}` : e.mensagem, requestId };
}

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;

type Pendente =
  | { tipo: "perfil"; usuario: Usuario }
  | { tipo: "acesso"; usuario: Usuario }
  | { tipo: "reenvio"; usuario: Usuario }
  | { tipo: "email"; usuario: Usuario }
  | { tipo: "tokens"; usuario: Usuario };

// Aviso de sucesso x aviso de atenção: "usuário criado, mas o e-mail não saiu" não é sucesso.
type Aviso = { texto: string; tom: "ok" | "aten" };

// Como a tela apurou o último acesso. `parcial` = a trilha tem mais eventos do que foi possível
// ler de uma vez (o que está na tela pode ser mais antigo que o real); `falha` = não deu para ler.
type EstadoTrilha = "carregando" | "ok" | "parcial" | "falha";

// Escolha do perfil DENTRO da confirmação: escolher e confirmar são o mesmo passo deliberado.
// Não usa `ConfirmarAcao` (que só aceita um campo de texto), mas reusa o mesmo diálogo do design
// system — `.modal-fundo`/`.modal-caixa`/`.modal-acoes`, Escape fecha, foco devolvido ao sair.
function ModalPerfil({
  usuario,
  ocupado,
  onConfirmar,
  onCancelar,
}: {
  usuario: Usuario;
  ocupado: boolean;
  onConfirmar: (novo: Perfil) => void;
  onCancelar: () => void;
}) {
  const [escolha, setEscolha] = useState<Perfil>(usuario.perfil);
  const primeiro = useRef<HTMLInputElement | null>(null);
  useEffect(() => {
    const anterior = document.activeElement as HTMLElement | null;
    primeiro.current?.focus();
    return () => anterior?.focus?.();
  }, []);
  const mudou = escolha !== usuario.perfil;
  const viraAdmin = escolha === "admin" && usuario.perfil !== "admin";
  return (
    <div
      className="modal-fundo"
      role="dialog"
      aria-modal="true"
      aria-labelledby="perfil-tit"
      onClick={(e) => e.target === e.currentTarget && onCancelar()}
      onKeyDown={(e) => e.key === "Escape" && onCancelar()}
    >
      <div className="modal-caixa">
        <h3 id="perfil-tit">Trocar o perfil de {usuario.nome}</h3>
        <p>
          Perfil é a capacidade geral na plataforma — vale já no próximo carregamento de tela, para
          todos os recursos, e a troca vai para a trilha de auditoria.
        </p>
        {/* Reset do fieldset inline: sem CSS próprio ele viria com a borda "groove" do navegador
            dentro do modal do produto (ver "CSS a aplicar" no relatório). */}
        <fieldset
          className="adm-escolha"
          style={{
            border: "none",
            margin: "0 0 14px",
            padding: 0,
            minWidth: 0,
            overflow: "auto",
            display: "flex",
            flexDirection: "column",
            gap: 8,
          }}
        >
          <legend className="eyebrow" style={{ padding: 0, marginBottom: 6 }}>
            Novo perfil
          </legend>
          {PERFIS.map((p, i) => (
            <label
              key={p.valor}
              className={"adm-escolha-item" + (escolha === p.valor ? " on" : "")}
              style={{ display: "flex", alignItems: "flex-start", gap: 8, fontSize: 13 }}
            >
              <input
                ref={i === 0 ? primeiro : undefined}
                type="radio"
                name="novo-perfil"
                value={p.valor}
                checked={escolha === p.valor}
                data-testid={`usuarios-perfil-opcao-${p.valor}`}
                onChange={() => setEscolha(p.valor)}
              />
              <span>
                <b>{p.rotulo}</b>
                {p.valor === usuario.perfil && " (atual)"}
                <span style={{ display: "block", fontSize: 12, color: "var(--ink-2)" }}>
                  {p.oque}
                </span>
              </span>
            </label>
          ))}
        </fieldset>
        {viraAdmin && (
          <p className="adm-form-erro" role="status" style={{ marginBottom: 14 }}>
            Administrador gere identidade e acesso, e passa em qualquer recurso — inclusive nos que
            não são dele.
          </p>
        )}
        <div className="modal-acoes">
          <button type="button" className="btn-sec" onClick={onCancelar}>
            Cancelar
          </button>
          <button
            type="button"
            className={viraAdmin ? "btn-perigo" : "btn-stamp"}
            disabled={!mudou || ocupado}
            data-testid="usuarios-perfil-confirmar"
            onClick={() => onConfirmar(escolha)}
          >
            {mudou
              ? `Trocar para ${rotuloPerfil(escolha)}`
              : `Já é ${rotuloPerfil(usuario.perfil)}`}
          </button>
        </div>
      </div>
    </div>
  );
}

interface TokenApi {
  id: string;
  nome: string;
  prefixo: string;
  criado_em: string | null;
  expira_em: string | null;
  ultimo_uso_em: string | null;
  revogado_em: string | null;
}

function estadoToken(t: TokenApi): { texto: string; cls: string } {
  if (t.revogado_em) return { texto: "revogado", cls: "off" };
  if (t.expira_em && new Date(t.expira_em).getTime() <= Date.now()) return { texto: "vencido", cls: "falha" };
  return { texto: "válido", cls: "ok" };
}

// Tokens de API da conta de SERVIÇO. O valor aparece UMA vez, logo depois de emitir — no
// banco fica só o hash, e esta tela não tem como mostrá-lo de novo. Revogar vale já no pedido
// seguinte do integrador. A confirmação de revogar é uma linha dentro do próprio modal: modal sobre
// modal esconde a lista que se está editando.
function ModalTokens({ usuario, onFechar }: { usuario: Usuario; onFechar: () => void }) {
  const [tokens, setTokens] = useState<TokenApi[] | null>(null);
  const [erro, setErro] = useState<string | null>(null);
  const [nome, setNome] = useState("");
  const [dias, setDias] = useState(90);
  const [emitido, setEmitido] = useState<{ token: string; prefixo: string; expira_em: string } | null>(null);
  const [ocupado, setOcupado] = useState(false);
  const [revogando, setRevogando] = useState<TokenApi | null>(null);
  const campo = useRef<HTMLInputElement | null>(null);

  const carregar = useCallback(async () => {
    const r = await reqUsuarios("GET", `/usuarios/${usuario.id}/tokens`);
    if (!r.ok) {
      setErro((await falhaDaResposta(r, "Falha ao carregar os tokens.")).mensagem);
      return;
    }
    setTokens(await r.json());
  }, [usuario.id]);

  useEffect(() => {
    void carregar();
  }, [carregar]);
  useEffect(() => {
    const anterior = document.activeElement as HTMLElement | null;
    campo.current?.focus();
    return () => anterior?.focus?.();
  }, []);

  const emitir = async () => {
    setErro(null);
    if (!nome.trim()) {
      setErro("Dê um nome ao token — onde ele vai morar (ex.: agendador externo — servidor de jobs).");
      return;
    }
    setOcupado(true);
    try {
      const r = await reqUsuarios("POST", `/usuarios/${usuario.id}/tokens`, { nome: nome.trim(), dias });
      if (!r.ok) {
        setErro((await falhaDaResposta(r, "Falha ao emitir o token.")).mensagem);
        return;
      }
      setEmitido(await r.json());
      setNome("");
      await carregar();
    } catch {
      setErro("Falha de rede ao emitir o token.");
    } finally {
      setOcupado(false);
    }
  };

  const revogar = async (t: TokenApi) => {
    setErro(null);
    setOcupado(true);
    try {
      const r = await reqUsuarios("DELETE", `/usuarios/${usuario.id}/tokens/${t.id}`);
      if (!r.ok) {
        setErro((await falhaDaResposta(r, "Falha ao revogar o token.")).mensagem);
        return;
      }
      setRevogando(null);
      await carregar();
    } catch {
      setErro("Falha de rede ao revogar o token.");
    } finally {
      setOcupado(false);
    }
  };

  const cel: React.CSSProperties = { padding: "5px 8px", borderBottom: "1px solid var(--hair)", textAlign: "left" };
  return (
    <div
      className="modal-fundo"
      role="dialog"
      aria-modal="true"
      aria-labelledby="tokens-tit"
      data-testid="modal-tokens"
      onClick={(e) => e.target === e.currentTarget && onFechar()}
      onKeyDown={(e) => e.key === "Escape" && onFechar()}
    >
      <div className="modal-caixa" style={{ maxWidth: 760 }}>
        <h3 id="tokens-tit">Tokens de API — {usuario.nome}</h3>
        <p>
          Conta de serviço entra só por token. Dê a cada token o nome de onde ele vai morar, e revogue
          o que saiu de uso. O valor aparece <b>uma vez</b>, logo depois de emitir.
        </p>
        {emitido && (
          <div className="exec-aviso" data-testid="token-emitido" style={{ display: "block", marginBottom: 12 }}>
            <b>Guarde agora</b> — este valor não aparece de novo (vale até {fmtMomento(emitido.expira_em)}):
            <input
              readOnly
              value={emitido.token}
              data-testid="token-valor"
              aria-label="Valor do token emitido"
              onFocus={(e) => e.target.select()}
              style={{ display: "block", width: "100%", fontFamily: "monospace", margin: "6px 0" }}
            />
            <button
              type="button"
              className="btn-sec"
              onClick={() => void navigator.clipboard?.writeText(emitido.token)}
            >
              Copiar
            </button>
          </div>
        )}
        <div style={{ display: "flex", gap: 10, alignItems: "flex-end", flexWrap: "wrap", marginBottom: 10 }}>
          <label style={{ display: "flex", flexDirection: "column", flex: "1 1 260px" }}>
            <span className="fld">Nome do token</span>
            <input
              ref={campo}
              className="nm"
              data-testid="token-nome"
              value={nome}
              placeholder="ex.: agendador externo — servidor de jobs"
              onChange={(e) => setNome(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && void emitir()}
            />
          </label>
          <label style={{ display: "flex", flexDirection: "column", width: 130 }}>
            <span className="fld">Validade (dias)</span>
            <input
              className="nm"
              type="number"
              min={1}
              max={365}
              data-testid="token-dias"
              value={dias}
              onChange={(e) => setDias(Number(e.target.value))}
            />
          </label>
          <button type="button" className="btn-stamp" data-testid="token-emitir" disabled={ocupado} onClick={() => void emitir()}>
            Emitir token
          </button>
        </div>
        {erro && (
          <p className="adm-form-erro" role="alert">
            {erro}
          </p>
        )}
        {tokens && tokens.length > 0 ? (
          <div style={{ overflowX: "auto" }}>
            <table style={{ borderCollapse: "collapse", width: "100%", fontSize: 13 }}>
              <thead>
                <tr>
                  {["Nome", "Começa com", "Emitido", "Vence", "Último uso", "Estado", ""].map((c) => (
                    <th key={c} style={cel}>
                      {c}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {tokens.map((t) => {
                  const est = estadoToken(t);
                  return (
                    <tr key={t.id} data-testid={`token-linha-${t.prefixo}`}>
                      <td style={cel}>{t.nome}</td>
                      <td style={{ ...cel, fontFamily: "monospace" }}>{t.prefixo}…</td>
                      <td style={cel}>{fmtMomento(t.criado_em)}</td>
                      <td style={cel}>{fmtMomento(t.expira_em)}</td>
                      <td style={cel}>{t.ultimo_uso_em ? fmtMomento(t.ultimo_uso_em) : "nunca usado"}</td>
                      <td style={cel}>
                        <span className={`statuspill ${est.cls}`}>
                          <span className="d" />
                          {est.texto}
                        </span>
                      </td>
                      <td style={cel}>
                        {!t.revogado_em &&
                          (revogando?.id === t.id ? (
                            <span style={{ display: "flex", gap: 6, alignItems: "center" }}>
                              o integrador leva 401 no próximo pedido
                              <button type="button" className="btn-sec" onClick={() => setRevogando(null)}>
                                Cancelar
                              </button>
                              <button
                                type="button"
                                className="btn-perigo"
                                data-testid="token-revogar-confirmar"
                                disabled={ocupado}
                                onClick={() => void revogar(t)}
                              >
                                Revogar
                              </button>
                            </span>
                          ) : (
                            <button
                              type="button"
                              className="btn-sec"
                              data-testid={`token-revogar-${t.prefixo}`}
                              onClick={() => setRevogando(t)}
                            >
                              Revogar…
                            </button>
                          ))}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : tokens ? (
          <p data-testid="tokens-vazio">Nenhum token emitido — a conta ainda não consegue entrar.</p>
        ) : null}
        <div className="modal-acoes">
          <button type="button" className="btn-sec" data-testid="tokens-fechar" onClick={onFechar}>
            Fechar
          </button>
        </div>
      </div>
    </div>
  );
}

export default function Usuarios(): JSX.Element {
  // Só para reagir à (re)autenticação: o Bearer em si vem do módulo `api`.
  const token = useSelector((s: RootState) => s.studio.token);

  const [usuarios, setUsuarios] = useState<Usuario[]>([]);
  const [carregando, setCarregando] = useState(true);
  const [erro, setErro] = useState<FalhaApi | null>(null);
  const [eu, setEu] = useState<api.UsuarioAtual | null>(null);
  // Alcance de quem está olhando. `eu` chega por `GET /auth/me` depois da 1ª pintura: até lá
  // `souAdmin` é falso, e a tela oferece o MENOS — mostrar "Trocar perfil" por um instante e
  // recolher depois é pior que nunca mostrar. Declarado AQUI, logo após `eu`, porque efeitos e
  // handlers abaixo o leem: o array de dependências é avaliado no render, e ler antes da
  // declaração é ReferenceError (a tela inteira quebrava).
  const souAdmin = eu?.perfil === "admin";
  const perfisQuePossoCriar = souAdmin ? PERFIS : PERFIS.filter((p) => p.valor === "operador");
  const substantivo = souAdmin ? "usuário" : "operador";
  // Plural à parte: `usuário + "es"` dava "usuárioes" na contagem do admin.
  const substantivoPlural = souAdmin ? "usuários" : "operadores";
  // Último login por usuário, apurado na trilha (ver cabeçalho do arquivo).
  const [ultimoLogin, setUltimoLogin] = useState<Record<string, string>>({});
  const [trilha, setTrilha] = useState<EstadoTrilha>("carregando");

  // filtros (busca por nome/e-mail + perfil + situação de acesso)
  const [busca, setBusca] = useState("");
  const [fPerfil, setFPerfil] = useState<Perfil | "">("");
  const [fSituacao, setFSituacao] = useState<"" | "liberado" | "suspenso">("");

  // formulário "Novo usuário"
  const [criando, setCriando] = useState(false);
  const [novoEmail, setNovoEmail] = useState("");
  const [novoNome, setNovoNome] = useState("");
  const [novoPerfil, setNovoPerfil] = useState<Perfil>("operador");
  const [salvando, setSalvando] = useState(false);
  const [erroForm, setErroForm] = useState<string | null>(null);
  const [aviso, setAviso] = useState<Aviso | null>(null);
  const primeiroCampo = useRef<HTMLInputElement | null>(null);

  // linhas em operação (id → true) para desabilitar controles
  const [ocupado, setOcupado] = useState<Record<string, boolean>>({});
  // ação sensível aguardando confirmação no modal do design system
  const [pendente, setPendente] = useState<Pendente | null>(null);

  const carregar = useCallback(async () => {
    setCarregando(true);
    setErro(null);
    try {
      const r = await reqUsuarios("GET", "/usuarios");
      if (!r.ok) {
        setErro(await falhaDaResposta(r, "Falha ao carregar usuários.", "lista"));
        setUsuarios([]);
        return;
      }
      setUsuarios(await r.json());
    } catch {
      setErro({ mensagem: "Falha de rede ao carregar usuários.", requestId: null });
      setUsuarios([]);
    } finally {
      setCarregando(false);
    }
  }, [token]);

  useEffect(() => {
    void carregar();
  }, [carregar]);

  // Último acesso pela trilha: os eventos vêm em ordem CRESCENTE, então o último `login` de cada
  // ator sobrescreve os anteriores e o mapa acaba com o mais recente.
  const carregarAcessos = useCallback(async () => {
    setTrilha("carregando");
    try {
      const { registros, completo } = await api.consultarAuditoriaTudo({ acao: "login" });
      const mapa: Record<string, string> = {};
      for (const r of registros) if (r.ator_id) mapa[r.ator_id] = r.ocorrido_em;
      setUltimoLogin(mapa);
      setTrilha(completo ? "ok" : "parcial");
    } catch {
      setUltimoLogin({});
      setTrilha("falha");
    }
  }, [token]);

  useEffect(() => {
    void carregarAcessos();
  }, [carregarAcessos]);

  // Quem sou eu: a tela precisa marcar a própria linha e impedir que o admin se rebaixe ou
  // suspenda o próprio acesso (auto-bloqueio irreversível pela UI — dependeria de outro admin).
  useEffect(() => {
    let vivo = true;
    api
      .meAtual()
      .then((u) => vivo && setEu(u))
      .catch(() => vivo && setEu(null));
    return () => {
      vivo = false;
    };
  }, [token]);

  useEffect(() => {
    if (criando) primeiroCampo.current?.focus();
  }, [criando]);

  // `eu` chega depois da 1ª pintura: se o formulário já estava aberto com outro perfil escolhido,
  // volta para operador em vez de enviar algo que a API recusaria com 403.
  useEffect(() => {
    if (!souAdmin && novoPerfil !== "operador") setNovoPerfil("operador");
  }, [souAdmin, novoPerfil]);

  const marcarOcupado = (id: string, v: boolean) => setOcupado((o) => ({ ...o, [id]: v }));
  const avisarOk = (texto: string) => setAviso({ texto, tom: "ok" });
  const avisarAtencao = (texto: string) => setAviso({ texto, tom: "aten" });

  // Texto único para "o canal não entregou": as três ações que disparam e-mail (criar, reenviar,
  // corrigir) precisam dizer a MESMA coisa — o cadastro está de pé, o que faltou foi a entrega.
  const naoEntregue = (email: string) =>
    `O cadastro está salvo, mas o link de acesso NÃO saiu para ${email}. Confira se o endereço ` +
    `está certo (e corrija se não estiver) e use "Reenviar acesso" — o link só vale depois de sair.`;

  const criar = async () => {
    setErroForm(null);
    setAviso(null);
    const email = novoEmail.trim().toLowerCase();
    const nome = novoNome.trim();
    if (!email || !nome) {
      setErroForm("Informe o e-mail e o nome.");
      return;
    }
    if (!EMAIL_RE.test(email)) {
      setErroForm("E-mail com formato inválido (ex.: pessoa@example.com).");
      return;
    }
    setSalvando(true);
    try {
      const r = await reqUsuarios("POST", "/usuarios", { email, nome, perfil: novoPerfil });
      if (!r.ok) {
        setErroForm((await falhaDaResposta(r, "Falha ao criar usuário.")).mensagem);
        return;
      }
      // `email_entregue` é o que a API sabe do canal. Ausente (API antiga) ≠ falso: nesse caso a
      // tela não afirma nem desmente a entrega, só não promete o que não pode confirmar.
      const corpo = (await r.json().catch(() => null)) as {
        email_entregue?: boolean | null;
        conta_servico?: boolean;
      } | null;
      if (corpo?.conta_servico) {
        // Conta de serviço: sem senha e sem link — o acesso é o token que se emite depois.
        avisarOk(
          `${nome} foi criado como conta de serviço. Ela não tem senha: emita o token em ` +
            `"Tokens de API" no menu da linha, e conceda "executar" nos pipelines em Permissões.`,
        );
      } else if (corpo?.email_entregue === false) {
        avisarAtencao(
          `${nome} foi criado como ${rotuloPerfil(novoPerfil)}. ${naoEntregue(email)}`,
        );
      } else {
        avisarOk(
          `${nome} foi criado como ${rotuloPerfil(novoPerfil)}. Um link de primeiro acesso saiu ` +
            `para ${email} pelo canal de notificação configurado — a senha é definida por quem ` +
            `recebe o link e nunca passa por esta tela.`,
        );
      }
      setCriando(false);
      setNovoEmail("");
      setNovoNome("");
      setNovoPerfil("operador");
      await carregar();
    } catch {
      setErroForm("Falha de rede ao criar usuário.");
    } finally {
      setSalvando(false);
    }
  };

  const aplicarPerfil = async (u: Usuario, perfil: Perfil) => {
    setAviso(null);
    setErro(null);
    marcarOcupado(u.id, true);
    const anterior = u.perfil;
    setUsuarios((lista) => lista.map((x) => (x.id === u.id ? { ...x, perfil } : x)));
    try {
      const r = await reqUsuarios("PATCH", `/usuarios/${u.id}/perfil`, { perfil });
      if (!r.ok) {
        setErro(await falhaDaResposta(r, "Falha ao alterar o perfil."));
        setUsuarios((lista) =>
          lista.map((x) => (x.id === u.id ? { ...x, perfil: anterior } : x)),
        );
        return;
      }
      avisarOk(`${u.nome} agora é ${rotuloPerfil(perfil)}. A troca ficou registrada na trilha.`);
    } catch {
      setErro({ mensagem: "Falha de rede ao alterar o perfil.", requestId: null });
      setUsuarios((lista) => lista.map((x) => (x.id === u.id ? { ...x, perfil: anterior } : x)));
    } finally {
      marcarOcupado(u.id, false);
    }
  };

  const aplicarAcesso = async (u: Usuario) => {
    setAviso(null);
    setErro(null);
    marcarOcupado(u.id, true);
    const alvo = !u.ativo;
    setUsuarios((lista) => lista.map((x) => (x.id === u.id ? { ...x, ativo: alvo } : x)));
    try {
      const r = await reqUsuarios("PATCH", `/usuarios/${u.id}/ativo`, { ativo: alvo });
      if (!r.ok) {
        setErro(await falhaDaResposta(r, "Falha ao alterar o acesso."));
        setUsuarios((lista) => lista.map((x) => (x.id === u.id ? { ...x, ativo: !alvo } : x)));
        return;
      }
      avisarOk(
        alvo
          ? `Acesso de ${u.nome} liberado. Ele pode entrar de novo com a senha que já tem.`
          : `Acesso de ${u.nome} suspenso. O login passa a ser recusado; o histórico e a trilha ` +
              `permanecem intactos.`,
      );
    } catch {
      setErro({ mensagem: "Falha de rede ao alterar o acesso.", requestId: null });
      setUsuarios((lista) => lista.map((x) => (x.id === u.id ? { ...x, ativo: !alvo } : x)));
    } finally {
      marcarOcupado(u.id, false);
    }
  };

  const reenviarAcesso = async (u: Usuario) => {
    setAviso(null);
    setErro(null);
    marcarOcupado(u.id, true);
    try {
      const r = await reqUsuarios("POST", `/usuarios/${u.id}/reenviar-acesso`);
      if (!r.ok) {
        setErro(await falhaDaResposta(r, "Falha ao reenviar o link de acesso."));
        return;
      }
      const c = (await r.json().catch(() => null)) as { email_entregue?: boolean } | null;
      if (c?.email_entregue === false) avisarAtencao(naoEntregue(u.email));
      else
        avisarOk(
          `Um link novo de acesso saiu para ${u.email}. Ele vale por tempo limitado e é de uso ` +
            `único; o link anterior, se existir, continua válido até expirar.`,
        );
    } catch {
      setErro({ mensagem: "Falha de rede ao reenviar o link de acesso.", requestId: null });
    } finally {
      marcarOcupado(u.id, false);
    }
  };

  const corrigirEmail = async (u: Usuario, novo: string) => {
    setAviso(null);
    setErro(null);
    const email = novo.trim().toLowerCase();
    if (!EMAIL_RE.test(email)) {
      setErro({
        mensagem: "E-mail com formato inválido (ex.: pessoa@example.com).",
        requestId: null,
      });
      return;
    }
    if (email === u.email) return;
    marcarOcupado(u.id, true);
    try {
      const r = await reqUsuarios("PATCH", `/usuarios/${u.id}/email`, { email });
      if (!r.ok) {
        setErro(await falhaDaResposta(r, "Falha ao corrigir o e-mail."));
        return;
      }
      const c = (await r.json().catch(() => null)) as
        | { email_entregue?: boolean; reenviado?: boolean }
        | null;
      // A lista só é reescrita depois do 200: otimismo aqui trocaria o identificador de login na
      // tela sem que o backend tivesse aceitado.
      setUsuarios((lista) => lista.map((x) => (x.id === u.id ? { ...x, email } : x)));
      if (c?.reenviado === false) {
        avisarAtencao(
          `E-mail de ${u.nome} corrigido para ${email}. O link de acesso NÃO foi reenviado ` +
            `porque o acesso está suspenso — libere o acesso e use "Reenviar acesso".`,
        );
      } else if (c?.email_entregue === false) {
        avisarAtencao(naoEntregue(email));
      } else {
        avisarOk(
          `E-mail de ${u.nome} corrigido para ${email} e um link novo de acesso saiu para lá. ` +
            `O endereço antigo (${u.email}) não entra mais — a troca ficou registrada na trilha.`,
        );
      }
    } catch {
      setErro({ mensagem: "Falha de rede ao corrigir o e-mail.", requestId: null });
    } finally {
      marcarOcupado(u.id, false);
    }
  };


  const total = usuarios.length;
  const liberados = useMemo(() => usuarios.filter((u) => u.ativo).length, [usuarios]);
  const temFiltro = !!(busca.trim() || fPerfil || fSituacao);

  const visiveis = useMemo(() => {
    const q = busca.trim().toLowerCase();
    return usuarios.filter((u) => {
      if (q && !`${u.nome} ${u.email}`.toLowerCase().includes(q)) return false;
      if (fPerfil && u.perfil !== fPerfil) return false;
      if (fSituacao === "liberado" && !u.ativo) return false;
      if (fSituacao === "suspenso" && u.ativo) return false;
      return true;
    });
  }, [usuarios, busca, fPerfil, fSituacao]);

  const limparFiltros = () => {
    setBusca("");
    setFPerfil("");
    setFSituacao("");
  };

  // Cadastro primeiro (se um dia for preenchido), trilha como apuração de hoje.
  const acessoDe = (u: Usuario): string | null => u.ultimo_acesso ?? ultimoLogin[u.id] ?? null;

  const itens: ItemCadastro[] = visiveis.map((u) => {
    const souEu = !!eu && eu.id === u.id;
    const iso = acessoDe(u);
    return {
      id: u.id,
      nome: souEu ? `${u.nome} (você)` : u.nome,
      meta: u.email,
      status: u.ativo ? "acesso liberado" : "acesso suspenso",
      scls: u.ativo ? "ok" : "falha",
      campos: [
        // Perfil é LEITURA na linha; trocar é ação deliberada, na coluna de ações.
        { rotulo: "Perfil", valor: <span>{rotuloPerfil(u.perfil)}</span> },
        {
          rotulo: "Último acesso (GMT-3)",
          valor: iso ? (
            <span className="num" title={fmtMomento(iso)}>
              {fmtMomentoCurto(iso)}
            </span>
          ) : trilha === "ok" ? (
            // Só afirma "nunca entrou" quando a trilha foi lida INTEIRA e não tem login dele.
            <span className="adm-nunca">aguardando 1º acesso</span>
          ) : trilha === "carregando" ? (
            <span className="adm-nunca">apurando…</span>
          ) : souEu ? (
            <span className="adm-nunca">sessão ativa agora</span>
          ) : (
            <span className="adm-nunca" title="Não foi possível apurar o último acesso na trilha.">
              não apurado
            </span>
          ),
        },
      ],
    };
  });

  return (
    <>
      {pendente?.tipo === "perfil" && (
        <ModalPerfil
          usuario={pendente.usuario}
          ocupado={!!ocupado[pendente.usuario.id]}
          onConfirmar={(novo) => {
            const u = pendente.usuario;
            setPendente(null);
            void aplicarPerfil(u, novo);
          }}
          onCancelar={() => setPendente(null)}
        />
      )}
      {pendente?.tipo === "tokens" && (
        <ModalTokens usuario={pendente.usuario} onFechar={() => setPendente(null)} />
      )}
      {pendente?.tipo === "acesso" && (
        <ConfirmarAcao
          titulo={pendente.usuario.ativo ? "Suspender o acesso" : "Liberar o acesso"}
          corpo={
            pendente.usuario.ativo ? (
              <>
                <b>{pendente.usuario.nome}</b> deixa de conseguir entrar: o login passa a ser
                recusado e as execuções agendadas por ele não têm mais dono ativo. Nada é apagado —
                histórico, relatórios e trilha continuam.
              </>
            ) : (
              <>
                <b>{pendente.usuario.nome}</b>{" "}
                {pendente.usuario.perfil === "servico"
                  ? "volta a entrar com os tokens que ainda valem"
                  : "volta a conseguir entrar com a senha que já tem"}
                , no perfil <b>{rotuloPerfil(pendente.usuario.perfil)}</b>.
              </>
            )
          }
          rotuloConfirmar={pendente.usuario.ativo ? "Suspender acesso" : "Liberar acesso"}
          perigo={pendente.usuario.ativo}
          onConfirmar={() => {
            const p = pendente;
            setPendente(null);
            void aplicarAcesso(p.usuario);
          }}
          onCancelar={() => setPendente(null)}
        />
      )}
      {pendente?.tipo === "reenvio" && (
        <ConfirmarAcao
          titulo="Reenviar o link de acesso"
          corpo={
            <>
              Gera um link novo de definição de senha para <b>{pendente.usuario.nome}</b> e envia
              para <b>{pendente.usuario.email}</b>. Use quando o e-mail não chegou. Se o endereço
              está errado, corrija o e-mail em vez de reenviar — o link só chega ao endereço do
              cadastro. A senha continua sendo definida por quem recebe o link.
            </>
          }
          rotuloConfirmar="Reenviar link"
          onConfirmar={() => {
            const p = pendente;
            setPendente(null);
            void reenviarAcesso(p.usuario);
          }}
          onCancelar={() => setPendente(null)}
        />
      )}
      {pendente?.tipo === "email" && (
        <ConfirmarAcao
          titulo="Corrigir o e-mail"
          corpo={
            <>
              O e-mail é o endereço de entrega do link E o identificador de login de{" "}
              <b>{pendente.usuario.nome}</b>: depois da troca, o endereço antigo (
              <b>{pendente.usuario.email}</b>) não entra mais. Um link novo de acesso sai para o
              endereço novo, e a troca fica registrada na trilha de auditoria.
            </>
          }
          rotuloCampo="E-mail"
          valorInicial={pendente.usuario.email}
          rotuloConfirmar="Corrigir e reenviar"
          onConfirmar={(novo) => {
            const p = pendente;
            setPendente(null);
            void corrigirEmail(p.usuario, novo);
          }}
          onCancelar={() => setPendente(null)}
        />
      )}

      <div className="page-head">
        <h1 className="display">Usuários</h1>
        <p className="sub">
          Quem entra na plataforma e com qual perfil. A plataforma não exclui usuário (a trilha é
          permanente): para tirar alguém do ar, suspenda o acesso.
          {!souAdmin && (
            <>
              {" "}
              <b>Esta lista mostra só os operadores</b> — é o que o seu perfil administra. Os
              demais perfis, e a troca de perfil de qualquer pessoa, são com um administrador.
            </>
          )}
        </p>
      </div>

      <div className="pad">
        <div className="lc-bar adm-barra">
          <span className="adm-contagem" data-testid="usuarios-contagem">
            {total === 0
              ? `nenhum ${substantivo}`
              : `${total} ${total === 1 ? substantivo : substantivoPlural} · ${liberados} com acesso liberado`}
            {temFiltro && total > 0 && ` · ${visiveis.length} na filtragem`}
          </span>
          <label className="adm-filtro">
            <span className="eyebrow">Buscar</span>
            <input
              className="nm"
              type="search"
              value={busca}
              placeholder="nome ou e-mail"
              data-testid="usuarios-busca"
              onChange={(e) => setBusca(e.target.value)}
            />
          </label>
          {/* Sem o filtro quando a lista tem um perfil só (especialista): filtrar "Operador"
              numa lista de operadores é um controle que nunca muda nada. */}
          {souAdmin && (
            <label className="adm-filtro">
              <span className="eyebrow">Perfil</span>
              <select
                className="nm"
                value={fPerfil}
                data-testid="usuarios-filtro-perfil"
                onChange={(e) => setFPerfil(e.target.value as Perfil | "")}
              >
                <option value="">Todos</option>
                {PERFIS.map((p) => (
                  <option key={p.valor} value={p.valor}>
                    {p.rotulo}
                  </option>
                ))}
              </select>
            </label>
          )}
          <label className="adm-filtro">
            <span className="eyebrow">Acesso</span>
            <select
              className="nm"
              value={fSituacao}
              data-testid="usuarios-filtro-acesso"
              onChange={(e) => setFSituacao(e.target.value as "" | "liberado" | "suspenso")}
            >
              <option value="">Todos</option>
              <option value="liberado">Liberado</option>
              <option value="suspenso">Suspenso</option>
            </select>
          </label>
          <button
            className="btn-stamp adm-barra-acao"
            data-testid="usuarios-novo"
            aria-expanded={criando}
            onClick={() => {
              setCriando((v) => !v);
              setErroForm(null);
              setAviso(null);
            }}
          >
            + Novo {substantivo}
          </button>
        </div>

        {/* Procedência da coluna: o número não pode se passar por dado do cadastro. */}
        <p className="adm-dica" style={{ margin: "0 0 12px" }} data-testid="usuarios-fonte-acesso">
          <b>Último acesso</b> é apurado na trilha de auditoria (evento de login), em
          America/Sao_Paulo.
          {trilha === "parcial" &&
            " A trilha tem mais eventos do que esta tela lê de uma vez: a data mostrada pode ser mais antiga que a real."}
          {trilha === "falha" && " Não foi possível ler a trilha agora — a coluna fica sem apuração."}
        </p>

        {aviso && (
          <div
            className={`card adm-banner adm-banner--${aviso.tom}`}
            role={aviso.tom === "aten" ? "alert" : "status"}
            data-testid="usuarios-aviso"
            data-tom={aviso.tom}
          >
            <span className="adm-banner-d" />
            <span>{aviso.texto}</span>
            <button
              type="button"
              className="adm-banner-x"
              aria-label="Fechar aviso"
              onClick={() => setAviso(null)}
            >
              ×
            </button>
          </div>
        )}

        {erro && (
          <div className="card adm-banner adm-banner--erro" role="alert" data-testid="usuarios-erro">
            <span className="adm-banner-d" />
            <span>
              {erro.mensagem}
              {erro.requestId && (
                <span className="adm-banner-rid"> · código de suporte {erro.requestId}</span>
              )}
            </span>
            <button type="button" className="pag-btn adm-banner-acao" onClick={() => void carregar()}>
              Tentar de novo
            </button>
          </div>
        )}

        {criando && (
          <form
            className="card adm-form"
            data-testid="usuarios-form"
            onSubmit={(e) => {
              e.preventDefault();
              void criar();
            }}
            onKeyDown={(e) => {
              if (e.key === "Escape") setCriando(false);
            }}
          >
            <div className="card-h">
              <h3>Novo {substantivo}</h3>
              <span className="tag">
                a senha não é definida aqui — vai um link de primeiro acesso
              </span>
            </div>
            <div className="adm-form-corpo">
              <div className="adm-form-grid">
                <label className="adm-campo">
                  <span className="fld">E-mail</span>
                  <input
                    ref={primeiroCampo}
                    className="nm"
                    type="email"
                    autoComplete="off"
                    value={novoEmail}
                    placeholder="pessoa@example.com"
                    data-testid="usuarios-novo-email"
                    onChange={(e) => setNovoEmail(e.target.value)}
                  />
                </label>
                <label className="adm-campo">
                  <span className="fld">Nome</span>
                  <input
                    className="nm"
                    type="text"
                    autoComplete="off"
                    value={novoNome}
                    placeholder="Nome completo"
                    data-testid="usuarios-novo-nome"
                    onChange={(e) => setNovoNome(e.target.value)}
                  />
                </label>
                {/* Select de uma opção só é controle morto: quem não é admin vê o perfil
                    DITO, não escolhido — e por que é o único. */}
                <label className="adm-campo">
                  <span className="fld">Perfil</span>
                  {perfisQuePossoCriar.length > 1 ? (
                    <select
                      className="nm"
                      value={novoPerfil}
                      data-testid="usuarios-novo-perfil"
                      onChange={(e) => setNovoPerfil(e.target.value as Perfil)}
                    >
                      {perfisQuePossoCriar.map((p) => (
                        <option key={p.valor} value={p.valor}>
                          {p.rotulo}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <span className="nm adm-campo-fixo" data-testid="usuarios-novo-perfil-fixo">
                      {rotuloPerfil(novoPerfil)}
                    </span>
                  )}
                </label>
              </div>
              <p className="adm-form-dica">
                {PERFIS.find((p) => p.valor === novoPerfil)?.oque}{" "}
                {souAdmin
                  ? "O perfil pode ser trocado depois."
                  : "O seu perfil cria operador — trocar o perfil de alguém é com um administrador."}
              </p>
              {erroForm && (
                <p className="adm-form-erro" role="alert" data-testid="usuarios-form-erro">
                  {erroForm}
                </p>
              )}
              <div className="adm-form-acoes">
                <button className="btn-stamp" type="submit" disabled={salvando}>
                  <Icon name="stamp" cls="ic" />
                  {salvando ? "Criando…" : `Criar ${substantivo}`}
                </button>
                <button
                  className="btn-sec"
                  type="button"
                  disabled={salvando}
                  onClick={() => {
                    setCriando(false);
                    setErroForm(null);
                  }}
                >
                  Cancelar
                </button>
              </div>
            </div>
          </form>
        )}

        {carregando && total === 0 ? (
          <div className="card lc-empty" data-testid="usuarios-carregando">
            <div className="big">Carregando usuários…</div>
          </div>
        ) : total === 0 && !erro ? (
          <div className="card lc-empty">
            <div className="big">Nenhum {substantivo} cadastrado</div>
            <div>
              Clique <b>+ Novo {substantivo}</b> para começar.
            </div>
          </div>
        ) : visiveis.length === 0 && total > 0 ? (
          <div className="card lc-empty">
            <div className="big">Nenhum {substantivo} com esses filtros</div>
            <div>
              {total} cadastrado{total === 1 ? "" : "s"} no total.{" "}
              <button type="button" className="lk" onClick={limparFiltros}>
                Limpar filtros
              </button>
            </div>
          </div>
        ) : visiveis.length > 0 ? (
          <GridLista
            rotulo="Usuários"
            itens={itens}
            acoes={(it) => {
              const u = usuarios.find((x) => x.id === it.id);
              if (!u) return null;
              // A própria linha não ganha um botão desabilitado (controle morto, sem foco, com a
              // explicação escondida num `title`): ganha o texto que diz por quê.
              //
              // "só outro administrador altera" era honesto mas lia como AUSÊNCIA de ação, no lugar
              // onde as outras linhas têm botão. Agora o texto nomeia o impedido (você) e o cadeado
              // diz que é bloqueio; o motivo completo está no `title` e, visível, no rodapé
              // "O que cada perfil pode fazer".
              if (eu && eu.id === u.id) {
                return (
                  <span
                    className="adm-nota-self"
                    data-testid={`usuarios-self-${u.id}`}
                    title={MOTIVO_SELF}
                  >
                    <IconeCadeado />
                    você não altera o seu próprio acesso
                  </span>
                );
              }
              return (
                <>
                  {/* Trocar perfil é SÓ do admin (a rota é a única que não afrouxou): para o
                      especialista o botão não fica desabilitado, some — controle morto na linha
                      de quem ele de fato administra convida a clicar e explica depois. */}
                  {souAdmin && (
                    <button
                      type="button"
                      data-testid={`usuarios-perfil-${u.id}`}
                      disabled={!!ocupado[u.id]}
                      onClick={() => setPendente({ tipo: "perfil", usuario: u })}
                    >
                      Trocar perfil
                    </button>
                  )}
                  {/* Reenviar link e corrigir e-mail são o conserto de "o e-mail não chegou": raras
                      ao lado das duas de segurança, e com quatro botões de peso igual a irreversível
                      (suspender) disputava atenção com elas. Vão para o menu, alcançáveis por
                      teclado. */}
                  <MenuMaisAcoes nome={u.nome}>
                    {/* Conta de serviço não tem senha nem link: o que ela tem é token. */}
                    {u.perfil === "servico" && (
                      <button
                        type="button"
                        data-testid={`usuarios-tokens-${u.id}`}
                        aria-label={`Tokens de API de ${u.nome}`}
                        disabled={!!ocupado[u.id]}
                        onClick={(e) => {
                          fecharMenuDoBotao(e);
                          setPendente({ tipo: "tokens", usuario: u });
                        }}
                      >
                        Tokens de API…
                      </button>
                    )}
                    {u.perfil !== "servico" && (
                    <button
                      type="button"
                      data-testid={`usuarios-reenviar-${u.id}`}
                      aria-label={`Reenviar link de acesso de ${u.nome}`}
                      disabled={!!ocupado[u.id] || !u.ativo}
                      title={
                        u.ativo
                          ? undefined
                          : "Acesso suspenso: libere o acesso antes de reenviar o link."
                      }
                      onClick={(e) => {
                        fecharMenuDoBotao(e);
                        setPendente({ tipo: "reenvio", usuario: u });
                      }}
                    >
                      Reenviar acesso
                    </button>
                    )}
                    <button
                      type="button"
                      data-testid={`usuarios-email-${u.id}`}
                      aria-label={`Corrigir o e-mail de ${u.nome}`}
                      disabled={!!ocupado[u.id]}
                      onClick={(e) => {
                        fecharMenuDoBotao(e);
                        setPendente({ tipo: "email", usuario: u });
                      }}
                    >
                      Corrigir e-mail
                    </button>
                    {/* Suspender/liberar acesso é mudança de segurança: entra no menu junto das
                        outras, deixando fora só a ação principal da linha. */}
                    <button
                      type="button"
                      className={u.ativo ? "del" : undefined}
                      data-testid={`usuarios-acesso-${u.id}`}
                      disabled={!!ocupado[u.id]}
                      onClick={() => setPendente({ tipo: "acesso", usuario: u })}
                    >
                      {u.ativo ? "Suspender acesso" : "Liberar acesso"}
                    </button>
                  </MenuMaisAcoes>
                </>
              );
            }}
          />
        ) : null}

        <details className="card adm-perfis">
          <summary>O que cada perfil pode fazer</summary>
          <dl className="adm-perfis-lista">
            {PERFIS.map((p) => (
              <div key={p.valor}>
                <dt>{p.rotulo}</dt>
                <dd>{p.oque}</dd>
              </div>
            ))}
          </dl>
          <p className="adm-perfis-pe">
            Perfil é a capacidade geral. O acesso a um recurso específico (pipeline, cubo, view,
            página, conexão) é concedido em <b>Permissões</b>, com níveis cumulativos.
          </p>
          <p className="adm-perfis-pe">{MOTIVO_SELF}</p>
          {!souAdmin && (
            <p className="adm-perfis-pe" data-testid="usuarios-alcance">
              O seu perfil (Especialista) cria e administra <b>operador</b>: criar, reenviar o link
              de acesso, corrigir o e-mail e suspender ou liberar o acesso. Trocar o perfil de
              alguém — inclusive de um operador — é exclusivo do administrador.
            </p>
          )}
        </details>
      </div>
    </>
  );
}
