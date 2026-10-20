import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useSelector } from "react-redux";

import { SeletorVersao } from "../SeletorVersao";
import * as api from "../../api/client";
import { apiUrl } from "../../prefixo";
import type {
  ExecConfig,
  ExecParam,
  ParametroPipeline,
  PipelineResumo,
} from "../../api/client";
import type { RootState } from "../../store/store";
import {
  Bloco,
  type BlocoTipo,
  type BlocoView,
  campoSistema,
  type CampoItem,
  ConfirmarAcao,
  GridLista,
  Icon,
  Interruptor,
  type ItemCadastro,
} from "../ui";
import {
  celula as celulaDado,
  diaSP,
  fmtHora,
  perfilColuna,
  rotuloDia,
  type PerfilColuna,
} from "../execucao/formato";
import { carregarListasAuxiliares, lerCatalogoComRascunhos } from "./paginaCarga";

// ============================================================================
// Construtor de página — QUADRO LIVRE (free canvas).
//
// Gaps de API conhecidos (a UI os DECLARA em vez de esconder):
//  - NÃO existe "GET /paginas" (listar todas). A lista usa GET /catalogo, que só devolve páginas
//    PUBLICADAS. Uma página criada e não publicada ficaria inalcançável para sempre pela UI — por
//    isso os ids criados aqui ficam registrados no navegador (`CHAVE_RASCUNHOS`) e aparecem na
//    lista marcados como rascunho, com o aviso de que só o publicar as torna visíveis a todos.
//  - Sem helper no client p/ consultar view / criar / salvar página: usamos fetch(apiUrl("/…")) local
//    seguindo o padrão de api/client.ts.
// ============================================================================

// --- Helpers de fetch locais (endpoints sem cobertura no client) ---
// Replicam o padrão Bearer + envelope de erro pt-BR de api/client.ts; o token
// vem do Redux store (mesmo padrão de Usuarios.tsx/Permissoes.tsx).
async function reqJson(token: string | null, metodo: string, caminho: string, corpo?: unknown): Promise<any> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const r = await fetch(apiUrl(caminho), {
    method: metodo,
    headers,
    body: corpo === undefined ? undefined : JSON.stringify(corpo),
  });
  const j = await r.json().catch(() => null);
  if (!r.ok) {
    const e = j?.erro;
    const det = (e?.detalhes ?? []).map((d: any) => d.mensagem).join(" · ");
    // `request_id` fica no texto: é o que o suporte pede para achar a requisição no log.
    const ref = e?.request_id ? ` (ref. ${String(e.request_id).slice(0, 8)})` : "";
    throw new Error(e ? `${e.mensagem}${det ? ` — ${det}` : ""}${ref}` : "Falha na requisição.");
  }
  return j;
}

interface DetalhePagina {
  nome: string;
  numero: number;
  ultimo?: number;
  definicao: any;
}

// Detalhe da página (GET /paginas/{id}) — não há helper no client.
async function obterPagina(token: string | null, id: string, numero?: number): Promise<DetalhePagina> {
  return reqJson(token, "GET", numero != null ? `/paginas/${id}?numero=${numero}` : `/paginas/${id}`);
}
async function listarVersoesPagina(token: string | null, id: string): Promise<import("../../api/client").VersaoResumo[]> {
  return reqJson(token, "GET", `/paginas/${id}/versoes`);
}

// Consultar view publicada → preencher cols/linhas do bloco.
async function consultarView(token: string | null, id: string): Promise<ResultadoView> {
  return reqJson(token, "POST", `/views/${id}/consultar`, { filtros: null });
}

// Definição da view publicada — só para ler `filtros_liberados` (quais dimensões o Portal aceita
// como filtro de página). Sem isso a aba Filtros era um painel morto: mostrava o que já estava
// salvo e não tinha como oferecer nada novo.
async function obterViewDef(token: string | null, id: string): Promise<{ definicao: any }> {
  return reqJson(token, "GET", `/views/${id}`);
}

// --- Tipos locais ---
type Tipo = BlocoTipo; // "pivot" | "tabela" | "valor"

interface ResultadoView {
  colunas?: string[];
  linhas?: Record<string, unknown>[];
  layout?: { linhas?: string[]; colunas?: string[]; valores?: string[] };
  dimensoes?: string[];
  medidas?: string[];
  origem?: { snapshot?: string; execucao_id?: string; no_id?: string; hash?: string };
}

// Estado do dado de UMA view: carregando ≠ vazio ≠ erro. Antes tudo caía num placeholder
// "— / sem dados", então "a view não tem linhas" e "a fonte não tem execução concluída" eram a
// mesma tela — e a segunda é um erro que exige ação.
type DadoView =
  | { estado: "carregando" }
  | { estado: "erro"; mensagem: string }
  | { estado: "ok"; res: ResultadoView };

// Rótulo amigável do tipo de um parâmetro do pipeline (só informativo na aba Execução).
const ROTULO_TIPO_PARAM: Record<string, string> = {
  inteiro: "número",
  texto: "texto",
  data: "data",
  opcoes: "lista",
  competencia: "competência",
};

// Operadores de filtro da view (espelho de `_OPS` em core/.../modelagem/consulta.py) em símbolo
// legível: o estado vazio precisa dizer QUAL comparação a view faz, e "maior_igual" não é frase.
const OP_FILTRO: Record<string, string> = {
  igual: "=",
  diferente: "≠",
  maior: ">",
  menor: "<",
  maior_igual: "≥",
  menor_igual: "≤",
  contem: "contém",
};

const ROTULO_TIPO_BLOCO: Record<Tipo, string> = {
  pivot: "pivot",
  tabela: "tabela",
  valor: "cartão-KPI",
};

interface BlocoEstado {
  k: number; // chave estável local
  vid: string; // id da view que o bloco referencia (SEMPRE uma view)
  tipo: Tipo; // pivot/tabela = visão completa · valor = KPI (a view como número único)
  total?: boolean; // mostra linha de Total (soma das colunas) no rodapé — configurável por bloco
  x: number;
  y: number;
  w: number;
  h: number;
  titulo?: string;
}

interface ItemPaleta {
  id: string; // vid (id da view publicada)
  nome: string;
  cor: string;
}

const TIPOS: [Tipo, string][] = [
  ["pivot", "pivot"],
  ["tabela", "tabela"],
  ["valor", "KPI"],
];

// `var(--navy)` em vez de #002050 fixo: no tema escuro o navy claro (#5b8bd0) é o que se vê —
// o hex escuro desaparecia no fundo escuro e o bloco perdia a faixa de cor.
const COR_VIEW = "var(--navy)";

// Paleta de cores p/ alternar entre cartões (apenas estética; a 1ª acompanha o tema).
const PALETA_CORES = [COR_VIEW, "#5f8a2e", "#46566b", "#0a2f63", "#b23b43"];

// Página criada e não publicada não aparece em GET /catalogo. Guardamos o id no navegador para
// que o rascunho continue alcançável por quem o criou (é lista de ids, nunca dado do negócio).
const CHAVE_RASCUNHOS = "cflow:paginas-rascunhos";

function lerRascunhos(): string[] {
  try {
    const v = JSON.parse(window.localStorage.getItem(CHAVE_RASCUNHOS) ?? "[]");
    return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
  } catch {
    return [];
  }
}

function gravarRascunhos(ids: string[]) {
  try {
    window.localStorage.setItem(CHAVE_RASCUNHOS, JSON.stringify(ids.slice(-40)));
  } catch {
    /* localStorage indisponível (modo privado): o rascunho só não fica lembrado. */
  }
}

// Texto de apoio que CARREGA informação: --ink-2 (7,2:1) e 11,5px. `.rzhint` (10px/--ink-3) é
// inline-flex com gap — com <b>/<button> dentro, a frase virava itens separados por vãos.
const NOTA: React.CSSProperties = { fontSize: 11.5, color: "var(--ink-2)", lineHeight: 1.45 };

// Título editável do quadro (o nome da página). `font:inherit` puxa a tipografia do <h1> que o
// envolve; o tracejado inferior é o único aviso de que dá para digitar ali — sem ele o campo
// passaria por texto estático. Inline porque `cflow.css` não é editável nesta empreitada.
const TITULO_INPUT: React.CSSProperties = {
  font: "inherit",
  color: "var(--ink)",
  background: "transparent",
  width: "100%",
  minWidth: 0,
  border: "1px solid transparent",
  borderBottom: "1px dashed var(--hair)",
  borderRadius: 5,
  padding: "1px 6px",
  marginLeft: -6,
};

// ---------------------------------------------------------------------------
// Formatação do dado exibido nos blocos — mesma régua da grade de Execuções
// (`execucao/formato.ts`): perfil por COLUNA, DECIMAL (texto) sem perder dígito, identificador
// sem separador de milhar, e NULL distinguível de string vazia.
// ---------------------------------------------------------------------------

// Coluna de view tem rótulo HUMANO ("NÚMERO DA VENDA"), não snake_case. `perfilColuna` decide
// "identificador não leva separador de milhar" pelo NOME — sem normalizar, o número da venda
// aparecia como "39.114". Normaliza acento/espaço só para essa decisão; o rótulo exibido é o original.
function perfisDaView(cols: string[], linhas: Record<string, unknown>[]): Record<string, PerfilColuna> {
  const out: Record<string, PerfilColuna> = {};
  for (const c of cols) {
    const chave = c
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "")
      .replace(/[^A-Za-z0-9]+/g, "_")
      .toLowerCase();
    out[c] = perfilColuna(
      linhas.map((l) => l[c]),
      chave,
    );
  }
  return out;
}

function renderCelula(v: unknown, perfil?: PerfilColuna): ReactNode {
  const cel = celulaDado(v, perfil);
  const titulo = cel.titulo ?? (cel.texto !== String(v) ? String(v) : undefined);
  // `.cel-nula` (itálico, tom de apoio) marca "não é dado": NULL vira "—", texto vazio "(vazio)".
  if (cel.vazio) return <span className="cel-nula" title={titulo}>{cel.texto}</span>;
  return <span title={titulo}>{cel.texto}</span>;
}

// Linha de Total do rodapé (mesma semântica do Portal: soma por COLUNA numérica). Não é fórmula
// livre — é a soma da coluna que a view já produziu, ligada/desligada por bloco.
function linhaTotal(
  mostrar: boolean | undefined,
  cols: string[],
  linhas: Record<string, unknown>[],
  perfis: Record<string, PerfilColuna>,
): ReactNode[] | undefined {
  if (!mostrar) return undefined;
  return cols.map((c, ci) => {
    if (perfis[c]?.tipo !== "numero") return <b key={c}>{ci === 0 ? "Total" : ""}</b>;
    // Aceita número E texto numérico: DECIMAL chega como string, e somar só `typeof number`
    // devolvia "0" numa coluna cheia de valores.
    const soma = linhas.reduce((s, l) => {
      const v = l[c];
      const n = typeof v === "number" ? v : typeof v === "string" && v !== "" ? Number(v) : NaN;
      return s + (Number.isFinite(n) ? n : 0);
    }, 0);
    return <b key={c}>{renderCelula(soma, perfis[c])}</b>;
  });
}

// Soma da 1ª medida da view — o número do cartão-KPI. É a MESMA conta do Portal (`totaisPorMedida`),
// para a prévia não prometer um número diferente do que o operador vai ver.
function totalPrimeiraMedida(res: ResultadoView): { medida: string | null; valor: number; linhas: number } {
  const medidas = res.medidas ?? res.layout?.valores ?? [];
  const linhas = res.linhas ?? [];
  const medida = medidas[0] ?? null;
  if (!medida) return { medida: null, valor: 0, linhas: linhas.length };
  let soma = 0;
  for (const l of linhas) {
    const v = l[medida];
    const n = typeof v === "number" ? v : typeof v === "string" && v !== "" ? Number(v) : NaN;
    if (Number.isFinite(n)) soma += n;
  }
  return { medida, valor: soma, linhas: linhas.length };
}

interface BlocoRender {
  view: BlocoView;
  colsNum: Set<number>;
  total?: ReactNode[];
  linhas: number;
}

function montarRender(res: ResultadoView, mostrarTotal: boolean | undefined, nomeFallback: string): BlocoRender {
  const cols = res.colunas ?? [];
  const linhas = res.linhas ?? [];
  const perfis = perfisDaView(cols, linhas);
  const colsNum = new Set<number>();
  cols.forEach((c, i) => perfis[c]?.tipo === "numero" && colsNum.add(i));
  const kpi = totalPrimeiraMedida(res);
  const perfilKpi = kpi.medida ? perfis[kpi.medida] : undefined;
  return {
    view: {
      cols,
      mini: linhas.map((l) => cols.map((c) => renderCelula(l[c], perfis[c]))),
      kpi: {
        // Rótulo diz de onde o número vem e sobre quantas linhas ele soma — sem isso o cartão
        // exibia um valor grande sem dizer se era total, primeira linha ou média.
        label: kpi.medida
          ? `${kpi.medida}${kpi.linhas > 1 ? ` · soma de ${kpi.linhas} linhas` : ""}`
          : nomeFallback,
        val: kpi.medida ? celulaDado(kpi.valor, perfilKpi).texto : "—",
      },
    },
    colsNum,
    total: linhaTotal(mostrarTotal, cols, linhas, perfis),
    linhas: linhas.length,
  };
}

// ============================================================================
// LISTA — estado real de cada página
//
// `GET /catalogo` devolve só `{id, nome, sistema, execucao}`. Sem enriquecer, a coluna EXECUÇÃO
// dizia "parametrizada" em TODAS as linhas — coluna sem informação — e a lista escondia o que
// define uma página na cadeia `cubo → view → PÁGINA → relatório`:
//   • quais VIEWS ela referencia (o bloco não calcula, ele aponta para uma view);
//   • o que o operador informa antes de gerar (parâmetro da execução + filtro liberado);
//   • qual versão está publicada e desde quando (só a publicada chega ao Portal);
//   • se já saiu daqui um relatório carimbado.
// Uma chamada por página (definição + versões) + UMA varredura dos relatórios, em paralelo e
// depois que a lista já apareceu; falha de uma linha vira "não lido" NELA, sem derrubar as outras.
//
// A definição lida é a ÚLTIMA SALVA (é a que o especialista vai editar). Quando ela está à frente
// da publicada, o pill da linha diz isso — senão a lista descreveria uma definição que o Portal
// não enxerga.
// ============================================================================

interface EstadoPagina {
  blocos: number;
  vids: string[]; // views distintas referenciadas, na ordem de aparição
  liberados: string[]; // `filtros_liberados` — dimensões que o operador escolhe no Portal
  operador: string[]; // rótulos dos parâmetros que o operador preenche antes de rodar
  parametrizada: boolean; // `definicao.execucao` — a página dispara um pipeline
  publicada: { numero: number; em: string | null } | null;
  rascunhoAdiante: number | null; // versão salva à frente da publicada (o Portal vê a outra)
}

// Três fases distinguíveis: "lendo…" não pode virar "nenhuma view", e falha de leitura não pode
// virar zero.
type Fase<T> = { fase: "carregando" } | { fase: "ok"; dado: T } | { fase: "erro" };

function estadoDaPagina(definicao: any, versoes: api.VersaoResumo[]): EstadoPagina {
  const def = definicao ?? {};
  const blocos: any[] = Array.isArray(def.blocos) ? def.blocos : [];
  // `vid` é o nome legado do campo no bloco. Distintas: dois blocos da mesma view contam uma view.
  const vids: string[] = [];
  for (const b of blocos) {
    const v = String(b?.view_id ?? b?.vid ?? "");
    if (v && !vids.includes(v)) vids.push(v);
  }
  const liberados: string[] = Array.isArray(def.filtros_liberados)
    ? def.filtros_liberados.map(String)
    : Array.isArray(def.filtros_pagina)
      ? def.filtros_pagina.map(String)
      : [];
  const exec = def.execucao ?? null;
  const operadorBruto: any[] = Array.isArray(exec?.operador) ? exec.operador : [];
  const pub = versoes.find((v) => v.ativa) ?? null;
  const topo = versoes[0] ?? null;
  return {
    blocos: blocos.length,
    vids,
    liberados,
    // O rótulo é o que o operador lê na tela; sem rótulo, o próprio nome do parâmetro.
    operador: operadorBruto
      .map((p) => String(p?.rotulo || p?.param || "").trim())
      .filter((r) => r !== ""),
    parametrizada: !!exec?.pipeline_id,
    publicada: pub ? { numero: pub.numero, em: pub.criado_em } : null,
    rascunhoAdiante: pub && topo && topo.numero > pub.numero ? topo.numero : null,
  };
}

async function lerEstadoPagina(id: string, definicaoJaLida?: unknown): Promise<EstadoPagina> {
  const [definicao, versoes] = await Promise.all([
    // O rascunho local já teve a definição lida para resolver o nome — não vale pedir de novo.
    definicaoJaLida !== undefined ? Promise.resolve(definicaoJaLida) : lerDefinicao(id),
    api.listarVersoesPagina(id),
  ]);
  return estadoDaPagina(definicao, versoes);
}

async function lerDefinicao(id: string): Promise<unknown> {
  const r = await api.apiFetch("GET", `/paginas/${id}`);
  if (!r.ok) throw new Error("Falha ao ler a definição da página");
  return (await r.json())?.definicao ?? {};
}

interface Carimbo {
  id: string;
  versao: number;
  em: string | null;
  por: string | null;
}

// Último relatório carimbado POR NOME de página. `GET /relatorios` não devolve o id da página, só
// o nome gravado no carimbo — por isso a atribuição é por nome e só vale para nome único (ver
// `nomesAmbiguos`): mostrar o carimbo da página errada numa tela de evidência é pior que não
// mostrar. Renomear a página zera a coluna, porque o carimbo guarda o nome da época.
async function ultimoCarimboPorNome(): Promise<Record<string, Carimbo>> {
  // Sem `api.listarRelatorios()` de propósito: aquele helper devolve [] quando o HTTP falha, e
  // então "nenhum relatório" e "não consegui ler" viram a mesma célula.
  const r = await api.apiFetch("GET", "/relatorios");
  if (!r.ok) throw new Error("Falha ao listar os relatórios carimbados");
  const rels: api.RelatorioResumo[] = await r.json();
  // Sem data conhecida nunca vira "o mais recente" por acaso.
  const instante = (c: { em: string | null }) => {
    const t = c.em ? Date.parse(c.em) : NaN;
    return Number.isFinite(t) ? t : -Infinity;
  };
  const out: Record<string, Carimbo> = {};
  for (const rel of rels) {
    const nome = String(rel.pagina ?? "").trim();
    if (!nome) continue;
    const novo: Carimbo = {
      id: String(rel.id),
      versao: rel.versao,
      em: rel.criado_em,
      por: rel.criado_por,
    };
    const atual = out[nome];
    if (!atual || instante(novo) > instante(atual)) out[nome] = novo;
  }
  return out;
}

// ---- Exibição da lista (pt-BR / America/Sao_Paulo) ----
const NF = new Intl.NumberFormat("pt-BR");
function plural(n: number, um: string, muitos: string): string {
  return `${NF.format(n)} ${n === 1 ? um : muitos}`;
}

function Fraco({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <span style={{ color: "var(--ink-3)" }} title={title}>
      {children}
    </span>
  );
}

// Célula que respeita as três fases. "lendo…" ≠ "não lido" (falhou) ≠ o valor.
function celFase<T>(f: Fase<T> | undefined, render: (dado: T) => ReactNode): ReactNode {
  if (!f || f.fase === "carregando") return <Fraco title="lendo a definição">lendo…</Fraco>;
  if (f.fase === "erro") return <Fraco title="não foi possível ler esta informação">não lido</Fraco>;
  return render(f.dado);
}

// Dia + hora em America/Sao_Paulo (o fuso aparece uma vez, no cabeçalho da coluna).
function quando(iso: string | null): string {
  const dia = diaSP(iso);
  return dia ? `${rotuloDia(dia)} ${fmtHora(iso)}` : "sem data registrada";
}

// Lista curta com "+N": nome de view e rótulo de filtro são longos e a coluna é estreita; o
// conjunto inteiro fica no `title`.
function resumirLista(itens: string[], max = 3): string {
  return itens.slice(0, max).join(", ") + (itens.length > max ? ` +${itens.length - max}` : "");
}

// Rascunho local resolvido. `morto` = 404/403 (some da memória do navegador); sem `nome` = leitura
// falhou por outro motivo e a linha só não aparece NESTA carga.
interface RascunhoLido {
  id: string;
  morto: boolean;
  nome?: string;
  definicao?: unknown;
}

// ============================================================================
// LISTA de páginas
// ============================================================================
export default function Pagina() {
  const token = useSelector((s: RootState) => s.studio.token);
  // A página aberta vive na URL (`/pagina?abrir=<id>`): é linkável e o voltar do navegador
  // funciona — o mesmo padrão já usado por Pipeline e Execuções.
  const [paramsUrl, setParamsUrl] = useSearchParams();
  const aberto = paramsUrl.get("abrir");

  const irPara = useCallback(
    (id: string | null) => {
      const p = new URLSearchParams(paramsUrl);
      if (id) p.set("abrir", id);
      else p.delete("abrir");
      setParamsUrl(p);
    },
    [paramsUrl, setParamsUrl],
  );

  // ----- Listagem -----
  const [listaRaw, setListaRaw] = useState<api.CatalogoItem[]>([]);
  const [rascunhos, setRascunhos] = useState<{ id: string; nome: string }[]>([]);
  const [ehAdmin, setEhAdmin] = useState(false);
  const [sistemaBusy, setSistemaBusy] = useState<string | null>(null);
  const [carregandoLista, setCarregandoLista] = useState(true);
  const [erroLista, setErroLista] = useState<string | null>(null);

  // Enriquecimento (ver bloco acima). `serieEnriq` = "última carga vence": recarregar duas vezes
  // rápido não pode deixar na tela o resultado da carga anterior.
  const [estado, setEstado] = useState<Record<string, Fase<EstadoPagina>>>({});
  const [nomeView, setNomeView] = useState<Fase<Record<string, string>>>({ fase: "carregando" });
  const [carimbos, setCarimbos] = useState<Fase<Record<string, Carimbo>>>({ fase: "carregando" });
  const serieEnriq = useRef(0);

  const enriquecer = useCallback(async (alvos: { id: string; definicao?: unknown }[]) => {
    const meu = ++serieEnriq.current;
    setEstado(
      Object.fromEntries(alvos.map((a) => [a.id, { fase: "carregando" } as Fase<EstadoPagina>])),
    );
    setNomeView({ fase: "carregando" });
    setCarimbos({ fase: "carregando" });

    // Nomes das views: UMA chamada resolve a referência de todas as linhas (o bloco guarda o id).
    // Enquanto ela não volta, nenhuma view pode ser chamada de "fora da sua lista" — seria uma
    // acusação de quebra baseada num mapa vazio.
    void api
      .listarViews()
      .then((vs) => {
        if (meu === serieEnriq.current)
          setNomeView({ fase: "ok", dado: Object.fromEntries(vs.map((v) => [v.id, v.nome])) });
      })
      .catch(() => {
        if (meu === serieEnriq.current) setNomeView({ fase: "erro" });
      });

    // Carimbos: tudo-ou-nada de propósito. Varredura parcial faria uma página que JÁ gerou
    // relatório aparecer como "nenhum" — numa tela de evidência isso é pior que "não lido".
    void ultimoCarimboPorNome()
      .then((dado) => {
        if (meu === serieEnriq.current) setCarimbos({ fase: "ok", dado });
      })
      .catch(() => {
        if (meu === serieEnriq.current) setCarimbos({ fase: "erro" });
      });

    await Promise.allSettled(
      alvos.map(async (a) => {
        try {
          const dado = await lerEstadoPagina(a.id, a.definicao);
          if (meu === serieEnriq.current) setEstado((s) => ({ ...s, [a.id]: { fase: "ok", dado } }));
        } catch {
          if (meu === serieEnriq.current) setEstado((s) => ({ ...s, [a.id]: { fase: "erro" } }));
        }
      }),
    );
  }, []);

  const recarregarLista = useCallback(async () => {
    setCarregandoLista(true);
    setErroLista(null);
    try {
      // Rascunhos lembrados neste navegador: some da lista local o que já foi publicado (está no
      // catálogo) ou o que não existe mais. Em PARALELO entre si e com o catálogo — em série, três
      // rascunhos seguravam a lista inteira, e esperar o catálogo para pedi-los custava mais uma ida
      // ao servidor. A leitura de um rascunho que o catálogo mostra publicado é descartada.
      const { publicadas, lidos } = await lerCatalogoComRascunhos(
        api.catalogo,
        lerRascunhos(),
        async (id): Promise<RascunhoLido> => {
          try {
            const r = await api.apiFetch("GET", `/paginas/${id}`);
            // Só 404/403 (definitivo) faz esquecer o rascunho. Não existe `GET /paginas`: se uma
            // falha passageira o apagasse daqui, ele ficaria inalcançável PARA SEMPRE.
            if (r.status === 404 || r.status === 403) return { id, morto: true };
            if (!r.ok) return { id, morto: false };
            const j = await r.json();
            return {
              id,
              morto: false,
              nome: String(j?.nome || "Página sem nome"),
              definicao: j?.definicao ?? {},
            };
          } catch {
            return { id, morto: false };
          }
        },
        setListaRaw,
      );
      gravarRascunhos(lidos.filter((x) => !x.morto).map((x) => x.id));
      const resolvidos = lidos.flatMap((x) =>
        x.nome !== undefined ? [{ id: x.id, nome: x.nome, definicao: x.definicao }] : [],
      );
      setRascunhos(resolvidos.map(({ id, nome }) => ({ id, nome })));
      // Enriquece TUDO do catálogo: a filtragem de páginas de sistema depende de `ehAdmin`, que
      // chega por outra rota — esperar por ele só atrasaria as linhas que já estão na tela.
      void enriquecer([
        ...resolvidos.map((r) => ({ id: r.id, definicao: r.definicao })),
        ...publicadas.map((p) => ({ id: p.id })),
      ]);
    } catch (e: any) {
      setErroLista(e?.message ?? "Falha ao carregar páginas.");
    } finally {
      setCarregandoLista(false);
    }
  }, [enriquecer]);

  useEffect(() => {
    if (aberto === null) void recarregarLista();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [aberto]);

  useEffect(() => {
    void api.meAtual().then((u) => setEhAdmin(u.perfil === "admin")).catch(() => setEhAdmin(false));
  }, []);

  const alternarSistema = async (id: string, sistema: boolean) => {
    setErroLista(null);
    setSistemaBusy(id);
    setListaRaw((l) => l.map((p) => (p.id === id ? { ...p, sistema } : p)));
    try {
      await api.definirSistema("paginas", id, sistema);
    } catch (e: any) {
      setListaRaw((l) => l.map((p) => (p.id === id ? { ...p, sistema: !sistema } : p)));
      setErroLista(e?.message ?? "Falha ao alterar a marca de sistema.");
    } finally {
      setSistemaBusy(null);
    }
  };

  // Não-admin nunca abre página de sistema no Studio (o backend dá 403); some da lista aqui também.
  const publicadas = useMemo(
    () => listaRaw.filter((p) => ehAdmin || p.sistema !== true),
    [listaRaw, ehAdmin],
  );

  const sistemaPorId = useMemo(
    () => new Map(listaRaw.map((p) => [p.id, p.sistema === true])),
    [listaRaw],
  );

  // Nome repetido entre páginas ⇒ o carimbo não pode ser atribuído (o resumo do relatório traz só
  // o NOME). Conta rascunho e publicada juntos: são páginas diferentes com o mesmo rótulo.
  const nomesAmbiguos = useMemo(() => {
    const c = new Map<string, number>();
    for (const n of [...rascunhos.map((r) => r.nome), ...publicadas.map((p) => p.nome)]) {
      const k = n.trim();
      c.set(k, (c.get(k) ?? 0) + 1);
    }
    return new Set([...c.entries()].filter(([, n]) => n > 1).map(([k]) => k));
  }, [rascunhos, publicadas]);

  const itens: ItemCadastro[] = useMemo(() => {
    const linhaSistema = (id: string) =>
      [campoSistema(ehAdmin, id, sistemaPorId.get(id) === true)].filter(
        (x): x is NonNullable<typeof x> => x !== null,
      );

    // Sub-linha do nome = as VIEWS que a página referencia. É o que define uma página (o bloco não
    // calcula: ele aponta para uma view) e era justamente o que a lista não dizia.
    const metaDe = (id: string, rascunhoLocal: boolean): string => {
      const f = estado[id];
      if (!f || f.fase === "carregando") return "lendo os blocos…";
      if (f.fase === "erro") return "não foi possível ler a definição desta página";
      const d = f.dado;
      if (d.blocos === 0)
        return rascunhoLocal
          ? "nenhum bloco ainda — arraste uma view publicada para o quadro e publique"
          : "nenhum bloco — a página não mostra nada";
      const cab = plural(d.blocos, "bloco", "blocos");
      if (nomeView.fase !== "ok")
        return `${cab} · ${nomeView.fase === "erro" ? "não foi possível ler os nomes das views" : "lendo os nomes das views…"}`;
      const conhecidas = d.vids.map((v) => nomeView.dado[v]).filter((n): n is string => !!n);
      const fora = d.vids.length - conhecidas.length;
      return [
        cab,
        conhecidas.length ? resumirLista(conhecidas) : "",
        // View apagada — ou de sistema, invisível para este perfil: nos dois casos a página
        // referencia algo que não está na lista de quem vai editá-la.
        fora ? `${plural(fora, "view", "views")} fora da sua lista` : "",
      ]
        .filter(Boolean)
        .join(" · ");
    };

    // A coluna que substitui a antiga "Execução" (dizia "parametrizada" em todas as linhas). Aqui
    // ela varia: é o CONTRATO com o operador — o que ele preenche antes de gerar o relatório.
    const campoOperador = (id: string): CampoItem => ({
      rotulo: "O operador informa",
      valor: celFase(estado[id], (d) => {
        const tudo = [...d.operador, ...d.liberados];
        if (tudo.length === 0)
          return (
            <Fraco
              title={
                d.parametrizada
                  ? "a página roda o pipeline sem pedir nada: o recorte já está fixo na definição"
                  : "o operador abre e vê o relatório inteiro; não há nada para informar"
              }
            >
              {d.parametrizada ? "nada (roda o pipeline)" : "nada"}
            </Fraco>
          );
        return (
          <span
            title={[
              d.operador.length
                ? `parâmetro da execução (roda o pipeline): ${d.operador.join(", ")}`
                : "",
              d.liberados.length
                ? `filtro liberado da view (recorta o resultado): ${d.liberados.join(", ")}`
                : "",
            ]
              .filter(Boolean)
              .join(" · ")}
          >
            {resumirLista(tudo)}
          </span>
        );
      }),
    });

    const campoPublicada = (id: string): CampoItem => ({
      rotulo: "Publicada (GMT-3)",
      valor: celFase(estado[id], (d) =>
        d.publicada ? (
          // nowrap: "v8 ·" numa linha e "25/06/2026 12:13" na outra não se lê como um momento.
          <span style={{ whiteSpace: "nowrap" }} title="versão que o Portal enxerga">
            v{d.publicada.numero} · {quando(d.publicada.em)}
          </span>
        ) : (
          <Fraco title="só a versão publicada entra no catálogo do Portal">nunca</Fraco>
        ),
      ),
    });

    // Sem `id`: o resumo do relatório não traz o id da página, a atribuição é toda por nome.
    const campoCarimbo = (nome: string): CampoItem => ({
      rotulo: "Último relatório",
      valor: celFase(carimbos, (mapa) => {
        const chave = nome.trim();
        if (nomesAmbiguos.has(chave))
          return (
            <Fraco title="há mais de uma página com este nome e o carimbo guarda só o NOME — não dá para dizer de qual delas o relatório saiu">
              nome repetido
            </Fraco>
          );
        const c = mapa[chave];
        if (!c)
          return (
            <Fraco title="nenhum relatório carimbado com este nome de página; o carimbo guarda o nome da época, então renomear a página zera esta coluna">
              nenhum
            </Fraco>
          );
        return (
          <span
            style={{ whiteSpace: "nowrap" }}
            title={`relatório ${c.id.slice(0, 8)} · página v${c.versao}${c.por ? ` · por ${c.por}` : ""} — atribuído pelo nome gravado no carimbo`}
          >
            v{c.versao} · {quando(c.em)}
          </span>
        );
      }),
    });

    const campos = (id: string, nome: string) => [
      campoOperador(id),
      campoPublicada(id),
      campoCarimbo(nome),
      ...linhaSistema(id),
    ];

    // Rascunhos primeiro: é o trabalho em aberto, e é o que a lista antes escondia.
    // O limite mora NA LINHA, não numa nota no topo: não existe "GET /paginas", então o rascunho
    // só é alcançável por este navegador (`CHAVE_RASCUNHOS`). Quem trocar de máquina não o
    // encontra — e uma nota geral acima da tabela não diz QUAL linha corre esse risco.
    const deRascunho: ItemCadastro[] = rascunhos.map((p) => ({
      id: p.id,
      nome: p.nome,
      meta: metaDe(p.id, true),
      status: "rascunho (só neste navegador)",
      scls: "skip",
      campos: campos(p.id, p.nome),
    }));
    const dePublicadas: ItemCadastro[] = publicadas.map((p) => {
      const f = estado[p.id];
      const d = f?.fase === "ok" ? f.dado : null;
      // O pill diz o estado de PUBLICAÇÃO; a coluna diz desde quando. "rascunho à frente" é o caso
      // que enganava: a lista descreve a última definição salva, e o Portal ainda vê a anterior.
      const pill = !d
        ? { label: f?.fase === "erro" ? "não lido" : "lendo…", cls: "off" }
        : d.rascunhoAdiante
          ? { label: `rascunho v${d.rascunhoAdiante} à frente`, cls: "skip" }
          : d.publicada
            ? { label: "publicada", cls: "ok" }
            : { label: "sem versão publicada", cls: "off" };
      return {
        id: p.id,
        nome: p.nome,
        meta: metaDe(p.id, false),
        status: pill.label,
        scls: pill.cls,
        campos: campos(p.id, p.nome),
      };
    });
    return [...deRascunho, ...dePublicadas];
  }, [
    publicadas,
    rascunhos,
    ehAdmin,
    sistemaBusy,
    sistemaPorId,
    estado,
    nomeView,
    carimbos,
    nomesAmbiguos,
  ]);

  const novaPagina = async () => {
    setErroLista(null);
    try {
      const r = await reqJson(token, "POST", "/paginas", { nome: "Nova página" });
      const id = String(r.id);
      gravarRascunhos([...lerRascunhos().filter((x) => x !== id), id]);
      irPara(id);
    } catch (e: any) {
      setErroLista(e?.message ?? "Falha ao criar página.");
    }
  };

  const excluirPagina = async (id: string) => {
    setErroLista(null);
    try {
      await api.excluirPagina(id);
      gravarRascunhos(lerRascunhos().filter((x) => x !== id));
      await recarregarLista();
    } catch (e: any) {
      setErroLista(e?.message ?? "Falha ao excluir página.");
    }
  };

  const renomearPagina = async (id: string, nome: string) => {
    setErroLista(null);
    try {
      await api.renomearPagina(id, nome);
      await recarregarLista();
    } catch (e: any) {
      setErroLista(e?.message ?? "Falha ao renomear página.");
    }
  };

  if (aberto !== null) {
    // `key`: trocar de página remonta o builder (estado do quadro é por página).
    return <Builder key={aberto} paginaId={aberto} token={token} onVoltar={() => irPara(null)} />;
  }

  const contagem =
    itens.length === 0
      ? "nenhuma página"
      : plural(itens.length, "página", "páginas") +
        (rascunhos.length ? ` · ${rascunhos.length} em rascunho` : "");

  return (
    <>
      <div className="page-head">
        <h1 className="display">Páginas</h1>
        <p className="sub">
          A página só <b>referencia</b> o modelo: cada bloco é uma view publicada, e todo número
          exibido nasce de uma medida do cubo. Publicar coloca a página no catálogo do Portal.
        </p>
      </div>
      <div className="pad">
        {erroLista && (
          <div className="exec-banner-falha" role="alert" style={{ marginBottom: 14 }}>
            <span className="dot" />
            <span className="exec-banner-txt">{erroLista}</span>
            <button type="button" className="pag-btn" onClick={() => void recarregarLista()}>
              Tentar de novo
            </button>
          </div>
        )}
        <div className="lc-bar">
          <span data-testid="paginas-contagem">{carregandoLista ? "carregando…" : contagem}</span>
          <button className="btn-stamp" style={{ marginLeft: "auto" }} onClick={() => void novaPagina()}>
            + Nova página
          </button>
        </div>
        <GridLista
              erro={!!erroLista}
          rotulo="Páginas"
          carregando={carregandoLista}
          itens={itens}
          onAbrir={(id) => irPara(id)}
          onExcluir={(id) => void excluirPagina(id)}
          onRenomear={(id, nome) => void renomearPagina(id, nome)}
          onSistema={ehAdmin ? (id, s) => void alternarSistema(id, s) : undefined}
          ehSistema={(it) => sistemaPorId.get(it.id) === true}
          vazio={
            <div className="card lc-empty">
              <div className="big">Nenhuma página ainda</div>
              <div>
                Clique <b>+ Nova página</b>, arraste views publicadas para o quadro e publique.
              </div>
            </div>
          }
        />
      </div>
    </>
  );
}

// ============================================================================
// BUILDER — quadro livre (componente isolado p/ ter useRef/listeners limpos).
// ============================================================================
interface BuilderProps {
  paginaId: string;
  token: string | null;
  onVoltar: () => void;
}

interface DragState {
  k: number;
  mode: "move" | "resize";
  sx: number;
  sy: number;
  ox: number;
  oy: number;
  ow: number;
  oh: number;
}

const MIN_W = 170;
const MIN_H = 110;

function Builder({ paginaId, token, onVoltar }: BuilderProps) {
  const navigate = useNavigate();
  const [blocos, setBlocos] = useState<BlocoEstado[]>([]);
  const [sel, setSel] = useState<number | null>(null);
  const [over, setOver] = useState(false);
  const [modo, setModo] = useState<"editar" | "ver">("editar");
  const [nome, setNome] = useState("Nova página");
  const [filt, setFilt] = useState<Record<string, boolean>>({});
  const [pub, setPub] = useState(false);
  const [guides, setGuides] = useState<{ a: "x" | "y"; p: number }[]>([]);

  const [baseNumero, setBaseNumero] = useState(0);
  // Aba do painel de config ao lado do canvas (agrupa as opções por tema).
  const [abaCfg, setAbaCfg] = useState<"pagina" | "filtros" | "execucao">("pagina");
  const [paleta, setPaleta] = useState<ItemPaleta[]>([]);
  const [buscaPaleta, setBuscaPaleta] = useState("");
  const [dadosBloco, setDadosBloco] = useState<Record<string, DadoView>>({});
  const [carregando, setCarregando] = useState(true);
  const [erro, setErro] = useState<string | null>(null);
  const [salvando, setSalvando] = useState(false);
  const [publicando, setPublicando] = useState(false);
  const [pedirDescarte, setPedirDescarte] = useState<null | (() => void)>(null);
  // Estado "o que está na tela é igual ao que está salvo" (detalhado mais abaixo, em `assinatura`).
  const [assinaturaSalva, setAssinaturaSalva] = useState<string | null>(null);
  const [marcarSalvo, setMarcarSalvo] = useState(false);

  // --- Execução (página parametrizada: definicao.execucao) ---
  // Modelo SIMPLES: escolher QUAIS params do pipeline o operador preenche (+ rótulo). Tipo/opções/
  // derivação vêm do pipeline. `execAtivo` controla o toggle; off → execucao removida da definição.
  const [execAtivo, setExecAtivo] = useState(false);
  const [execPipeline, setExecPipeline] = useState("");
  const [execParams, setExecParams] = useState<ExecParam[]>([]); // params escolhidos (na ordem)
  const [pipelineParams, setPipelineParams] = useState<ParametroPipeline[]>([]); // declarados no pipeline
  const [pipelinesDisp, setPipelinesDisp] = useState<PipelineResumo[]>([]);

  const ver = modo === "ver";

  // Versões (dropdown). `versaoSel` = versão CARREGADA (null = última). baseNumero = nº máximo
  // (lock) — editar/publicar uma versão anterior gera uma NOVA (a anterior fica intacta).
  const [versoes, setVersoes] = useState<api.VersaoResumo[]>([]);
  const [versaoSel, setVersaoSel] = useState<number | null>(null);
  const ultima = versoes.length ? versoes[0].numero : null;

  const carregarVersoes = useCallback(() => {
    listarVersoesPagina(token, paginaId).then(setVersoes).catch(() => setVersoes([]));
  }, [token, paginaId]);

  // Aplica o detalhe (última ou versão específica) ao estado do builder.
  const aplicarPagina = useCallback((det: DetalhePagina) => {
    setNome(det.nome ?? "Nova página");
    setBaseNumero(det.ultimo ?? det.numero ?? 0); // lock = máximo
    const def = det.definicao ?? {};
    const blocosDef: any[] = def.blocos ?? [];
    let n = 1;
    setBlocos(
      blocosDef.map((b) => ({
        k: n++,
        vid: b.view_id ?? b.vid,
        tipo: (b.tipo as Tipo) ?? "pivot",
        total: !!b.total,
        x: b.x ?? 22,
        y: b.y ?? 18,
        w: b.w ?? 300,
        h: b.h ?? 206,
        titulo: b.titulo,
      })),
    );
    const liberados: string[] = def.filtros_liberados ?? def.filtros_pagina ?? [];
    setFilt(Object.fromEntries(liberados.map((d) => [d, true])));
    const exec = def.execucao as ExecConfig | undefined;
    setExecAtivo(!!exec);
    setExecPipeline(exec?.pipeline_id ?? "");
    setExecParams(exec?.operador ?? []);
    setSel(null);
    setPub(false);
  }, []);

  // Última chamada vence (mesma régua do Pipeline e da View): abrir o construtor dispara mais de uma
  // carga do detalhe (o StrictMode dobra o efeito de montagem no dev; trocar de versão depressa faz
  // o mesmo em produção). A primeira a chegar mostrava o formulário e a outra, chegando depois,
  // reescrevia o nome digitado com "Nova página" e esvaziava o quadro — a página saía publicada com
  // o nome default e não aparecia no Portal pelo nome dado. Só a carga mais recente aplica.
  const serieCarga = useRef(0);
  const carregar = useCallback(
    async (numero?: number) => {
      const minha = ++serieCarga.current;
      setCarregando(true);
      setErro(null);
      setAssinaturaSalva(null); // nada a comparar enquanto a definição não chegou
      try {
        const det = await obterPagina(token, paginaId, numero);
        if (minha !== serieCarga.current) return;
        aplicarPagina(det);
        setMarcarSalvo(true);
      } catch (e: any) {
        if (minha !== serieCarga.current) return;
        setErro(e?.message ?? "Falha ao abrir a página.");
      } finally {
        if (minha === serieCarga.current) setCarregando(false);
      }
    },
    [token, paginaId, aplicarPagina],
  );

  // --- Carregamento inicial: detalhe (via carregar) + paleta (views) + listas + versões ---
  useEffect(() => {
    let vivo = true;
    (async () => {
      try {
        // Paleta (views publicadas — a página nunca calcula, ; todo número vem de uma view)
        // e pipelines da execução parametrizada: pedidas juntas, aplicadas na ordem de antes.
        await carregarListasAuxiliares(
          { views: api.listarViews, pipelines: api.listarPipelines },
          {
            views: (views) => {
              if (vivo)
                setPaleta(
                  views.map((v, i) => ({ id: v.id, nome: v.nome, cor: PALETA_CORES[i % PALETA_CORES.length] || COR_VIEW })),
                );
            },
            pipelines: (pipes) => {
              if (vivo) setPipelinesDisp(pipes);
            },
          },
        );
      } catch {
        /* listas auxiliares: falha não bloqueia o builder */
      }
    })();
    carregarVersoes();
    void carregar();
    return () => {
      vivo = false;
    };
  }, [carregar, carregarVersoes]);

  // ----- Alterações não salvas -----
  // Assinatura do que é PERSISTIDO. Sem isto, "← Páginas" e trocar de versão descartavam o
  // trabalho em silêncio (o quadro inteiro).
  const assinatura = useMemo(
    () =>
      JSON.stringify({
        nome,
        blocos: blocos.map((b) => [b.vid, b.tipo, !!b.total, Math.round(b.x), Math.round(b.y), Math.round(b.w), Math.round(b.h), b.titulo ?? null]),
        filtros: Object.keys(filt).filter((k) => filt[k]).sort(),
        execAtivo,
        execPipeline,
        execParams,
      }),
    [nome, blocos, filt, execAtivo, execPipeline, execParams],
  );
  const assinaturaRef = useRef(assinatura);
  assinaturaRef.current = assinatura;
  useEffect(() => {
    // Roda DEPOIS de aplicarPagina: a assinatura já reflete o estado recém-carregado.
    if (!marcarSalvo) return;
    setAssinaturaSalva(assinatura);
    setMarcarSalvo(false);
  }, [marcarSalvo, assinatura]);
  const sujo = assinaturaSalva !== null && assinatura !== assinaturaSalva;

  // Aviso do navegador ao fechar a aba com trabalho não salvo.
  useEffect(() => {
    if (!sujo) return;
    const aviso = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", aviso);
    return () => window.removeEventListener("beforeunload", aviso);
  }, [sujo]);

  const comGuarda = (acao: () => void) => {
    if (sujo) setPedirDescarte(() => acao);
    else acao();
  };

  const selecionarVersao = (numero: number | null) =>
    comGuarda(() => {
      setVersaoSel(numero);
      setErro(null);
      void carregar(numero ?? undefined);
    });

  // --- Dado dos blocos: carrega SEMPRE (editar e pré-visualizar) ---
  // O especialista precisa ver o que o bloco mostra enquanto monta; e o erro da consulta
  // (ex.: "a fonte não tem execução concluída") tem de aparecer, não virar "sem dados".
  const buscandoRef = useRef<Set<string>>(new Set());
  const carregarView = useCallback(
    (vid: string) => {
      if (buscandoRef.current.has(vid)) return;
      buscandoRef.current.add(vid);
      setDadosBloco((d) => ({ ...d, [vid]: { estado: "carregando" } }));
      consultarView(token, vid)
        .then((res) => setDadosBloco((d) => ({ ...d, [vid]: { estado: "ok", res } })))
        .catch((e: any) =>
          setDadosBloco((d) => ({
            ...d,
            [vid]: { estado: "erro", mensagem: e?.message ?? "Falha ao consultar a view." },
          })),
        )
        .finally(() => buscandoRef.current.delete(vid));
    },
    [token],
  );

  const vidsDosBlocos = useMemo(() => [...new Set(blocos.map((b) => b.vid))].join(","), [blocos]);
  useEffect(() => {
    for (const vid of vidsDosBlocos ? vidsDosBlocos.split(",") : []) {
      if (!dadosBloco[vid]) carregarView(vid);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [vidsDosBlocos, carregarView]);

  // --- Definição publicada das views do quadro ---
  // Serve a DUAS perguntas: quais dimensões a view LIBERA (aba Filtros) e quais filtros ela
  // APLICA — que é a resposta para "por que este bloco veio vazio". Buscada sob demanda: quando a
  // aba Filtros abre ou quando algum bloco volta com zero linha.
  const [defsView, setDefsView] = useState<Record<string, any>>({});
  const [carregandoDefs, setCarregandoDefs] = useState(false);
  const defsPedidasRef = useRef<Set<string>>(new Set());
  const carregarDefsView = useCallback(
    (vids: string[]) => {
      const faltando = [...new Set(vids)].filter((v) => v && !defsPedidasRef.current.has(v));
      if (faltando.length === 0) return;
      faltando.forEach((v) => defsPedidasRef.current.add(v));
      setCarregandoDefs(true);
      Promise.all(
        faltando.map(async (vid) => {
          try {
            return [vid, (await obterViewDef(token, vid))?.definicao ?? {}] as const;
          } catch {
            // `_erroDef` distingue "a view não filtra nada" de "não consegui ler a definição": sem
            // essa marca, uma falha de rede viraria a afirmação de que não há filtro — e o bloco
            // vazio mandaria o especialista procurar no lugar errado.
            return [vid, { _erroDef: true }] as const;
          }
        }),
      )
        .then((pares) => setDefsView((d) => ({ ...d, ...Object.fromEntries(pares) })))
        .finally(() => setCarregandoDefs(false));
    },
    [token],
  );

  useEffect(() => {
    if (abaCfg !== "filtros") return;
    carregarDefsView(blocos.map((b) => b.vid));
  }, [abaCfg, blocos, carregarDefsView]);

  useEffect(() => {
    const vazios = blocos
      .filter((b) => {
        const d = dadosBloco[b.vid];
        return d?.estado === "ok" && (d.res.linhas ?? []).length === 0;
      })
      .map((b) => b.vid);
    if (vazios.length) carregarDefsView(vazios);
  }, [blocos, dadosBloco, carregarDefsView]);

  const dimsCandidatas = useMemo(() => {
    const s = new Set<string>();
    for (const b of blocos) {
      const libs = defsView[b.vid]?.filtros_liberados;
      if (Array.isArray(libs)) for (const d of libs as string[]) s.add(d);
    }
    return [...s].sort((a, b) => a.localeCompare(b, "pt-BR"));
  }, [blocos, defsView]);
  // Dimensão salva na página que NENHUMA view libera: o Portal recusa esse filtro (403).
  const dimsOrfas = useMemo(
    () => Object.keys(filt).filter((d) => filt[d] && !dimsCandidatas.includes(d)),
    [filt, dimsCandidatas],
  );

  // --- Arraste/redimensionamento (porta exata da lógica do protótipo) ---
  const drag = useRef<DragState | null>(null);
  const blocosRef = useRef<BlocoEstado[]>(blocos);
  blocosRef.current = blocos;

  useEffect(() => {
    const TH = 6;
    const mv = (e: MouseEvent) => {
      const dr = drag.current;
      if (!dr) return;
      const dx = e.clientX - dr.sx;
      const dy = e.clientY - dr.sy;
      const bs = blocosRef.current;
      const cur = bs.find((b) => b.k === dr.k);
      if (!cur) return;
      if (dr.mode === "resize") {
        setBlocos((p) =>
          p.map((b) => (b.k === dr.k ? { ...b, w: Math.max(MIN_W, dr.ow + dx), h: Math.max(MIN_H, dr.oh + dy) } : b)),
        );
        return;
      }
      const rawx = Math.max(0, dr.ox + dx);
      const rawy = Math.max(0, dr.oy + dy);
      let nx = rawx;
      let ny = rawy;
      const w = cur.w;
      const h = cur.h;
      const others = bs.filter((b) => b.k !== dr.k);
      let gx: number | null = null;
      let gy: number | null = null;
      let bdx = TH + 1;
      let bdy = TH + 1;
      others.forEach((o) => {
        const oxs = [o.x, o.x + o.w / 2, o.x + o.w];
        const oys = [o.y, o.y + o.h / 2, o.y + o.h];
        [0, w / 2, w].forEach((off) =>
          oxs.forEach((ox) => {
            const c = ox - off;
            const d = Math.abs(c - rawx);
            if (d < bdx) {
              bdx = d;
              nx = c;
              gx = ox;
            }
          }),
        );
        [0, h / 2, h].forEach((off) =>
          oys.forEach((oy) => {
            const c = oy - off;
            const d = Math.abs(c - rawy);
            if (d < bdy) {
              bdy = d;
              ny = c;
              gy = oy;
            }
          }),
        );
      });
      setGuides([
        ...(gx != null ? [{ a: "x" as const, p: gx }] : []),
        ...(gy != null ? [{ a: "y" as const, p: gy }] : []),
      ]);
      setBlocos((p) => p.map((b) => (b.k === dr.k ? { ...b, x: nx, y: ny } : b)));
    };
    const up = () => {
      drag.current = null;
      setGuides([]);
    };
    window.addEventListener("mousemove", mv);
    window.addEventListener("mouseup", up);
    return () => {
      window.removeEventListener("mousemove", mv);
      window.removeEventListener("mouseup", up);
    };
  }, []);

  const down = (e: React.MouseEvent, b: BlocoEstado, mode: "move" | "resize") => {
    drag.current = { k: b.k, mode, sx: e.clientX, sy: e.clientY, ox: b.x, oy: b.y, ow: b.w, oh: b.h };
    setSel(b.k);
    e.preventDefault();
  };

  const addAt = (vid: string, x: number, y: number, tipo: Tipo = "pivot") => {
    const kpi = tipo === "valor";
    setBlocos((bs) => {
      // `k` derivado do máximo atual: com `seq` de fora do updater, dois cliques no mesmo tique
      // geravam blocos com a MESMA chave (React quebrava a seleção e o remover).
      const k = bs.reduce((m, b) => Math.max(m, b.k), 0) + 1;
      setSel(k);
      return [
        ...bs,
        { k, vid, tipo, x: Math.max(0, x), y: Math.max(0, y), w: kpi ? 208 : 300, h: kpi ? 128 : 200 },
      ];
    });
    setPub(false);
  };

  // Posição em cascata (3 colunas) para o botão "+ adicionar": antes todo bloco novo caía em
  // 40,40 e ficava escondido debaixo do anterior.
  const proximaPosicao = (kpi: boolean) => {
    const n = blocos.length;
    const col = n % 3;
    const row = Math.floor(n / 3);
    return { x: 22 + col * (kpi ? 226 : 318), y: 18 + row * (kpi ? 148 : 218) };
  };

  const del = (k: number) => {
    setBlocos((bs) => bs.filter((b) => b.k !== k));
    setSel((s) => (s === k ? null : s));
    setPub(false);
  };
  const setTipo = (k: number, t: Tipo) =>
    setBlocos((bs) =>
      bs.map((b) =>
        b.k === k
          ? { ...b, tipo: t, ...(t === "valor" ? { w: Math.max(MIN_W, Math.min(b.w, 240)), h: Math.max(MIN_H, Math.min(b.h, 140)) } : {}) }
          : b,
      ),
    );
  const setTit = (k: number, t: string) => setBlocos((bs) => bs.map((b) => (b.k === k ? { ...b, titulo: t } : b)));
  const setTotal = (k: number, v: boolean) => setBlocos((bs) => bs.map((b) => (b.k === k ? { ...b, total: v } : b)));
  const mover = (k: number, dx: number, dy: number) =>
    setBlocos((bs) => bs.map((b) => (b.k === k ? { ...b, x: Math.max(0, b.x + dx), y: Math.max(0, b.y + dy) } : b)));
  const redimensionar = (k: number, dw: number, dh: number) =>
    setBlocos((bs) =>
      bs.map((b) => (b.k === k ? { ...b, w: Math.max(MIN_W, b.w + dw), h: Math.max(MIN_H, b.h + dh) } : b)),
    );

  const itemDe = (vid: string): ItemPaleta | undefined => paleta.find((p) => p.id === vid);
  const nomeDaView = (vid: string) => itemDe(vid)?.nome ?? `view ${vid.slice(0, 8)}`;
  const tituloDe = (b: BlocoEstado) => (b.titulo !== undefined ? b.titulo : itemDe(b.vid)?.nome ?? "");
  const blocoSel = blocos.find((b) => b.k === sel) ?? null;

  // Execuções que alimentaram a prévia (provenance): a prévia lê o snapshot da ÚLTIMA execução
  // concluída de cada pipeline-fonte — e a tela precisa dizer isso.
  const origens = useMemo(() => {
    const m = new Map<string, string>(); // execucao_id -> no_id
    for (const b of blocos) {
      const d = dadosBloco[b.vid];
      const o = d?.estado === "ok" ? d.res.origem : undefined;
      if (o?.execucao_id) m.set(o.execucao_id, o.no_id ?? "");
    }
    return [...m.entries()];
  }, [blocos, dadosBloco]);

  // --- Execução: carrega os parâmetros DECLARADOS do pipeline escolhido (para o operador escolher). ---
  useEffect(() => {
    if (!execAtivo || !execPipeline) {
      setPipelineParams([]);
      return;
    }
    let vivo = true;
    api
      .obterPipeline(execPipeline)
      .then((d) => vivo && setPipelineParams(d.definicao?.parametros ?? []))
      .catch(() => vivo && setPipelineParams([]));
    return () => {
      vivo = false;
    };
  }, [execAtivo, execPipeline]);

  // --- Mutações em memória da config de execução ---
  // Marca/desmarca um parâmetro como preenchido pelo operador (preserva o rótulo já definido).
  const alternarParam = (nomeParam: string) =>
    setExecParams((ps) =>
      ps.some((p) => p.param === nomeParam) ? ps.filter((p) => p.param !== nomeParam) : [...ps, { param: nomeParam }],
    );
  const definirRotulo = (nomeParam: string, rotulo: string) =>
    setExecParams((ps) => ps.map((p) => (p.param === nomeParam ? { ...p, rotulo } : p)));

  // --- Definição p/ persistir ---
  // TODO bloco (pivot/tabela/valor) carrega um view_id real, então todos passam no
  // validar_pagina do backend (a página não calcula) e sobrevivem ao publicar.
  const construirDefinicao = () => {
    const liberados = Object.keys(filt).filter((k) => filt[k]);
    const blocosDef = blocos.map((b) => ({
      titulo: b.titulo !== undefined ? b.titulo : itemDe(b.vid)?.nome,
      view_id: b.vid,
      tipo: b.tipo,
      total: !!b.total,
      x: Math.round(b.x),
      y: Math.round(b.y),
      w: Math.round(b.w),
      h: Math.round(b.h),
    }));
    const def: Record<string, unknown> = {
      blocos: blocosDef,
      filtros_pagina: liberados,
      filtros_liberados: liberados,
    };
    // Toggle ligado → grava o bloco execucao (página parametrizada). Desligado → omite-o.
    // Só persiste params que ainda existem no pipeline (ignora os removidos).
    if (execAtivo) {
      const nomes = new Set(pipelineParams.map((p) => p.nome));
      const execucao: ExecConfig = {
        pipeline_id: execPipeline,
        operador: execParams.filter((p) => nomes.size === 0 || nomes.has(p.param)),
      };
      def.execucao = execucao;
    }
    return def;
  };

  // Valida ANTES de chamar a API: erro de configuração aparece na hora, em pt-BR, sem gastar
  // uma ida ao servidor que voltaria com mensagem genérica.
  const problemas = useMemo(() => {
    const p: string[] = [];
    if (!nome.trim()) p.push("Dê um nome à página.");
    if (blocos.length === 0) p.push("Adicione ao menos um bloco de view ao quadro.");
    if (execAtivo && !execPipeline) p.push("Escolha o pipeline da página parametrizada (aba Execução) ou desligue a opção.");
    return p;
  }, [nome, blocos.length, execAtivo, execPipeline]);

  const salvarRascunho = async () => {
    const marca = assinaturaRef.current;
    setSalvando(true);
    setErro(null);
    try {
      const def = construirDefinicao();
      const r = await reqJson(token, "PUT", `/paginas/${paginaId}/rascunho`, { definicao: def, base_numero: baseNumero });
      const numero = r.numero ?? baseNumero + 1;
      setBaseNumero(numero);
      setAssinaturaSalva(marca);
      carregarVersoes();
      return numero;
    } catch (e: any) {
      setErro(e?.message ?? "Falha ao salvar rascunho.");
      throw e;
    } finally {
      setSalvando(false);
    }
  };

  const publicar = async () => {
    if (problemas.length) {
      setErro(problemas.join(" "));
      return;
    }
    const marca = assinaturaRef.current;
    setPublicando(true);
    setErro(null);
    try {
      // Salva o rascunho primeiro (garante que a definição corrente vai pro publicado).
      const def = construirDefinicao();
      const r = await reqJson(token, "PUT", `/paginas/${paginaId}/rascunho`, { definicao: def, base_numero: baseNumero });
      const numero = r.numero ?? baseNumero + 1;
      // O número da versão PUBLICADA vem da resposta (publicar cria `base+1`) — não é chute nosso.
      const pubR = await reqJson(token, "POST", `/paginas/${paginaId}/publicar`, {
        base_numero: numero,
        nota: "publicado pelo Studio",
        nome: nome.trim() || undefined,
      });
      setBaseNumero(pubR?.numero ?? numero + 1);
      setAssinaturaSalva(marca);
      setPub(true);
      setVersaoSel(null);
      carregarVersoes();
    } catch (e: any) {
      setErro(e?.message ?? "Falha ao publicar página.");
    } finally {
      setPublicando(false);
    }
  };

  if (carregando) {
    return (
      <>
        <div className="page-head">
          <button className="link-back" onClick={onVoltar}>
            ← Páginas
          </button>
          <h1 className="display">Abrindo a página…</h1>
        </div>
        <div className="pad">
          <div className="card exec-lista-vazia">Carregando definição…</div>
        </div>
      </>
    );
  }

  // Bloco que rodou e voltou sem linha. "A view não retornou linhas", repetido em três blocos, não
  // diz o que fazer a seguir. Aqui a tela mostra O QUE a view filtra — é isso que costuma zerar o
  // resultado — e oferece os dois passos que existem de verdade: consultar de novo e abrir a
  // execução que alimentou o snapshot. A página não executa pipeline, então não há
  // "rodar agora" honesto daqui; quem roda é a tela de Execuções, e é para lá que o link leva.
  const blocoVazio = (b: BlocoEstado, origem?: ResultadoView["origem"]) => {
    const def = defsView[b.vid];
    const filtros: any[] = Array.isArray(def?.filtros) ? def.filtros : [];
    const ex = origem?.execucao_id;
    return (
      <div
        className="pb-vazio"
        style={{ textAlign: "left", padding: "12px 13px", fontSize: 11.5, lineHeight: 1.5 }}
      >
        <b style={{ color: "var(--ink)" }}>Nenhuma linha</b> — a view «{nomeDaView(b.vid)}» consultou
        o snapshot e não achou nada. Não é erro: é resultado.
        {!def ? (
          <div style={{ marginTop: 5 }}>Lendo o que ela filtra…</div>
        ) : def._erroDef ? (
          <div style={{ marginTop: 5 }}>
            Não foi possível ler a definição da view — o filtro que ela aplica não pôde ser
            conferido aqui.
          </div>
        ) : filtros.length > 0 ? (
          <div style={{ marginTop: 5 }}>
            É por aqui que se começa — filtro fixo da view:{" "}
            {filtros.map((f, i) => (
              <span key={i}>
                {i > 0 && " · "}
                <b>{String(f?.dimensao ?? "?")}</b>{" "}
                {OP_FILTRO[String(f?.op ?? "igual")] ?? String(f?.op ?? "=")}{" "}
                {celulaDado(f?.valor).texto}
              </span>
            ))}
          </div>
        ) : (
          <div style={{ marginTop: 5 }}>
            A view não aplica filtro nenhum — então o vazio vem do snapshot da fonte, não de filtro.
          </div>
        )}
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginTop: 7 }}>
          <button
            type="button"
            className="lk"
            style={{ fontSize: 11.5 }}
            onClick={() => carregarView(b.vid)}
          >
            consultar de novo
          </button>
          {ex && (
            <button
              type="button"
              className="lk"
              style={{ fontSize: 11.5 }}
              title={`Abrir a execução ${ex} que produziu o snapshot`}
              onClick={() => navigate(`/execucoes/${ex}`)}
            >
              ver a execução {ex.slice(0, 8)}
            </button>
          )}
        </div>
      </div>
    );
  };

  // ---- corpo de um bloco: carregando ≠ erro ≠ vazio ≠ dado ----
  const corpoBloco = (b: BlocoEstado) => {
    const d = dadosBloco[b.vid];
    if (!d || d.estado === "carregando") return <p className="pb-vazio">Carregando a view…</p>;
    if (d.estado === "erro")
      return (
        <div className="no-preview-erro" role="note">
          {d.mensagem}{" "}
          <button type="button" className="lk" onClick={() => carregarView(b.vid)}>
            tentar de novo
          </button>
        </div>
      );
    const r = montarRender(d.res, b.total, nomeDaView(b.vid));
    if (b.tipo === "valor") return <Bloco view={r.view} tipo="valor" />;
    if (r.linhas === 0) return blocoVazio(b, d.res.origem);
    return <Bloco view={r.view} tipo={b.tipo} total={r.total} colsNum={r.colsNum} />;
  };

  const canvasInner = (
    <div
      className={"pcanvas" + (ver ? " ver" : "") + (over && !ver ? " over" : "")}
      // O fundo do quadro em edição é branco fixo no CSS (com pontos claros) e não tem override de
      // tema: no escuro ficava uma folha BRANCA com cartões escuros em cima. Aqui vem por token.
      style={
        ver
          ? undefined
          : {
              background:
                "radial-gradient(circle at 1px 1px, var(--hair) 1px, transparent 0) 0 0/22px 22px, var(--surface)",
            }
      }
      onDragOver={(e) => {
        if (ver) return;
        e.preventDefault();
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        if (ver) return;
        e.preventDefault();
        setOver(false);
        const vid = e.dataTransfer.getData("v");
        const tipo = (e.dataTransfer.getData("t") as Tipo) || "pivot";
        const r = e.currentTarget.getBoundingClientRect();
        if (vid) addAt(vid, e.clientX - r.left - 150, e.clientY - r.top - 20, tipo);
      }}
    >
      {blocos.length === 0 && (
        <div className="pg-empty">
          <div className="big">{ver ? "Nada para o operador ver ainda" : "Quadro vazio"}</div>
          <div>
            {ver
              ? "Volte para Editar e leve uma view publicada para o quadro."
              : "Arraste uma view publicada da coluna da esquerda, ou use “+ adicionar”."}
          </div>
        </div>
      )}
      {blocos.map((b) => {
        const item = itemDe(b.vid);
        const cor = item?.cor ?? COR_VIEW;
        const titulo = tituloDe(b);
        const dado = dadosBloco[b.vid];
        const linhas = dado?.estado === "ok" ? (dado.res.linhas ?? []).length : null;
        return (
          <div
            key={b.k}
            className={"pblock" + (b.tipo === "valor" ? " valor" : "") + (sel === b.k && !ver ? " sel" : "") + (ver ? " ver" : "")}
            style={{ left: b.x, top: b.y, width: b.w, height: b.h }}
            tabIndex={ver ? undefined : 0}
            role={ver ? undefined : "group"}
            aria-label={
              ver
                ? undefined
                : `Bloco ${titulo || "sem título"} · ${ROTULO_TIPO_BLOCO[b.tipo]} · view ${nomeDaView(b.vid)}. Setas movem, Shift+setas redimensionam.`
            }
            onMouseDown={() => !ver && setSel(b.k)}
            onFocus={() => !ver && setSel(b.k)}
            onKeyDown={(e) => {
              if (ver) return;
              // Digitar no título do bloco não pode mover o bloco.
              const alvo = e.target as HTMLElement;
              if (alvo.tagName === "INPUT" || alvo.tagName === "BUTTON") return;
              const passo = e.altKey ? 1 : 10;
              const eixo: Record<string, [number, number]> = {
                ArrowLeft: [-passo, 0],
                ArrowRight: [passo, 0],
                ArrowUp: [0, -passo],
                ArrowDown: [0, passo],
              };
              const d = eixo[e.key];
              if (!d) return;
              e.preventDefault();
              if (e.shiftKey) redimensionar(b.k, d[0], d[1]);
              else mover(b.k, d[0], d[1]);
            }}
          >
            <div
              className="pbh"
              style={{ borderTop: "3px solid " + cor }}
              onMouseDown={(e) =>
                !ver && !["BUTTON", "INPUT"].includes((e.target as HTMLElement).tagName) && down(e, b, "move")
              }
            >
              <span className="vd" style={{ background: cor }} />
              {ver ? (
                <span className="pbt">{titulo || "(sem título)"}</span>
              ) : (
                <input
                  className="pbt-in"
                  value={titulo}
                  placeholder="(sem título)"
                  aria-label={`Título do bloco da view ${nomeDaView(b.vid)}`}
                  onChange={(e) => setTit(b.k, e.target.value)}
                  onMouseDown={(e) => e.stopPropagation()}
                />
              )}
              {/* Contagem de linhas: o cabeçalho passa a dizer o TAMANHO do que o bloco mostra. */}
              {linhas != null && b.tipo !== "valor" && (
                <span className="rzhint" title="linhas que a view retornou">
                  {linhas === 1 ? "1 linha" : `${linhas.toLocaleString("pt-BR")} linhas`}
                </span>
              )}
              {!ver && (
                <button
                  className="x"
                  aria-label={`Remover o bloco ${titulo || nomeDaView(b.vid)} do quadro`}
                  onClick={() => del(b.k)}
                >
                  ×
                </button>
              )}
            </div>
            <div className="pbbody">{corpoBloco(b)}</div>
            {!ver && <div className="rsz" onMouseDown={(e) => down(e, b, "resize")} />}
          </div>
        );
      })}
      {!ver &&
        guides.map((g, i) => (
          <div key={i} className={"galign " + g.a} style={g.a === "x" ? { left: g.p } : { top: g.p }} />
        ))}
    </div>
  );

  const paletaFiltrada = paleta.filter((v) =>
    buscaPaleta.trim() ? v.nome.toLowerCase().includes(buscaPaleta.trim().toLowerCase()) : true,
  );

  return (
    <>
      {pedirDescarte && (
        <ConfirmarAcao
          titulo="Descartar alterações?"
          corpo={
            <>
              Há alterações no quadro que não foram salvas nem publicadas. Se sair agora, elas são
              perdidas.
            </>
          }
          rotuloConfirmar="Descartar e sair"
          perigo
          onConfirmar={() => {
            const acao = pedirDescarte;
            setPedirDescarte(null);
            acao();
          }}
          onCancelar={() => setPedirDescarte(null)}
        />
      )}
      <div className="page-head">
        <button className="link-back" onClick={() => comGuarda(onVoltar)}>
          ← Páginas
        </button>
        <div className="sd-head">
          {/* `flex:1 1 auto` + `minWidth:0`: sem crescer, este bloco se dimensionava pelo conteúdo e
              o título ficava preso na largura-base do campo, com folga sobrando ao lado. */}
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 14,
              flexWrap: "wrap",
              flex: "1 1 auto",
              minWidth: 0,
            }}
          >
            {/* O nome era um campo estreito no trilho da direita, truncado, repetindo este mesmo
                título. Passa a ser o próprio título: um lugar só e com largura para caber. Segue
                <input> (mesmo id/testid) DENTRO do h1 — sem ele a tela ficaria sem cabeçalho de
                nível 1, e o valor do campo é o que compõe o nome acessível do cabeçalho. */}
            <h1 className="display" style={{ margin: 0, flex: "1 1 300px", minWidth: 180, maxWidth: 620 }}>
              <input
                id="pg-nome"
                data-testid="pagina-nome"
                aria-label="Nome da página"
                title="Nome da página — gravado ao publicar; é por ele que o operador a encontra no Portal"
                style={TITULO_INPUT}
                value={nome}
                placeholder="Nova página"
                onChange={(e) => {
                  setNome(e.target.value);
                  setPub(false);
                }}
              />
            </h1>
            {versoes.length > 0 && (
              <SeletorVersao versoes={versoes} ultima={ultima} sel={versaoSel} ocupado={salvando || publicando} onSelecionar={selecionarVersao} />
            )}
            {versaoSel != null && versaoSel !== ultima && (
              <span className="ver-aviso">
                Vendo a v{versaoSel}. Publicar daqui cria uma versão NOVA — a v{versaoSel} fica intacta.
              </span>
            )}
          </div>
          <div className="sd-actions">
            <span className="modo-seg" role="group" aria-label="Modo do quadro">
              <button type="button" aria-pressed={!ver} className={!ver ? "on" : ""} onClick={() => setModo("editar")}>
                Editar
              </button>
              <button type="button" aria-pressed={ver} className={ver ? "on" : ""} onClick={() => setModo("ver")}>
                Pré-visualizar
              </button>
            </span>
            <button
              type="button"
              className="pag-btn"
              data-testid="pagina-salvar"
              disabled={salvando || publicando}
              onClick={() => void salvarRascunho().catch(() => {})}
            >
              {salvando ? "Salvando…" : "Salvar rascunho"}
            </button>
            <button className="btn-stamp" disabled={publicando || salvando} onClick={() => void publicar()}>
              <Icon name="stamp" cls="ic" /> {publicando ? "Publicando…" : "Publicar página"}
            </button>
          </div>
        </div>
        <p className="sub" style={{ marginTop: 8 }}>
          {sujo ? (
            <b data-testid="pagina-sujo">Alterações não salvas.</b>
          ) : baseNumero === 0 ? (
            "Página nova, sem definição salva — publique para ela entrar no catálogo do Portal."
          ) : (
            `Versão atual v${baseNumero}${
              versoes.find((v) => v.numero === baseNumero)?.status === "publicada"
                ? " (publicada)"
                : " (rascunho, ainda fora do Portal)"
            }.`
          )}{" "}
          Cada bloco referencia uma view publicada; a página não calcula nada.
        </p>
      </div>
      <div className="pad">
        {erro && (
          <div className="exec-banner-falha" role="alert" style={{ marginBottom: 14 }}>
            <span className="dot" />
            <span className="exec-banner-txt">{erro}</span>
          </div>
        )}
        {pub && (
          <div className="run-banner" role="status" style={{ marginTop: 0, marginBottom: 14 }}>
            <span className="dot" />
            <span>
              <b>Publicada</b> · v{baseNumero} · {blocos.length} bloco{blocos.length === 1 ? "" : "s"} de
              view{execAtivo ? " · página parametrizada" : ""} · já aparece no Portal do operador.
            </span>
          </div>
        )}
        {ver ? (
          <>
            <div className="filterbar" data-testid="pg-preview-filtros">
              {/* Chips, não selects: aqui não há valor a escolher — o operador escolhe no Portal.
                  Um <select> com uma opção "Tudo" era um controle que não controlava nada. */}
              {Object.keys(filt).filter((k) => filt[k]).length === 0 ? (
                <span className="fl">Sem filtros liberados</span>
              ) : (
                Object.keys(filt)
                  .filter((k) => filt[k])
                  .map((k) => (
                    <span key={k} className="fl">
                      {k} <b>livre</b>
                    </span>
                  ))
              )}
              <span className="rzhint">
                é o que o operador vê — ele escolhe o valor de cada filtro no Portal
              </span>
            </div>
            {origens.length > 0 && (
              <p style={{ ...NOTA, marginBottom: 10 }}>
                Prévia sobre o snapshot da última execução concluída de cada fonte:{" "}
                {origens.map(([ex, no], i) => (
                  <span key={ex}>
                    {i > 0 && " · "}
                    <button
                      type="button"
                      className="lk"
                      style={{ fontSize: 11 }}
                      title={`Abrir a execução ${ex}`}
                      onClick={() => navigate(`/execucoes/${ex}`)}
                    >
                      {ex.slice(0, 8)}
                    </button>
                    {no ? ` · nó ${no}` : ""}
                  </span>
                ))}
              </p>
            )}
            {canvasInner}
          </>
        ) : (
          <>
            {/* O que falta para publicar fica ACIMA das abas: a coluna da paleta é alta e o que
                vem depois do grid caía centenas de pixels abaixo da dobra (era onde morava o
                "Salvar rascunho", que ninguém achava). */}
            {problemas.length > 0 && (
              <div className="exec-aviso" data-testid="pg-problemas" style={{ marginBottom: 12, maxWidth: 720 }}>
                Falta para publicar: {problemas.join(" ")}
              </div>
            )}
            {/* Abas trocam a tela inteira (como no editor de Pipeline). */}
            <div
              className="cfg-tabs"
              data-testid="pg-cfg-tabs"
              role="group"
              aria-label="O que configurar na página"
              style={{ marginBottom: 14 }}
            >
              {([
                ["pagina", "Página"],
                ["filtros", `Filtros${Object.keys(filt).filter((k) => filt[k]).length ? ` (${Object.keys(filt).filter((k) => filt[k]).length})` : ""}`],
                ["execucao", `Execução${execAtivo ? " (ativa)" : ""}`],
              ] as const).map(([id, rot]) => (
                <button
                  key={id}
                  type="button"
                  aria-pressed={abaCfg === id}
                  className={abaCfg === id ? "on" : ""}
                  onClick={() => setAbaCfg(id)}
                >
                  {rot}
                </button>
              ))}
            </div>

            {/* Aba Página: paleta + quadro + config do bloco selecionado. */}
            <div style={{ display: abaCfg === "pagina" ? "block" : "none" }}>
              <div className="pbuild">
                <div
                  className="pg-lib"
                  // .pg-lib não tem regra no CSS: a coluna crescia com o nº de views e definia a
                  // altura da página (com 8 views, ~2000px). Rola dentro de si, junto do quadro.
                  style={{ maxHeight: 520, overflowY: "auto", paddingRight: 4 }}
                >
                  <h4 className="pg-col-h">Views publicadas</h4>
                  {paleta.length > 4 && (
                    <input
                      className="no-func-busca"
                      value={buscaPaleta}
                      placeholder="Filtrar views…"
                      aria-label="Filtrar views da paleta"
                      onChange={(e) => setBuscaPaleta(e.target.value)}
                    />
                  )}
                  {paleta.length === 0 && (
                    <div className="ld">
                      Nenhuma view publicada ainda. Publique uma view para poder montar a página.
                    </div>
                  )}
                  {paleta.length > 0 && paletaFiltrada.length === 0 && (
                    <div className="ld">Nenhuma view com “{buscaPaleta}”.</div>
                  )}
                  {paletaFiltrada.map((v) => {
                    const usada = blocos.some((b) => b.vid === v.id);
                    return (
                      <div
                        key={v.id}
                        // Sem `.used` (opacity .55): esmaecer o cartão derrubava o nome da view
                        // abaixo do contraste AA. Quem já está no quadro é dito por escrito.
                        className="libcard"
                        draggable
                        onDragStart={(e) => {
                          e.dataTransfer.setData("v", v.id);
                          e.dataTransfer.setData("t", "pivot");
                        }}
                      >
                        <div className="lt">
                          <span className="vdot" style={{ background: v.cor }} />
                          {v.nome}
                        </div>
                        <div className="ld">{usada ? "já está no quadro" : "arraste para o quadro"}</div>
                        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                          <button
                            className="add"
                            onClick={() => addAt(v.id, proximaPosicao(false).x, proximaPosicao(false).y, "pivot")}
                          >
                            + adicionar
                          </button>
                          <button
                            className="add"
                            title="Cartão-KPI: a view como um número único (soma da 1ª medida)"
                            onClick={() => addAt(v.id, proximaPosicao(true).x, proximaPosicao(true).y, "valor")}
                          >
                            + KPI
                          </button>
                        </div>
                      </div>
                    );
                  })}
                </div>
                {canvasInner}
                <div className="pg-settings" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                  {/* O box "Nome da página" saiu daqui: duplicava o título e, em 300px, chegava
                      truncado. O nome se edita no próprio título, no topo. */}
                  <div className="sbox" data-testid="pg-bloco-sel">
                    <span className="fld">Bloco selecionado</span>
                    {!blocoSel ? (
                      <div className="ld">
                        Clique num bloco (ou chegue nele por Tab) para mudar tipo, total e tamanho.
                        <br />
                        Setas movem · Shift+setas redimensionam · Alt refina em 1 px.
                      </div>
                    ) : (
                      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                        <div style={{ fontSize: 12.5 }}>
                          <b>{tituloDe(blocoSel) || "(sem título)"}</b>
                          <div className="ld">view: {nomeDaView(blocoSel.vid)}</div>
                        </div>
                        <div>
                          <span className="fld" id="pg-rot-tipo">Como exibir</span>
                          <span className="tipo-seg" role="group" aria-labelledby="pg-rot-tipo">
                            {TIPOS.map(([t, l]) => (
                              <button
                                key={t}
                                type="button"
                                aria-pressed={blocoSel.tipo === t}
                                className={blocoSel.tipo === t ? "on" : ""}
                                onClick={() => setTipo(blocoSel.k, t)}
                              >
                                {l}
                              </button>
                            ))}
                          </span>
                        </div>
                        {blocoSel.tipo !== "valor" && (
                          <div className="togg">
                            <span>
                              Linha de Total
                              <span className="ld" style={{ display: "block" }}>
                                soma cada coluna numérica no rodapé
                              </span>
                            </span>
                            <Interruptor
                              ligado={!!blocoSel.total}
                              rotulo="Mostrar linha de Total no rodapé do bloco"
                              onAlternar={() => setTotal(blocoSel.k, !blocoSel.total)}
                            />
                          </div>
                        )}
                        <div className="rzhint">
                          posição {Math.round(blocoSel.x)}, {Math.round(blocoSel.y)} · tamanho{" "}
                          {Math.round(blocoSel.w)}×{Math.round(blocoSel.h)}
                        </div>
                        <button className="lnk" onClick={() => del(blocoSel.k)}>
                          Remover este bloco do quadro
                        </button>
                      </div>
                    )}
                  </div>
                </div>
              </div>
            </div>

            {/* Aba Filtros — tela inteira. */}
            <div style={{ display: abaCfg === "filtros" ? "block" : "none", maxWidth: 620 }}>
              <div className="card" style={{ padding: 14 }}>
                <span className="fld" style={{ marginBottom: 9 }}>
                  O operador pode filtrar por
                </span>
                <div className="ld" style={{ marginBottom: 10 }}>
                  A lista vem das dimensões que as views dos blocos <b>liberaram</b> (
                  <code>filtros_liberados</code>). O Portal recusa filtro fora dessa lista.
                </div>
                {blocos.length === 0 && (
                  <div className="ld">Adicione blocos ao quadro: o filtro sai das views deles.</div>
                )}
                {blocos.length > 0 && carregandoDefs && dimsCandidatas.length === 0 && (
                  <div className="ld">Lendo as views dos blocos…</div>
                )}
                {blocos.length > 0 && !carregandoDefs && dimsCandidatas.length === 0 && (
                  <div className="ld">
                    Nenhuma das views deste quadro liberou dimensão para filtro. Publique a view com
                    <code> filtros_liberados</code> para o operador poder filtrar.
                  </div>
                )}
                {dimsCandidatas.map((k) => (
                  <div key={k} className="togg">
                    <span>{k}</span>
                    <Interruptor
                      ligado={!!filt[k]}
                      rotulo={`Liberar o filtro por ${k} no Portal`}
                      onAlternar={() => {
                        setFilt((f) => ({ ...f, [k]: !f[k] }));
                        setPub(false);
                      }}
                    />
                  </div>
                ))}
                {dimsOrfas.length > 0 && (
                  <div className="no-preview-erro" role="note" style={{ marginTop: 10 }}>
                    Filtro salvo em dimensão que nenhuma view do quadro libera:{" "}
                    <b>{dimsOrfas.join(", ")}</b>. O Portal recusa esse filtro (403) — desligue-o ou
                    libere a dimensão na view.
                    <div style={{ marginTop: 6 }}>
                      {dimsOrfas.map((d) => (
                        <button
                          key={d}
                          type="button"
                          className="lk"
                          style={{ marginRight: 10 }}
                          onClick={() => setFilt((f) => ({ ...f, [d]: false }))}
                        >
                          desligar {d}
                        </button>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </div>

            {/* Aba Execução — tela inteira. */}
            <div style={{ display: abaCfg === "execucao" ? "block" : "none", maxWidth: 720 }}>
              <PainelExecucao
                ativo={execAtivo}
                onToggle={() => {
                  setExecAtivo((v) => !v);
                  setPub(false);
                }}
                pipelineId={execPipeline}
                onPipeline={setExecPipeline}
                pipelines={pipelinesDisp}
                pipelineParams={pipelineParams}
                escolhidos={execParams}
                onAlternar={alternarParam}
                onRotulo={definirRotulo}
              />
            </div>

          </>
        )}
      </div>
    </>
  );
}

// ============================================================================
// PainelExecucao — edita definicao.execucao (página parametrizada), MODELO SIMPLES.
// Toggle on → escolhe o pipeline e QUAIS parâmetros declarados o operador preenche (+ rótulo).
// Tipo, opções e derivação vêm da definição do parâmetro no pipeline — não se redefine nada aqui.
// ============================================================================
interface PainelExecucaoProps {
  ativo: boolean;
  onToggle: () => void;
  pipelineId: string;
  onPipeline: (id: string) => void;
  pipelines: PipelineResumo[];
  pipelineParams: ParametroPipeline[];
  escolhidos: ExecParam[];
  onAlternar: (nome: string) => void;
  onRotulo: (nome: string, rotulo: string) => void;
}

// Estilo compartilhado dos inputs do painel (segue o look de .nm, mais compacto).
const inExec: React.CSSProperties = {
  width: "100%",
  border: "1px solid var(--hair)",
  borderRadius: 6,
  padding: "7px 9px",
  fontSize: 13,
  fontFamily: "inherit",
  color: "var(--ink)",
  background: "var(--surface)",
};

function CampoExec({ rotulo, children }: { rotulo: string; children: ReactNode }) {
  return (
    <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <span className="fld">{rotulo}</span>
      {children}
    </label>
  );
}

function PainelExecucao({
  ativo,
  onToggle,
  pipelineId,
  onPipeline,
  pipelines,
  pipelineParams,
  escolhidos,
  onAlternar,
  onRotulo,
}: PainelExecucaoProps) {
  const escolhido = (nome: string) => escolhidos.find((p) => p.param === nome);
  // O operador só preenche parâmetros NÃO-derivados; os derivados são resolvidos pelo pipeline.
  const operaveis = pipelineParams.filter((p) => !p.derivacao);
  const derivados = pipelineParams.filter((p) => p.derivacao);
  // Parâmetro `segredo` não pode ser escolhido aqui: o valor iria para a trilha/agendamento.
  const segredos = operaveis.filter((p) => (p as any).segredo);
  return (
    <div data-testid="painel-execucao" className="card" style={{ marginTop: 12, padding: 14 }}>
      <div className="togg" style={{ borderBottom: ativo ? "1px solid var(--hair-2)" : "none" }}>
        <span>Esta página é parametrizada (roda um pipeline)</span>
        <Interruptor
          ligado={ativo}
          rotulo="Esta página é parametrizada (roda um pipeline)"
          onAlternar={onToggle}
          testid="exec-ativo"
        />
      </div>

      {ativo && (
        <div style={{ display: "flex", flexDirection: "column", gap: 14, marginTop: 12 }}>
          <CampoExec rotulo="Pipeline que alimenta a página">
            <select style={inExec} value={pipelineId} onChange={(e) => onPipeline(e.target.value)}>
              <option value="">— escolha o pipeline —</option>
              {pipelines.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.nome}
                </option>
              ))}
            </select>
          </CampoExec>
          {!pipelineId && (
            <div className="exec-aviso">
              Sem pipeline escolhido a página não pode ser publicada como parametrizada.
            </div>
          )}

          {pipelineId && operaveis.length === 0 && (
            <div className="ld">
              Este pipeline não declara parâmetros. Defina os parâmetros na tela do Pipeline para o
              operador poder preenchê-los aqui.
            </div>
          )}

          {operaveis.length > 0 && (
            <div>
              <span className="fld">Parâmetros que o operador preenche</span>
              <div className="ld" style={{ marginTop: 4, marginBottom: 8 }}>
                Marque os que aparecem no Portal e ajuste o rótulo. O tipo de campo e as opções vêm
                da definição do parâmetro no pipeline.
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {operaveis.map((p) => {
                  const sel = escolhido(p.nome);
                  const ehSegredo = !!(p as any).segredo;
                  return (
                    <div key={p.nome} className="cfg-item" data-testid={`exec-param-${p.nome}`}>
                      <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
                        <input type="checkbox" checked={!!sel} onChange={() => onAlternar(p.nome)} />
                        <span style={{ fontFamily: "ui-monospace,Menlo,monospace", fontSize: 13 }}>{p.nome}</span>
                        <span className="ld">
                          · {ROTULO_TIPO_PARAM[p.tipo] ?? p.tipo}
                          {p.obrigatorio ? " · obrigatório" : ""}
                          {p.opcoes_fonte || (p.opcoes && p.opcoes.length) ? " · com opções" : ""}
                          {ehSegredo ? " · credencial" : ""}
                        </span>
                      </label>
                      {sel && (
                        <div style={{ marginTop: 8 }}>
                          <CampoExec rotulo="Rótulo (exibido ao operador)">
                            <input
                              style={inExec}
                              value={sel.rotulo ?? ""}
                              placeholder={p.nome}
                              onChange={(e) => onRotulo(p.nome, e.target.value)}
                            />
                          </CampoExec>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
              {segredos.length > 0 && (
                <div className="exec-aviso" style={{ marginTop: 10 }}>
                  {segredos.map((p) => p.nome).join(", ")}: parâmetro marcado como <b>credencial</b>.
                  O valor é redigido no registro da execução, mas evite expô-lo no Portal — prefira
                  guardar a credencial no cofre da conexão.
                </div>
              )}
            </div>
          )}

          {derivados.length > 0 && (
            <div>
              <span className="fld">Derivados (automáticos)</span>
              <div className="ld" style={{ marginTop: 4, marginBottom: 6 }}>
                Resolvidos pelo pipeline a partir de outro parâmetro — o operador não preenche.
              </div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                {derivados.map((p) => (
                  <span
                    key={p.nome}
                    style={{
                      fontFamily: "ui-monospace,Menlo,monospace",
                      fontSize: 12,
                      color: "var(--ink-2)",
                      background: "var(--hair-2)",
                      borderRadius: 4,
                      padding: "2px 8px",
                    }}
                  >
                    {p.nome}
                  </span>
                ))}
              </div>
            </div>
          )}

          <p style={{ ...NOTA, margin: 0 }}>
            Ao publicar, esta página aparece no Portal como <b>página parametrizada</b>: o operador
            preenche os parâmetros marcados, o pipeline roda e o relatório é gerado.
          </p>
        </div>
      )}
    </div>
  );
}
