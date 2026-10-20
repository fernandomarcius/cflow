import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSelector } from "react-redux";

import * as api from "../../api/client";
import { ConfirmarAcao, GridLista, Icon, type ItemCadastro } from "../ui";
import { fmtHora, fmtMomento, fmtMomentoCurto } from "../execucao/formato";
import type { RootState } from "../../store/store";

// ─────────────────────────────────────────────────────────────────────────
// Permissões (admin) — acesso a UM recurso para UM sujeito (usuário ou grupo).
//
// Quatro coisas que a tela precisa deixar explícitas, porque errar aqui muda quem vê o quê:
//
// 1. NÍVEL É CUMULATIVO (administrar ⊇ editar ⊇ executar ⊇ ver). A escada mostra o que o nível
//    escolhido já inclui — antes era um `<select>` de quatro palavras soltas e o admin adivinhava.
// 2. CONCEDER É POR NÍVEL, NÃO "ELEVAÇÃO". O backend insere UMA linha (sujeito, recurso, nível);
//    `criada:false` significa "essa linha exata já existia", não "nível mantido ou elevado" (era o
//    que a tela dizia — e era falso).
// 3. REVOGAR REMOVE SÓ AQUELE NÍVEL. Se o sujeito também recebeu outro nível, ou pertence a um
//    grupo com acesso, ele CONTINUA com acesso. Dizer só "Permissão revogada." fazia o admin
//    acreditar que tinha cortado o acesso.
// 4. O QUE JÁ ESTÁ CONCEDIDO APARECE. A API não expõe listagem de permissões (só POST/DELETE), e
//    enquanto ela não expõe, a tela RECONSTRÓI o estado a partir da TRILHA DE AUDITORIA: `criar`
//    de `recurso_tipo=permissao` concede, `excluir` revoga, reprocessados em ordem cronológica.
//    Sem essa lista, "Revogar" era um chute — o admin digitava a tupla de memória e a tela
//    respondia "removido" mesmo quando não havia nada para remover.
//
// A lista é DERIVADA, e diz isso: cobre o que passou por esta tela. NÃO cobre o acesso automático
// de quem criou o recurso (concedido junto da criação, sem registro de permissão na trilha) nem o
// bypass do perfil admin. Nada aqui pode se passar por "a verdade completa do acesso".
// ─────────────────────────────────────────────────────────────────────────

type SujeitoTipo = "usuario" | "grupo";
type RecursoTipo = "pipeline" | "cubo" | "view" | "pagina" | "conexao";
type Nivel = "ver" | "executar" | "editar" | "administrar";

interface Opcao {
  id: string;
  nome: string;
}

interface Grupo {
  id: string;
  nome: string;
  descricao?: string | null;
  membros?: { id: string; nome: string; email?: string }[];
}

interface CorpoPermissao {
  sujeito_tipo: SujeitoTipo;
  sujeito_id: string;
  recurso_tipo: RecursoTipo;
  recurso_id: string;
  nivel: Nivel;
}

const RECURSOS: { tipo: RecursoTipo; rotulo: string }[] = [
  { tipo: "pipeline", rotulo: "Pipeline" },
  { tipo: "cubo", rotulo: "Cubo" },
  { tipo: "view", rotulo: "View" },
  { tipo: "pagina", rotulo: "Página" },
  { tipo: "conexao", rotulo: "Conexão" },
];

// Ordem cumulativa (a mesma de cflow_core.authz.perfis): cada nível inclui todos os anteriores.
// `oque` é a AÇÃO do nível, e precisa encadear em lista ("abrir e consultar, rodar e gerar
// relatório, alterar e publicar") — é dela que sai a frase que explica o cumulativo.
const NIVEIS: { nivel: Nivel; rotulo: string; oque: string }[] = [
  { nivel: "ver", rotulo: "Ver", oque: "abrir e consultar" },
  { nivel: "executar", rotulo: "Executar", oque: "rodar e gerar relatório" },
  { nivel: "editar", rotulo: "Editar", oque: "alterar e publicar" },
  { nivel: "administrar", rotulo: "Administrar", oque: "conceder acesso a outros" },
];

function incluidos(nivel: Nivel): Nivel[] {
  const i = NIVEIS.findIndex((n) => n.nivel === nivel);
  return NIVEIS.slice(0, Math.max(0, i)).map((n) => n.nivel);
}

function rotuloNivel(n: Nivel): string {
  return NIVEIS.find((x) => x.nivel === n)?.rotulo ?? n;
}

function rotuloRecursoTipo(t: RecursoTipo | string): string {
  return RECURSOS.find((r) => r.tipo === t)?.rotulo ?? String(t);
}

// Ordem cumulativa também ordena a lista: administrar primeiro (é o acesso que mais pesa).
function pesoNivel(n: Nivel): number {
  const i = NIVEIS.findIndex((x) => x.nivel === n);
  return i < 0 ? 0 : NIVEIS.length - i;
}

// Lista pt-BR do que o nível cobre ("editar" ⇒ "ver, executar e editar").
function listaCobertura(nivel: Nivel): string {
  const cobre = [...incluidos(nivel), nivel].map((n) => rotuloNivel(n).toLowerCase());
  if (cobre.length === 1) return cobre[0];
  return `${cobre.slice(0, -1).join(", ")} e ${cobre[cobre.length - 1]}`;
}

// As AÇÕES que o nível libera, em ordem cumulativa. A nota da escada dizia "Quem recebe ver pode
// ver" — verdade que não informa nada. Com as ações, o nível mínimo também explica o que é:
// "quem recebe ver pode abrir e consultar".
function listaAcoes(nivel: Nivel): string {
  return [...incluidos(nivel), nivel]
    .map((n) => NIVEIS.find((x) => x.nivel === n)?.oque ?? n)
    .join(", ");
}

interface Retorno<T> {
  ok: boolean;
  status: number;
  dados: T | null;
  erro: string | null;
  requestId: string | null;
}

// Envelope de erro pt-BR + `request_id` guardado (é o que o suporte pede). Usa o `apiFetch` do
// cliente compartilhado: endpoints de identidade não têm função tipada própria.
async function chamar<T>(metodo: string, caminho: string, corpo?: unknown): Promise<Retorno<T>> {
  let r: Response;
  try {
    r = await api.apiFetch(metodo, caminho, corpo);
  } catch {
    return {
      ok: false,
      status: 0,
      dados: null,
      erro: "Falha de conexão com o servidor.",
      requestId: null,
    };
  }
  if (r.status === 204) {
    return { ok: true, status: 204, dados: null, erro: null, requestId: null };
  }
  const j = await r.json().catch(() => null);
  const e = (j as any)?.erro;
  const requestId = e?.request_id ?? r.headers.get("X-Request-Id") ?? null;
  if (!r.ok) {
    if (r.status === 403) {
      return {
        ok: false,
        status: 403,
        dados: null,
        erro: "Esta tela é restrita a administradores.",
        requestId,
      };
    }
    const det = (e?.detalhes ?? [])
      .map((d: any) => d?.mensagem)
      .filter(Boolean)
      .join(" · ");
    const msg = e?.mensagem
      ? det
        ? `${e.mensagem} — ${det}`
        : e.mensagem
      : "Falha na operação.";
    return { ok: false, status: r.status, dados: null, erro: msg, requestId };
  }
  return { ok: true, status: r.status, dados: j as T, erro: null, requestId };
}

// Texto único para "o alvo desta permissão não está mais na lista" (as duas rotas de seleção — a
// mesma aba e a que troca o tipo e recarrega — precisam dizer a mesma coisa).
function avisoAusente(oQue: string): string {
  return (
    `Esta permissão aponta para ${oQue} que não está na lista de hoje (provavelmente excluído). ` +
    `Não levei esse alvo para o formulário; para tirar o acesso, use "Revogar" na própria linha.`
  );
}

function normalizarLista(itens: any[]): Opcao[] {
  return (itens ?? []).map((it) => ({
    id: String(it.id),
    nome: it.nome ?? it.email ?? it.titulo ?? String(it.id),
  }));
}

interface Registro {
  hora: string;
  tipo: "concedido" | "revogado" | "sem_efeito";
  texto: string;
}

// Uma permissão viva, reconstruída da trilha. `quando`/`por` vêm do registro que a concedeu.
interface Concedido {
  chave: string;
  sujeito_tipo: SujeitoTipo;
  sujeito_id: string;
  recurso_tipo: RecursoTipo;
  recurso_id: string;
  nivel: Nivel;
  quando: string;
  por: string | null;
}

// Identidade da permissão: a MESMA tupla que o backend usa como chave (sujeito, recurso, nível).
// Só assim conceder→revogar se cancelam no replay.
function chaveTupla(p: Record<string, unknown> | null | undefined): string | null {
  if (!p) return null;
  const partes = [p.sujeito_tipo, p.sujeito_id, p.recurso_tipo, p.recurso_id, p.nivel];
  if (partes.some((x) => x == null || x === "")) return null;
  return partes.map(String).join("|");
}

// Reprocessa a trilha em ordem cronológica: `criar` acende a tupla, `excluir` apaga. A rota já
// devolve ASCENDENTE por `ocorrido_em` — é disso que o replay depende.
function reconstruir(registros: api.RegistroAuditoria[]): Concedido[] {
  const vivas = new Map<string, Concedido>();
  for (const reg of registros) {
    if (reg.acao === "criar") {
      const p = reg.depois;
      const chave = chaveTupla(p);
      if (!chave || !p) continue;
      vivas.set(chave, {
        chave,
        sujeito_tipo: String(p.sujeito_tipo) as SujeitoTipo,
        sujeito_id: String(p.sujeito_id),
        recurso_tipo: String(p.recurso_tipo) as RecursoTipo,
        recurso_id: String(p.recurso_id),
        nivel: String(p.nivel) as Nivel,
        quando: reg.ocorrido_em,
        por: reg.ator_id,
      });
    } else if (reg.acao === "excluir") {
      const chave = chaveTupla(reg.antes);
      if (chave) vivas.delete(chave);
    }
  }
  return [...vivas.values()];
}

type PedidoPerm =
  | { tipo: "revogar" }
  | { tipo: "revogar_linha"; g: Concedido; sujeito: string; recurso: string }
  | { tipo: "excluir_grupo"; grupo: Grupo }
  | { tipo: "remover_membro"; grupo: Grupo; membroId: string; membroNome: string };

export default function Permissoes(): JSX.Element {
  // Só para reagir à (re)autenticação — o Bearer vem do módulo `api`.
  const token = useSelector((s: RootState) => s.studio.token);

  // --- Conceder/revogar permissão ---
  const [sujeitoTipo, setSujeitoTipo] = useState<SujeitoTipo>("usuario");
  const [sujeitoId, setSujeitoId] = useState("");
  const [recursoTipo, setRecursoTipo] = useState<RecursoTipo>("pipeline");
  const [recursoId, setRecursoId] = useState("");
  const [nivel, setNivel] = useState<Nivel>("ver");

  const [sujeitos, setSujeitos] = useState<Opcao[]>([]);
  const [recursos, setRecursos] = useState<Opcao[]>([]);
  const [carregandoSujeitos, setCarregandoSujeitos] = useState(false);
  const [carregandoRecursos, setCarregandoRecursos] = useState(false);
  const [erroRecursos, setErroRecursos] = useState<string | null>(null);

  const [msgPerm, setMsgPerm] = useState<{
    tipo: "ok" | "erro" | "aten";
    texto: string;
    requestId?: string | null;
  } | null>(null);
  const [salvando, setSalvando] = useState(false);
  const [registros, setRegistros] = useState<Registro[]>([]);
  const [pedido, setPedido] = useState<PedidoPerm | null>(null);

  // --- O que já está concedido (reconstruído da trilha) ---
  const [concedidos, setConcedidos] = useState<Concedido[] | null>(null);
  const [carregandoConcedidos, setCarregandoConcedidos] = useState(true);
  const [erroConcedidos, setErroConcedidos] = useState<string | null>(null);
  const [trilhaParcial, setTrilhaParcial] = useState(false);
  // Nome de cada recurso por tipo — a lista concedida cruza os cinco tipos, não só o selecionado.
  const [nomesRecurso, setNomesRecurso] = useState<Record<string, Record<string, string>>>({});
  // Selecionar sujeito/recurso a partir de uma linha concedida: trocar o TIPO dispara a recarga da
  // lista, que zera o id escolhido — o alvo fica guardado aqui e é aplicado quando a lista chega.
  const sujeitoDesejado = useRef<string | null>(null);
  const recursoDesejado = useRef<string | null>(null);

  // --- Grupos ---
  const [grupos, setGrupos] = useState<Grupo[]>([]);
  const [carregandoGrupos, setCarregandoGrupos] = useState(false);
  const [erroGrupos, setErroGrupos] = useState<string | null>(null);
  const [novoGrupoNome, setNovoGrupoNome] = useState("");
  const [novoGrupoDesc, setNovoGrupoDesc] = useState("");
  const [criandoGrupo, setCriandoGrupo] = useState(false);
  const [usuarios, setUsuarios] = useState<Opcao[]>([]);
  const [membroSel, setMembroSel] = useState<Record<string, string>>({});

  // --- Carregar sujeitos (usuários ou grupos) conforme o tipo ---
  const carregarSujeitos = useCallback(async () => {
    setCarregandoSujeitos(true);
    setSujeitoId("");
    const caminho = sujeitoTipo === "usuario" ? "/usuarios" : "/grupos";
    const r = await chamar<any[]>("GET", caminho);
    setCarregandoSujeitos(false);
    if (!r.ok) {
      setSujeitos([]);
      setMsgPerm({
        tipo: "erro",
        texto: r.erro ?? "Falha ao carregar a lista de sujeitos.",
        requestId: r.requestId,
      });
      return;
    }
    const lista = normalizarLista(r.dados ?? []);
    setSujeitos(lista);
    const alvo = sujeitoDesejado.current;
    sujeitoDesejado.current = null;
    if (!alvo) return;
    // Alvo que veio de uma linha concedida: se não está na lista, DIZ (antes sumia em silêncio e o
    // formulário ficava "— selecione —" sem explicar por quê).
    if (lista.some((s) => s.id === alvo)) setSujeitoId(alvo);
    else
      setMsgPerm({
        tipo: "aten",
        texto: avisoAusente(sujeitoTipo === "grupo" ? "o grupo" : "o usuário"),
      });
  }, [sujeitoTipo, token]);

  useEffect(() => {
    void carregarSujeitos();
  }, [carregarSujeitos]);

  // --- Carregar recursos conforme o tipo selecionado ---
  const carregarRecursos = useCallback(async () => {
    setCarregandoRecursos(true);
    setErroRecursos(null);
    setRecursoId("");
    try {
      let itens: any[] = [];
      switch (recursoTipo) {
        case "pipeline":
          itens = await api.listarPipelines();
          break;
        case "cubo":
          itens = await api.listarCubos();
          break;
        case "view":
          itens = await api.listarViews();
          break;
        case "pagina":
          itens = await api.catalogo();
          break;
        case "conexao":
          itens = await api.listarConexoes();
          break;
      }
      const lista = normalizarLista(itens);
      setRecursos(lista);
      const alvo = recursoDesejado.current;
      recursoDesejado.current = null;
      if (!alvo) return;
      if (lista.some((x) => x.id === alvo)) setRecursoId(alvo);
      else
        setMsgPerm({
          tipo: "aten",
          texto: avisoAusente(`o ${rotuloRecursoTipo(recursoTipo).toLowerCase()}`),
        });
    } catch (e: any) {
      // Sem isto a falha virava "Nenhum disponível" — a tela dizia que não existe recurso quando
      // na verdade a carga tinha falhado.
      setRecursos([]);
      setErroRecursos(e?.message ?? "Falha ao carregar a lista de recursos.");
    } finally {
      setCarregandoRecursos(false);
    }
  }, [recursoTipo, token]);

  useEffect(() => {
    void carregarRecursos();
  }, [carregarRecursos]);

  // --- Grupos + usuários (for the group manager) ---
  const carregarGrupos = useCallback(async () => {
    setCarregandoGrupos(true);
    setErroGrupos(null);
    const r = await chamar<Grupo[]>("GET", "/grupos");
    setCarregandoGrupos(false);
    if (!r.ok) {
      setGrupos([]);
      setErroGrupos(r.erro ?? "Falha ao carregar grupos.");
      return;
    }
    setGrupos((r.dados ?? []) as Grupo[]);
  }, [token]);

  const carregarUsuarios = useCallback(async () => {
    const r = await chamar<any[]>("GET", "/usuarios");
    if (r.ok) setUsuarios(normalizarLista(r.dados ?? []));
  }, [token]);

  useEffect(() => {
    void carregarGrupos();
    void carregarUsuarios();
  }, [carregarGrupos, carregarUsuarios]);

  // Nome de TODO recurso, dos cinco tipos: a lista concedida cruza os tipos e "pipeline a3f2c1…"
  // não diz a que o acesso se refere. Uma falha por tipo não derruba os outros (o id curto fica
  // como identificação).
  const carregarNomesRecurso = useCallback(async () => {
    const tipos: [RecursoTipo, () => Promise<any[]>][] = [
      ["pipeline", api.listarPipelines],
      ["cubo", api.listarCubos],
      ["view", api.listarViews],
      ["pagina", api.catalogo],
      ["conexao", api.listarConexoes],
    ];
    const pares = await Promise.all(
      tipos.map(async ([tipo, carregar]) => {
        try {
          const mapa: Record<string, string> = {};
          for (const it of normalizarLista(await carregar())) mapa[it.id] = it.nome;
          return [tipo, mapa] as const;
        } catch {
          return [tipo, {}] as const;
        }
      }),
    );
    setNomesRecurso(Object.fromEntries(pares));
  }, [token]);

  // Estado atual das permissões = replay da trilha (a API não expõe a consulta).
  const carregarConcedidos = useCallback(async () => {
    setCarregandoConcedidos(true);
    setErroConcedidos(null);
    try {
      const { registros, completo } = await api.consultarAuditoriaTudo({
        recurso_tipo: "permissao",
      });
      setConcedidos(reconstruir(registros));
      setTrilhaParcial(!completo);
    } catch (e: any) {
      setConcedidos(null);
      setErroConcedidos(e?.message ?? "Falha ao ler a trilha de auditoria.");
    } finally {
      setCarregandoConcedidos(false);
    }
  }, [token]);

  useEffect(() => {
    void carregarNomesRecurso();
    void carregarConcedidos();
  }, [carregarNomesRecurso, carregarConcedidos]);

  const nomeSujeito = useMemo(
    () => sujeitos.find((s) => s.id === sujeitoId)?.nome ?? "",
    [sujeitos, sujeitoId],
  );
  const nomeRecurso = useMemo(
    () => recursos.find((r) => r.id === recursoId)?.nome ?? "",
    [recursos, recursoId],
  );
  const recursoRotulo = RECURSOS.find((r) => r.tipo === recursoTipo)?.rotulo ?? "Recurso";

  function corpoAtual(): CorpoPermissao | null {
    if (!sujeitoId || !recursoId) return null;
    return {
      sujeito_tipo: sujeitoTipo,
      sujeito_id: sujeitoId,
      recurso_tipo: recursoTipo,
      recurso_id: recursoId,
      nivel,
    };
  }

  const registrar = (tipo: Registro["tipo"], texto: string) =>
    setRegistros((rs) =>
      [{ hora: fmtHora(new Date().toISOString()), tipo, texto }, ...rs].slice(0, 12),
    );

  async function conceder() {
    const corpo = corpoAtual();
    if (!corpo) return;
    setSalvando(true);
    setMsgPerm(null);
    const r = await chamar<{ criada: boolean }>("POST", "/permissoes", corpo);
    setSalvando(false);
    if (!r.ok) {
      setMsgPerm({
        tipo: "erro",
        texto: r.erro ?? "Falha ao conceder a permissão.",
        requestId: r.requestId,
      });
      return;
    }
    const alvo = `${nomeSujeito} em ${recursoRotulo.toLowerCase()} “${nomeRecurso}”`;
    if (r.dados?.criada) {
      setMsgPerm({
        tipo: "ok",
        texto:
          `Concedido: ${alvo} agora tem ${rotuloNivel(nivel).toLowerCase()} — ` +
          `pode ${listaAcoes(nivel)}.`,
      });
      registrar("concedido", `${rotuloNivel(nivel)} · ${alvo}`);
      void carregarConcedidos();
    } else {
      // `criada:false` = a linha (sujeito, recurso, nível) já existia. Nada foi elevado.
      setMsgPerm({
        tipo: "aten",
        texto: `Nada mudou: ${alvo} já tinha exatamente ${rotuloNivel(nivel).toLowerCase()}.`,
      });
      registrar("sem_efeito", `${rotuloNivel(nivel)} (já existia) · ${alvo}`);
    }
  }

  async function revogar() {
    const corpo = corpoAtual();
    if (!corpo) return;
    setSalvando(true);
    setMsgPerm(null);
    const r = await chamar<unknown>("DELETE", "/permissoes", corpo);
    setSalvando(false);
    if (!r.ok) {
      setMsgPerm({
        tipo: "erro",
        texto: r.erro ?? "Falha ao revogar a permissão.",
        requestId: r.requestId,
      });
      return;
    }
    const alvo = `${nomeSujeito} em ${recursoRotulo.toLowerCase()} “${nomeRecurso}”`;
    // O backend responde 204 mesmo quando não havia nada para remover. Dizer "removido" nos dois
    // casos era a mentira que fazia o admin achar que tinha cortado um acesso que nunca existiu.
    setMsgPerm({
      tipo: jaConcedido ? "ok" : "aten",
      texto: jaConcedido
        ? `Removido o nível ${rotuloNivel(nivel).toLowerCase()} de ${alvo}. ` +
          `Outros níveis concedidos à parte, e o acesso que venha de um grupo, continuam valendo.`
        : `Revogação aplicada em ${alvo}, mas a trilha não registrava ` +
          `${rotuloNivel(nivel).toLowerCase()} aí. Se o nível existia sem passar por esta tela ` +
          `(por exemplo, concedido junto da criação do recurso), ele foi removido; senão, nada mudou.`,
    });
    // A linha da sessão não pode afirmar "revogado" quando a trilha não registrava o nível: a
    // mensagem acima diz "pode não ter mudado nada" e a lista dizia o contrário, em vermelho.
    registrar(
      jaConcedido ? "revogado" : "sem_efeito",
      `${rotuloNivel(nivel)}${jaConcedido ? "" : " (não registrado na trilha)"} · ${alvo}`,
    );
    void carregarConcedidos();
  }

  // Revoga a partir da LINHA concedida: a tupla vem do que está concedido, não do formulário —
  // este é o caminho em que "Revogar" deixa de ser chute.
  async function revogarLinha(g: Concedido, sujeito: string, recurso: string) {
    setSalvando(true);
    setMsgPerm(null);
    const r = await chamar<unknown>("DELETE", "/permissoes", {
      sujeito_tipo: g.sujeito_tipo,
      sujeito_id: g.sujeito_id,
      recurso_tipo: g.recurso_tipo,
      recurso_id: g.recurso_id,
      nivel: g.nivel,
    });
    setSalvando(false);
    if (!r.ok) {
      setMsgPerm({
        tipo: "erro",
        texto: r.erro ?? "Falha ao revogar a permissão.",
        requestId: r.requestId,
      });
      return;
    }
    const alvo = `${sujeito} em ${rotuloRecursoTipo(g.recurso_tipo).toLowerCase()} “${recurso}”`;
    setMsgPerm({
      tipo: "ok",
      texto:
        `Removido o nível ${rotuloNivel(g.nivel).toLowerCase()} de ${alvo}. ` +
        `Outros níveis concedidos à parte, e o acesso que venha de um grupo, continuam valendo.`,
    });
    registrar("revogado", `${rotuloNivel(g.nivel)} · ${alvo}`);
    void carregarConcedidos();
  }

  // Leva a linha concedida para o formulário (para elevar/rebaixar o nível sem redigitar a tupla).
  // Trocar o tipo recarrega a lista e zera o id — daí o alvo ir num ref até a lista chegar.
  //
  // A permissão pode apontar para um sujeito/recurso que não existe mais (a lista marca esse caso
  // com "(removido?)"). Selecionar um id fora das opções deixava o `<select>` em branco com o id
  // no estado: o resumo lia “ver em pipeline ””” e Conceder/Revogar iam para um alvo que a tela
  // não conseguia nomear. Só entra no formulário o que está na lista; o resto vira aviso.
  function usarNoFormulario(g: Concedido) {
    setNivel(g.nivel);
    const ausentes: string[] = [];
    if (g.sujeito_tipo === sujeitoTipo) {
      if (sujeitos.some((s) => s.id === g.sujeito_id)) setSujeitoId(g.sujeito_id);
      else ausentes.push(g.sujeito_tipo === "grupo" ? "o grupo" : "o usuário");
    } else {
      sujeitoDesejado.current = g.sujeito_id;
      setSujeitoTipo(g.sujeito_tipo);
    }
    if (g.recurso_tipo === recursoTipo) {
      if (recursos.some((r) => r.id === g.recurso_id)) setRecursoId(g.recurso_id);
      else ausentes.push(`o ${rotuloRecursoTipo(g.recurso_tipo).toLowerCase()}`);
    } else {
      recursoDesejado.current = g.recurso_id;
      setRecursoTipo(g.recurso_tipo);
    }
    if (ausentes.length) setMsgPerm({ tipo: "aten", texto: avisoAusente(ausentes.join(" e ")) });
  }

  // --- Ações de grupo ---
  async function criarGrupo() {
    if (!novoGrupoNome.trim()) {
      setErroGrupos("Informe o nome do grupo.");
      return;
    }
    setCriandoGrupo(true);
    setErroGrupos(null);
    const r = await chamar<{ id: string }>("POST", "/grupos", {
      nome: novoGrupoNome.trim(),
      descricao: novoGrupoDesc.trim() || null,
    });
    setCriandoGrupo(false);
    if (!r.ok) {
      setErroGrupos(r.erro ?? "Falha ao criar o grupo.");
      return;
    }
    setNovoGrupoNome("");
    setNovoGrupoDesc("");
    await carregarGrupos();
    // O grupo novo pode ser sujeito de permissão agora: recarrega a lista se ela está em uso.
    if (sujeitoTipo === "grupo") await carregarSujeitos();
  }

  async function excluirGrupo(id: string) {
    setErroGrupos(null);
    const r = await chamar<unknown>("DELETE", `/grupos/${id}`);
    if (!r.ok) {
      setErroGrupos(r.erro ?? "Falha ao excluir o grupo.");
      return;
    }
    await carregarGrupos();
    if (sujeitoTipo === "grupo") await carregarSujeitos();
  }

  async function adicionarMembro(grupoId: string) {
    const usuarioId = membroSel[grupoId];
    if (!usuarioId) return;
    setErroGrupos(null);
    const r = await chamar<unknown>("POST", `/grupos/${grupoId}/membros`, {
      usuario_id: usuarioId,
    });
    if (!r.ok) {
      setErroGrupos(r.erro ?? "Falha ao adicionar o membro.");
      return;
    }
    setMembroSel((m) => ({ ...m, [grupoId]: "" }));
    await carregarGrupos();
  }

  async function removerMembro(grupoId: string, usuarioId: string) {
    setErroGrupos(null);
    const r = await chamar<unknown>("DELETE", `/grupos/${grupoId}/membros/${usuarioId}`);
    if (!r.ok) {
      setErroGrupos(r.erro ?? "Falha ao remover o membro.");
      return;
    }
    await carregarGrupos();
  }

  const alvoCompleto = !!sujeitoId && !!recursoId;
  const podeAgir = alvoCompleto && !salvando;
  const faltando = !sujeitoId
    ? sujeitoTipo === "usuario"
      ? "Escolha o usuário."
      : "Escolha o grupo."
    : !recursoId
      ? `Escolha o ${recursoRotulo.toLowerCase()}.`
      : "";

  // --- Lista do que está concedido (nomes resolvidos; id curto quando o nome não é conhecido) ---
  const nomeDeUsuario = useCallback(
    (id: string | null) => (id ? (usuarios.find((u) => u.id === id)?.nome ?? null) : null),
    [usuarios],
  );
  const nomeDeSujeito = useCallback(
    (g: Concedido) =>
      (g.sujeito_tipo === "grupo"
        ? grupos.find((x) => x.id === g.sujeito_id)?.nome
        : nomeDeUsuario(g.sujeito_id)) ?? `${g.sujeito_id.slice(0, 8)}… (removido?)`,
    [grupos, nomeDeUsuario],
  );
  const nomeDeRecurso = useCallback(
    (g: Concedido) =>
      nomesRecurso[g.recurso_tipo]?.[g.recurso_id] ?? `${g.recurso_id.slice(0, 8)}… (removido?)`,
    [nomesRecurso],
  );

  const listaConcedidos = useMemo(() => {
    const arr = (concedidos ?? []).map((g) => ({
      g,
      sujeito: nomeDeSujeito(g),
      recurso: nomeDeRecurso(g),
    }));
    arr.sort(
      (a, b) =>
        a.sujeito.localeCompare(b.sujeito, "pt-BR") ||
        a.recurso.localeCompare(b.recurso, "pt-BR") ||
        pesoNivel(b.g.nivel) - pesoNivel(a.g.nivel),
    );
    return arr;
  }, [concedidos, nomeDeSujeito, nomeDeRecurso]);

  // O formulário deixa de ser cego: o que a trilha sabe sobre o alvo selecionado AGORA.
  const jaConcedido = useMemo(
    () =>
      (concedidos ?? []).find(
        (g) =>
          g.sujeito_tipo === sujeitoTipo &&
          g.sujeito_id === sujeitoId &&
          g.recurso_tipo === recursoTipo &&
          g.recurso_id === recursoId &&
          g.nivel === nivel,
      ) ?? null,
    [concedidos, sujeitoTipo, sujeitoId, recursoTipo, recursoId, nivel],
  );
  const outrosNiveis = useMemo(
    () =>
      (concedidos ?? []).filter(
        (g) =>
          g.sujeito_tipo === sujeitoTipo &&
          g.sujeito_id === sujeitoId &&
          g.recurso_tipo === recursoTipo &&
          g.recurso_id === recursoId &&
          g.nivel !== nivel,
      ),
    [concedidos, sujeitoTipo, sujeitoId, recursoTipo, recursoId, nivel],
  );

  const itensConcedidos: ItemCadastro[] = listaConcedidos.map(({ g, sujeito, recurso }) => ({
    id: g.chave,
    testid: "pm-concedido",
    nome: sujeito,
    meta: g.sujeito_tipo === "grupo" ? "grupo" : "usuário",
    status: rotuloNivel(g.nivel).toLowerCase(),
    // Nível não é desfecho: `falha` (vermelho) para "administrar" e `ok` (verde) para "ver" pintavam
    // de erro e de sucesso uma configuração — a lista abria com linhas vermelhas que não são falha
    // nenhuma. Duas faixas honestas, no vocabulário que o produto já usa: âmbar = "pode escrever"
    // (o mesmo sentido de `permitir_escrita` em Conexões), neutro = acesso de leitura/execução.
    scls: g.nivel === "administrar" || g.nivel === "editar" ? "skip" : "off",
    campos: [
      {
        rotulo: "Recurso",
        valor: (
          <span>
            {recurso}
            {/* `.dg-sub` só é bloco dentro de `.dg-nome` (CSS do grid) — fora dali, o display vem
                daqui, como nas outras listas. */}
            <span
              className="dg-sub"
              style={{ display: "block", fontSize: 11.5, color: "var(--ink-2)" }}
            >
              {rotuloRecursoTipo(g.recurso_tipo).toLowerCase()}
            </span>
          </span>
        ),
      },
      {
        rotulo: "Pode",
        valor: <span title={`Nível ${rotuloNivel(g.nivel)}`}>{listaCobertura(g.nivel)}</span>,
      },
      { rotulo: "Concedido por", valor: nomeDeUsuario(g.por) ?? "—" },
      {
        rotulo: "Quando (GMT-3)",
        valor: <span title={fmtMomento(g.quando)}>{fmtMomentoCurto(g.quando)}</span>,
      },
    ],
  }));

  return (
    <>
      {pedido?.tipo === "revogar" && (
        <ConfirmarAcao
          titulo="Revogar este nível"
          corpo={
            <>
              Remove <b>{rotuloNivel(nivel).toLowerCase()}</b> de <b>{nomeSujeito}</b> em{" "}
              {recursoRotulo.toLowerCase()} <b>{nomeRecurso}</b>.
              <br />
              Revoga <b>apenas este nível</b>. Se {nomeSujeito} também recebeu outro nível neste
              recurso, ou pertence a um grupo com acesso a ele, esse acesso <b>continua valendo</b> —
              confira antes de considerar o acesso encerrado.
            </>
          }
          rotuloConfirmar="Revogar nível"
          perigo
          onConfirmar={() => {
            setPedido(null);
            void revogar();
          }}
          onCancelar={() => setPedido(null)}
        />
      )}
      {pedido?.tipo === "revogar_linha" && (
        <ConfirmarAcao
          titulo="Revogar este nível"
          corpo={
            <>
              Remove <b>{rotuloNivel(pedido.g.nivel).toLowerCase()}</b> de <b>{pedido.sujeito}</b>{" "}
              em {rotuloRecursoTipo(pedido.g.recurso_tipo).toLowerCase()} <b>{pedido.recurso}</b>.
              <br />
              Revoga <b>apenas este nível</b>. Outros níveis desta mesma pessoa (ou de um grupo a
              que ela pertença) neste recurso <b>continuam valendo</b> — a lista mostra quais.
            </>
          }
          rotuloConfirmar="Revogar nível"
          perigo
          onConfirmar={() => {
            const p = pedido;
            setPedido(null);
            void revogarLinha(p.g, p.sujeito, p.recurso);
          }}
          onCancelar={() => setPedido(null)}
        />
      )}
      {pedido?.tipo === "excluir_grupo" && (
        <ConfirmarAcao
          titulo="Excluir o grupo"
          corpo={
            <>
              Excluir <b>{pedido.grupo.nome}</b> apaga <b>todas as permissões concedidas ao grupo</b>{" "}
              e desfaz o vínculo dos {pedido.grupo.membros?.length ?? 0} membro
              {(pedido.grupo.membros?.length ?? 0) === 1 ? "" : "s"}. Quem tinha acesso só por este
              grupo perde o acesso na hora. Esta ação não pode ser desfeita.
            </>
          }
          rotuloConfirmar="Excluir grupo"
          perigo
          onConfirmar={() => {
            const g = pedido.grupo;
            setPedido(null);
            void excluirGrupo(g.id);
          }}
          onCancelar={() => setPedido(null)}
        />
      )}
      {pedido?.tipo === "remover_membro" && (
        <ConfirmarAcao
          titulo="Remover do grupo"
          corpo={
            <>
              <b>{pedido.membroNome}</b> sai de <b>{pedido.grupo.nome}</b> e perde o acesso que vinha
              deste grupo. Permissões concedidas diretamente a ele continuam valendo.
            </>
          }
          rotuloConfirmar="Remover do grupo"
          perigo
          onConfirmar={() => {
            const p = pedido;
            setPedido(null);
            void removerMembro(p.grupo.id, p.membroId);
          }}
          onCancelar={() => setPedido(null)}
        />
      )}

      <div className="page-head">
        <h1 className="display">Permissões</h1>
        <p className="sub">
          Acesso a um recurso específico, para um usuário ou um grupo. Quem cria um recurso já o
          administra, e o perfil admin passa em tudo — aqui é o acesso do resto do time.
        </p>
      </div>

      <div className="pad">
        {/* ---- Conceder / revogar ---- */}
        <div className="card adm-card">
          <div className="card-h">
            <h3>Conceder ou revogar acesso</h3>
            <span className="tag">níveis cumulativos · uma permissão por vez</span>
          </div>
          <div className="adm-card-corpo">
            <div className="selrow">
              <fieldset className="pm-grupo">
                <legend>Quem recebe</legend>
              <div className="f">
                <label htmlFor="pm-sujeito-tipo">Tipo de sujeito</label>
                <select
                  id="pm-sujeito-tipo"
                  value={sujeitoTipo}
                  onChange={(e) => setSujeitoTipo(e.target.value as SujeitoTipo)}
                >
                  <option value="usuario">Usuário</option>
                  <option value="grupo">Grupo</option>
                </select>
              </div>
              <div className="f">
                <label htmlFor="pm-sujeito">
                  {sujeitoTipo === "usuario" ? "Usuário" : "Grupo"}
                </label>
                <select
                  id="pm-sujeito"
                  value={sujeitoId}
                  onChange={(e) => setSujeitoId(e.target.value)}
                  disabled={carregandoSujeitos}
                >
                  <option value="">
                    {carregandoSujeitos
                      ? "Carregando…"
                      : sujeitos.length
                        ? "— selecione —"
                        : sujeitoTipo === "usuario"
                          ? "Nenhum usuário cadastrado"
                          : "Nenhum grupo cadastrado"}
                  </option>
                  {sujeitos.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.nome}
                    </option>
                  ))}
                </select>
              </div>
              </fieldset>
              <fieldset className="pm-grupo">
                <legend>Sobre o quê</legend>
              <div className="f">
                <label htmlFor="pm-recurso-tipo">Tipo de recurso</label>
                <select
                  id="pm-recurso-tipo"
                  value={recursoTipo}
                  onChange={(e) => setRecursoTipo(e.target.value as RecursoTipo)}
                >
                  {RECURSOS.map((r) => (
                    <option key={r.tipo} value={r.tipo}>
                      {r.rotulo}
                    </option>
                  ))}
                </select>
              </div>
              <div className="f">
                <label htmlFor="pm-recurso">{recursoRotulo}</label>
                <select
                  id="pm-recurso"
                  value={recursoId}
                  onChange={(e) => setRecursoId(e.target.value)}
                  disabled={carregandoRecursos}
                  aria-describedby={erroRecursos ? "pm-recurso-erro" : undefined}
                >
                  <option value="">
                    {carregandoRecursos
                      ? "Carregando…"
                      : erroRecursos
                        ? "Falha ao carregar"
                        : recursos.length
                          ? "— selecione —"
                          : `Nenhum ${recursoRotulo.toLowerCase()} cadastrado`}
                  </option>
                  {recursos.map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.nome}
                    </option>
                  ))}
                </select>
                {erroRecursos && (
                  <span className="adm-erro-inline" id="pm-recurso-erro" role="alert">
                    {erroRecursos}{" "}
                    <button type="button" className="lk" onClick={() => void carregarRecursos()}>
                      Tentar de novo
                    </button>
                  </span>
                )}
              </div>
              </fieldset>
            </div>

            {/* Escada de níveis: o escolhido em destaque, os que ele JÁ INCLUI marcados como
                incluídos. Sem isso, "editar" e "ver" pareciam alternativas independentes. */}
            <fieldset className="adm-escada">
              <legend className="eyebrow">Nível de acesso</legend>
              {/* `aria-pressed` + agrupamento pelo <fieldset>/<legend>: mesma receita dos chips de
                  status de Execuções (um grupo de botões de escolha única, não um menu). */}
              <div className="adm-escada-chips">
                {NIVEIS.map((n) => {
                  const escolhido = n.nivel === nivel;
                  const incluido = incluidos(nivel).includes(n.nivel);
                  return (
                    <button
                      key={n.nivel}
                      type="button"
                      aria-pressed={escolhido}
                      data-testid={`pm-nivel-${n.nivel}`}
                      className={
                        "chip adm-nivel" +
                        (escolhido ? " on" : "") +
                        (incluido ? " adm-nivel--inc" : "")
                      }
                      onClick={() => setNivel(n.nivel)}
                    >
                      <span className="mk" />
                      <span className="adm-nivel-rot">{n.rotulo}</span>
                      <span className="adm-nivel-oque">{n.oque}</span>
                      {incluido && <span className="adm-nivel-selo">incluído</span>}
                    </button>
                  );
                })}
              </div>
              <p className="adm-escada-nota" data-testid="pm-escada-nota">
                Cumulativo: <b>administrar</b> ⊇ <b>editar</b> ⊇ <b>executar</b> ⊇ <b>ver</b>. Quem
                recebe <b>{rotuloNivel(nivel).toLowerCase()}</b> pode {listaAcoes(nivel)}
                {nivel === "ver"
                  ? " — e nada além disso."
                  : " — não é preciso conceder os níveis de baixo separadamente."}
              </p>
            </fieldset>

            <div className="adm-resumo" data-testid="pm-resumo">
              {alvoCompleto ? (
                <>
                  <b>{nomeSujeito}</b> {sujeitoTipo === "grupo" ? "(grupo)" : ""} →{" "}
                  <b>{rotuloNivel(nivel).toLowerCase()}</b> em {recursoRotulo.toLowerCase()}{" "}
                  <b>{nomeRecurso}</b>
                  {/* Estado do alvo pela trilha: sem isto, conceder o que já existe e revogar o
                      que não existe davam a MESMA resposta de sucesso. */}
                  {concedidos && (
                    <span className="adm-resumo-estado" data-testid="pm-resumo-estado">
                      {jaConcedido
                        ? `já concedido em ${fmtMomentoCurto(jaConcedido.quando)}` +
                          (nomeDeUsuario(jaConcedido.por)
                            ? ` por ${nomeDeUsuario(jaConcedido.por)}`
                            : "")
                        : "ainda não concedido"}
                      {outrosNiveis.length > 0 &&
                        ` · também tem ${outrosNiveis
                          .map((o) => rotuloNivel(o.nivel).toLowerCase())
                          .join(", ")} neste recurso`}
                    </span>
                  )}
                </>
              ) : (
                <span className="adm-resumo-falta">{faltando}</span>
              )}
            </div>

            <div className="adm-acoes">
              {/* Botão desabilitado tem de dizer POR QUÊ no próprio botão: o motivo já aparece no
                  resumo acima, mas quem vai clicar olha para o botão apagado, não para o resumo. */}
              <button
                className="btn-stamp"
                data-testid="pm-conceder"
                onClick={() => void conceder()}
                disabled={!podeAgir}
                title={podeAgir ? undefined : salvando ? "Processando…" : faltando}
              >
                <Icon name="stamp" cls="ic" /> {salvando ? "Processando…" : "Conceder"}
              </button>
              <button
                className="btn-sec"
                data-testid="pm-revogar"
                onClick={() => setPedido({ tipo: "revogar" })}
                disabled={!podeAgir}
                title={podeAgir ? undefined : salvando ? "Processando…" : faltando}
              >
                Revogar este nível
              </button>
            </div>

            {msgPerm && (
              <div
                className={
                  "card adm-banner " +
                  (msgPerm.tipo === "ok"
                    ? "adm-banner--ok"
                    : msgPerm.tipo === "erro"
                      ? "adm-banner--erro"
                      : "adm-banner--aten")
                }
                role={msgPerm.tipo === "erro" ? "alert" : "status"}
                data-testid="pm-msg"
              >
                <span className="adm-banner-d" />
                <span>
                  {msgPerm.texto}
                  {msgPerm.requestId && (
                    <span className="adm-banner-rid"> · código de suporte {msgPerm.requestId}</span>
                  )}
                </span>
                <button
                  type="button"
                  className="adm-banner-x"
                  aria-label="Fechar aviso"
                  onClick={() => setMsgPerm(null)}
                >
                  ×
                </button>
              </div>
            )}

            {registros.length > 0 && (
              <div className="adm-sessao">
                <h4 className="eyebrow">Feito nesta sessão (GMT-3)</h4>
                <ul className="adm-sessao-lista" data-testid="pm-sessao">
                  {registros.map((r, i) => (
                    <li key={i}>
                      <span className="num adm-sessao-hora">{r.hora}</span>
                      <span
                        className={
                          "statuspill " +
                          (r.tipo === "concedido" ? "ok" : r.tipo === "revogado" ? "falha" : "skip")
                        }
                      >
                        <span className="d" />
                        {r.tipo === "concedido"
                          ? "concedido"
                          : r.tipo === "revogado"
                            ? "revogado"
                            : "sem efeito"}
                      </span>
                      <span className="adm-sessao-txt">{r.texto}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </div>

        {/* ---- O que já está concedido (reconstruído da trilha) ---- */}
        <div className="card adm-card">
          <div className="card-h">
            <h3>Acesso concedido</h3>
            <span className="tag junto" data-testid="pm-concedidos-contagem">
              {carregandoConcedidos
                ? "lendo a trilha…"
                : !concedidos
                  ? "indisponível"
                  : concedidos.length === 0
                    ? "nada concedido por esta tela"
                    : `${concedidos.length} permissã${concedidos.length === 1 ? "o" : "es"}`}
            </span>
            <div className="exec-h-acoes">
              <button
                type="button"
                className="pag-btn"
                disabled={carregandoConcedidos}
                data-testid="pm-concedidos-atualizar"
                onClick={() => void carregarConcedidos()}
              >
                Atualizar
              </button>
            </div>
          </div>
          <div className="adm-card-corpo">
            {/* Não é a verdade completa do acesso, e a tela diz qual pedaço é. */}
            <p className="adm-dica">
              Reconstruído da <b>trilha de auditoria</b> (append-only): cada concessão e cada
              revogação feitas por esta tela, reprocessadas na ordem em que aconteceram.{" "}
              <b>Não inclui</b> o acesso automático de quem criou o recurso nem o bypass do perfil
              admin — esses não passam por aqui.
            </p>

            {trilhaParcial && (
              <div className="card adm-banner adm-banner--aten" role="status">
                <span className="adm-banner-d" />
                <span>
                  A trilha tem mais registros do que esta tela lê de uma vez — a lista abaixo pode
                  estar desatualizada. Consulte a trilha completa em <b>Auditoria</b>.
                </span>
              </div>
            )}

            {erroConcedidos ? (
              <div className="card adm-banner adm-banner--erro" role="alert" data-testid="pm-concedidos-erro">
                <span className="adm-banner-d" />
                <span>
                  {erroConcedidos} Sem a trilha, esta tela concede e revoga, mas não mostra o que
                  já está concedido.
                </span>
                <button
                  type="button"
                  className="pag-btn adm-banner-acao"
                  onClick={() => void carregarConcedidos()}
                >
                  Tentar de novo
                </button>
              </div>
            ) : (
              <GridLista
                rotulo="Permissões concedidas"
                carregando={carregandoConcedidos && !concedidos}
                itens={itensConcedidos}
                abrirLabel="Usar no formulário"
                onAbrir={(chave) => {
                  const alvo = listaConcedidos.find((x) => x.g.chave === chave);
                  if (alvo) usarNoFormulario(alvo.g);
                }}
                acoes={(it) => {
                  const alvo = listaConcedidos.find((x) => x.g.chave === it.id);
                  if (!alvo) return null;
                  return (
                    <>
                      <button type="button" onClick={() => usarNoFormulario(alvo.g)}>
                        Usar no formulário
                      </button>
                      <button
                        type="button"
                        className="del"
                        disabled={salvando}
                        data-testid={`pm-revogar-linha-${alvo.g.chave}`}
                        onClick={() =>
                          setPedido({
                            tipo: "revogar_linha",
                            g: alvo.g,
                            sujeito: alvo.sujeito,
                            recurso: alvo.recurso,
                          })
                        }
                      >
                        Revogar
                      </button>
                    </>
                  );
                }}
                vazio={
                  <div className="lc-empty">
                    <div className="big">Nenhuma permissão concedida por esta tela</div>
                    <div>
                      Quem cria um recurso já o administra, e o perfil admin passa em tudo — esses
                      acessos existem sem aparecer aqui.
                    </div>
                  </div>
                }
              />
            )}
          </div>
        </div>

        {/* ---- Grupos ---- */}
        <div className="card adm-card">
          <div className="card-h">
            <h3>Grupos</h3>
            <span className="tag junto">
              {grupos.length === 0
                ? "nenhum grupo"
                : `${grupos.length} grupo${grupos.length === 1 ? "" : "s"}`}
            </span>
          </div>
          <div className="adm-card-corpo">
            <p className="adm-dica">
              Um grupo recebe permissões uma vez e todo membro herda. Tirar alguém do grupo tira o
              acesso que vinha dele.
            </p>
            <form
              className="selrow adm-selrow-fim"
              onSubmit={(e) => {
                e.preventDefault();
                void criarGrupo();
              }}
            >
              <div className="f">
                <label htmlFor="pm-grupo-nome">Nome do grupo</label>
                <input
                  id="pm-grupo-nome"
                  className="nm"
                  value={novoGrupoNome}
                  onChange={(e) => setNovoGrupoNome(e.target.value)}
                  placeholder="ex.: Auditoria"
                />
              </div>
              <div className="f adm-f-larga">
                <label htmlFor="pm-grupo-desc">Descrição (opcional)</label>
                <input
                  id="pm-grupo-desc"
                  className="nm"
                  value={novoGrupoDesc}
                  onChange={(e) => setNovoGrupoDesc(e.target.value)}
                  placeholder="finalidade do grupo"
                />
              </div>
              <button className="btn-stamp" type="submit" disabled={criandoGrupo}>
                {criandoGrupo ? "Criando…" : "+ Criar grupo"}
              </button>
            </form>

            {erroGrupos && (
              <div className="card adm-banner adm-banner--erro" role="alert" data-testid="pm-erro-grupos">
                <span className="adm-banner-d" />
                <span>{erroGrupos}</span>
                <button
                  type="button"
                  className="adm-banner-x"
                  aria-label="Fechar aviso"
                  onClick={() => setErroGrupos(null)}
                >
                  ×
                </button>
              </div>
            )}

            <div className="card lc-list adm-grupos">
              {carregandoGrupos && grupos.length === 0 ? (
                <div className="lc-empty">Carregando grupos…</div>
              ) : grupos.length === 0 ? (
                <div className="lc-empty">
                  <div className="big">Nenhum grupo cadastrado</div>
                  <div>Crie um grupo acima para conceder acesso a várias pessoas de uma vez.</div>
                </div>
              ) : (
                grupos.map((g) => {
                  const membros = g.membros ?? [];
                  const idsMembros = new Set(membros.map((m) => m.id));
                  // Só quem ainda NÃO é membro aparece na lista de adicionar.
                  const candidatos = usuarios.filter((u) => !idsMembros.has(u.id));
                  return (
                    <div key={g.id} className="lc-row adm-grupo-row">
                      <div className="lc-main adm-grupo-main">
                        <div className="lc-nome">{g.nome}</div>
                        <div className="lc-meta">
                          {g.descricao || "sem descrição"} ·{" "}
                          {membros.length === 0
                            ? "nenhum membro"
                            : `${membros.length} membro${membros.length === 1 ? "" : "s"}`}
                        </div>
                        {membros.length > 0 && (
                          <div className="adm-membros">
                            {membros.map((m) => (
                              <span key={m.id} className="zchip">
                                {m.nome || m.email || m.id}
                                <button
                                  className="x"
                                  type="button"
                                  aria-label={`Remover ${m.nome || m.email || m.id} do grupo ${g.nome}`}
                                  title="Remover do grupo"
                                  onClick={() =>
                                    setPedido({
                                      tipo: "remover_membro",
                                      grupo: g,
                                      membroId: m.id,
                                      membroNome: m.nome || m.email || m.id,
                                    })
                                  }
                                >
                                  ×
                                </button>
                              </span>
                            ))}
                          </div>
                        )}
                      </div>
                      <div className="lc-acts adm-grupo-acts">
                        <select
                          className="nm adm-sel-membro"
                          aria-label={`Adicionar membro em ${g.nome}`}
                          value={membroSel[g.id] ?? ""}
                          disabled={candidatos.length === 0}
                          onChange={(e) =>
                            setMembroSel((m) => ({ ...m, [g.id]: e.target.value }))
                          }
                        >
                          <option value="">
                            {candidatos.length === 0
                              ? "todos já são membros"
                              : "+ adicionar membro…"}
                          </option>
                          {candidatos.map((u) => (
                            <option key={u.id} value={u.id}>
                              {u.nome}
                            </option>
                          ))}
                        </select>
                        <button
                          type="button"
                          disabled={!membroSel[g.id]}
                          onClick={() => void adicionarMembro(g.id)}
                        >
                          Adicionar
                        </button>
                        <button
                          type="button"
                          className="del"
                          onClick={() => setPedido({ tipo: "excluir_grupo", grupo: g })}
                        >
                          Excluir grupo
                        </button>
                      </div>
                    </div>
                  );
                })
              )}
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
