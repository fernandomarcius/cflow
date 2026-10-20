import { Fragment, type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { Icon, Bloco } from "../ui";
import * as api from "../../api/client";
import type { CatalogoItem, ExecConfig, Opcao, ParametroPipeline } from "../../api/client";
import {
  celula as celulaDado,
  diaSP,
  fmtDataExtenso,
  fmtDuracaoMs,
  fmtHora,
  fmtMomento,
  fmtMomentoCurto,
  num,
  perfisDaAmostra,
  rotuloDia,
  type PerfilColuna,
} from "../execucao/formato";
import { papelData, resolverJanela } from "../periodos";

// Portal (F5): superfície do operador. Catálogo de páginas publicadas (read-only),
// parâmetros liberados → visualização (multi-pivot read-only) → relatório carimbado imutável.
// A página NÃO calcula — os blocos só referenciam o modelo.
//
// Uma página pode carregar uma config `execucao` (check parametrizado): nesse caso a etapa de
// parâmetros renderiza os inputs declarados no pipeline e "Gerar relatório" EXECUTA o pipeline
// com os parâmetros (operador + derivados) antes de consultar a página e mostrar a visualização.
// Sem `execucao` (relatório fixo), o fluxo segue como antes: deriva os filtros liberados das
// dimensões → consulta a página → visualização.
//
// O que esta tela não pode deixar de dizer, porque o número aqui é EVIDÊNCIA e não painel:
//  • de QUANDO é o dado (fim da execução que produziu os snapshots, em America/Sao_Paulo com
//    fuso explícito) — nada na tela pode passar por dado ao vivo;
//  • zero linha é resultado legítimo de um check e tem de se distinguir de tela quebrada/erro;
//  • número formatado pela MESMA régua da grade de Execuções e da tela de Relatórios (DECIMAL
//    chega como texto e não perde dígito; identificador não leva separador de milhar);
//  • parâmetro de escopo em branco é ERRO, nunca "traz tudo" (senão a execução lê a base inteira
//    e devolve um resultado plausível e errado).

type ResumoPagina = CatalogoItem;

interface Origem {
  snapshot?: string;
  execucao_id?: string;
  no_id?: string;
  hash?: string;
  // Como o snapshot foi resolvido: "execucao" (a execução pedida), "cadeia" (execução IRMÃ do
  // mesmo disparo — bloco cujo cubo lê um pipeline upstream do DAG) ou "ultima" (fallback para a
  // última concluída do pipeline do cubo — fora do disparo; tem de ser dito na tela).
  resolucao?: "execucao" | "cadeia" | "ultima";
}

interface Resultado {
  colunas: string[];
  linhas: Record<string, unknown>[];
  layout?: { linhas?: string[]; colunas?: string[]; valores?: string[] };
  dimensoes?: string[];
  medidas?: string[];
  origem?: Origem;
}

interface BlocoConsulta {
  titulo?: string;
  tipo?: string;
  total?: boolean; // mostra linha de Total (soma das colunas) no rodapé — configurável por bloco
  view_id: string;
  resultado: Resultado;
}

interface ConsultaPagina {
  pagina: string;
  versao: number;
  blocos: BlocoConsulta[];
}

interface TraceItem {
  view_id: string;
  origem?: Origem;
}

interface Relatorio {
  id: string;
  pagina?: string;
  versao?: number;
  trace?: TraceItem[];
}

interface Filtro {
  dimensao: string;
  op: string;
  valor: unknown;
}

// Dimensão liberada derivada do resultado da página: nome + valores distintos observados.
interface ParamLiberado {
  dimensao: string;
  valores: string[];
}

const TODOS = "__todos__";

// Quantas linhas de um bloco a tela desenha antes de pedir confirmação. Um bloco de dezenas de
// milhares de linhas travava a aba; e truncar em SILÊNCIO seria pior que truncar dizendo.
const LINHAS_POR_BLOCO = 200;

// Separador interno de chave de agrupamento do cross-tab. Antes as chaves de linha eram juntadas
// com "" — ("SP","A") e ("S","PA") colidiam e DUAS linhas do relatório viravam uma, em silêncio.
const SEP_CHAVE = "\u0001";

const ehKpi = (t?: string) => ["kpi", "valor"].includes((t ?? "").toLowerCase());
const ehLargo = (t?: string) => ["observacoes", "detalhe", "tabela"].includes((t ?? "").toLowerCase());

function curto(id?: string): string {
  return id ? id.slice(0, 8) : "—";
}

// Ordenação de texto no padrão pt-BR e com `numeric`: sem isso "10" vinha antes de "9" nos menus
// de valor e nas colunas do cross-tab.
function cmpTexto(a: unknown, b: unknown): number {
  return String(a ?? "").localeCompare(String(b ?? ""), "pt-BR", { numeric: true });
}

// Número a partir do valor cru: aceita texto numérico porque DECIMAL chega da API como STRING —
// somar só `typeof number` zerava a coluna (o rodapé "Total" de uma coluna de valor saía vazio).
function comoNumero(v: unknown): number | null {
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (typeof v === "string" && v !== "") {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

// Célula do dado: perfil por COLUNA (tipo + casas decimais uniformes), DECIMAL (que chega como
// texto) sem perder dígito, identificador sem separador de milhar, NULL distinguível de string
// vazia. Mesma régua da grade de Execuções (`execucao/formato.ts`) e da tela de Relatórios.
function renderCelula(v: unknown, perfil?: PerfilColuna): ReactNode {
  if (typeof v === "boolean") {
    // `aria-label` porque `title` não é anunciado de forma confiável; tons dos pills de status (AA).
    return (
      <span aria-label={v ? "Sim" : "Não"} style={{ color: v ? "#2f6b46" : "#a8353d", fontWeight: 800, fontSize: 14 }}>
        {v ? "✓" : "✗"}
      </span>
    );
  }
  const cel = celulaDado(v, perfil);
  const titulo = cel.titulo ?? (cel.texto !== String(v) ? String(v) : undefined);
  if (cel.vazio) return <span className="cel-nula" title={titulo}>{cel.texto}</span>;
  return <span title={titulo}>{cel.texto}</span>;
}

// Linha de Total (rodapé): soma por COLUNA numérica; 1ª coluna não-numérica vira "Total".
// A conta é a MESMA da tela de Relatórios — o carimbo não pode mostrar um total diferente do que
// o operador viu quando pediu o relatório.
function linhaTotal(
  mostrar: boolean | undefined,
  cols: string[],
  linhas: Record<string, unknown>[],
  perfis: Record<string, PerfilColuna>,
): ReactNode[] | undefined {
  if (!mostrar) return undefined;
  return cols.map((c, ci) => {
    if (perfis[c]?.tipo !== "numero") return <b key={c}>{ci === 0 ? "Total" : ""}</b>;
    const soma = linhas.reduce((s, l) => s + (comoNumero(l[c]) ?? 0), 0);
    return <b key={c}>{renderCelula(soma, perfis[c])}</b>;
  });
}

// Número do cartão-KPI: soma da 1ª medida da view, com o rótulo dizendo QUAL medida é e sobre
// quantas linhas ela soma — "Total: 36" sem dizer de quê é número anônimo (e somar medidas de
// grandezas diferentes daria um número sem sentido).
function kpiDoBloco(res: Resultado, tituloBloco: string): { label: string; val: string } {
  const medidas = res.medidas ?? res.layout?.valores ?? [];
  const linhas = res.linhas ?? [];
  const medida = medidas[0];
  if (!medida) return { label: tituloBloco, val: "—" };
  let soma = 0;
  for (const l of linhas) soma += comoNumero(l[medida]) ?? 0;
  const perfil = perfisDaAmostra([medida], linhas)[medida];
  return {
    label: `${medida}${linhas.length > 1 ? ` · soma de ${num(linhas.length)} linhas` : ""}`,
    val: celulaDado(soma, perfil).texto,
  };
}

// Deriva os parâmetros (filtros liberados) a partir das dimensões dos blocos e dos
// valores distintos observados — o operador só escolhe dentro do que o especialista expôs.
function derivarParams(blocos: BlocoConsulta[]): ParamLiberado[] {
  const acc = new Map<string, Set<string>>();
  for (const b of blocos) {
    const dims = b.resultado.dimensoes ?? b.resultado.layout?.linhas ?? [];
    for (const d of dims) {
      if (!acc.has(d)) acc.set(d, new Set());
      const set = acc.get(d)!;
      for (const l of b.resultado.linhas) {
        const v = (l as Record<string, unknown>)[d];
        if (v !== null && v !== undefined) set.add(String(v));
      }
    }
  }
  return [...acc.entries()].map(([dimensao, vals]) => ({
    dimensao,
    valores: [...vals].sort(cmpTexto),
  }));
}

// ---- Suporte ao fluxo de execução (check parametrizado) ----

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

// Mês de competência corrente ("YYYY-MM") — default do filtro.
function mesAtual(): string {
  const h = new Date();
  return `${h.getFullYear()}-${pad2(h.getMonth() + 1)}`;
}

// Resolução de período (mês/semana/dia/relativos/intervalo) mora em `cflow/periodos.ts`;
// momentos/durações vêm de `cflow/execucao/formato.ts` (fuso explícito) — as telas não podem
// divergir na hora que exibem.

// Parâmetro do pipeline exibido ao operador: a DEFINIÇÃO (tipo/opções/obrigatório) vem do pipeline;
// o rótulo vem da config da página. As opções (estáticas `opcoes` ou dinâmicas `opcoes_fonte`) são
// resolvidas no carregamento; `opcoes` vazio = campo livre (input por tipo).
interface ParamOperador {
  nome: string;
  rotulo: string;
  decl: ParametroPipeline;
  opcoes: Opcao[]; // {valor (enviado), rotulo (exibido)}
  // Opções dinâmicas que NÃO carregaram. Sem esta marca o campo caía silenciosamente para texto
  // livre — e digitar um código que não existe devolve relatório plausível e vazio.
  opcoesFalharam?: boolean;
}

// Janela padrão (mês atual) p/ pré-preencher parâmetros `data` de início/fim.
function janelaMesAtual(): { inicio: string; fim: string } {
  const iv = resolverJanela(mesAtual());
  return { inicio: iv.data_inicio, fim: iv.data_fim };
}

// Converte o valor digitado no tipo do parâmetro do pipeline para o binding do SQL (mantém o
// binding inteiro das fontes — sem regressão). Vazio → "" (filtro neutro).
function valorPorTipo(decl: ParametroPipeline, v: string): unknown {
  if (v === "") return "";
  if (decl.tipo === "inteiro") return /^-?\d+$/.test(v) ? Number(v) : v;
  return v;
}

// id do campo — o rótulo precisa apontar para ele (`label htmlFor`), senão o leitor de tela
// anuncia um input sem nome, e é neste id que o foco cai quando falta um obrigatório.
function idCampo(nome: string): string {
  return `pt-campo-${nome}`;
}

// Rótulo de campo do formulário do operador. `.plabel` pinta a célula inteira de LIMA sólido: com
// três obrigatórios seguidos, o formulário lia como três erros na tela antes de o operador digitar
// qualquer coisa. Fundo neutro + filete separando a coluna do rótulo; "obrigatório" passa a ser o
// asterisco mais uma barra de acento à esquerda (só nos que são). Inline porque `cflow.css` não é
// editável nesta empreitada — o CSS equivalente vai no relatório.
const ROTULO_CAMPO: React.CSSProperties = {
  background: "var(--soft)",
  borderRight: "1px solid var(--hair-2)",
};
const ROTULO_OBRIG: React.CSSProperties = {
  ...ROTULO_CAMPO,
  boxShadow: "inset 3px 0 0 var(--lime)",
};

// A data resolvida por extenso (ver `fmtDataExtenso`): é confirmação do que foi escolhido, não
// dica — precisa do peso de --ink, senão some ao lado do texto de apoio. Fica ACIMA do campo
// (o formato do `input[type=date]` é do navegador e pode ser mm/dd/yyyy).
const DATA_LIDA: React.CSSProperties = { fontSize: 12, color: "var(--ink)", fontWeight: 600 };
// Mesma linha, ainda sem data escolhida: existe para o campo não trocar de posição quando o valor
// chega, e para dizer que ali vai aparecer a leitura em português.
const DATA_LIDA_VAZIA: React.CSSProperties = { fontSize: 12, color: "var(--ink-2)", fontWeight: 500 };
// Marca de "quem preencheu foi a plataforma": peso normal ao lado da data (que está em --ink/600),
// em `--ink-2` (7,5:1 no claro; token já vetado no escuro) — é ressalva, não o dado.
const SUGERIDO: React.CSSProperties = { color: "var(--ink-2)", fontWeight: 400 };

// Célula da coluna de carimbo que NÃO é um instante ("nunca", "carregando…", "não disponível"):
// `--ink-2` (7,5:1 sobre o branco; `--ink-3` não chega a 4,5:1) + itálico para não se confundir com
// uma data. É a informação que decide se o relatório de hoje já saiu — não pode ser a mais apagada
// da linha. Inline porque `cflow.css` não é editável nesta empreitada.
const CARIMBO_FRACO: React.CSSProperties = { color: "var(--ink-2)", fontStyle: "italic" };

// Quando um relatório foi carimbado, na régua das telas de execução: "hoje, 14:22" / "ontem,
// 09:07" / "12/07 16:40". O fuso aparece UMA vez, no cabeçalho da coluna.
function quandoCarimbo(iso: string): string {
  const dia = diaSP(iso);
  const rot = dia ? rotuloDia(dia) : null;
  return rot === "hoje" || rot === "ontem" ? `${rot}, ${fmtHora(iso)}` : fmtMomentoCurto(iso);
}

// Cores da linha fina no topo de cada bloco (cosmético, cicla por índice). Mesma paleta do preview
// da Página e da tela de Relatórios — blocos idênticos entre telas. A 1ª cor é o token do tema:
// #002050 fixo desaparecia no fundo escuro.
const LINHA_CORES = ["var(--navy)", "#5f8a2e", "#46566b", "#0a2f63", "#b23b43"] as const;

// Pivota o resultado (formato longo do cubo) em cross-tab LARGO conforme o layout da view:
// as dimensões de `colunas` viram colunas; `linhas` agrupam; `valores` preenchem as células.
// Sem `colunas` no layout → devolve como veio (1 linha por grupo). Espelha o que o Perspective
// faz, para a tabela-fallback do Portal honrar a config da view.
function pivotar(r: Resultado): { colunas: string[]; linhas: Record<string, unknown>[] } {
  const colDims = r.layout?.colunas ?? [];
  const rowDims = r.layout?.linhas ?? [];
  const valores = r.layout?.valores ?? [];
  if (colDims.length === 0 || rowDims.length === 0 || valores.length === 0) {
    return { colunas: r.colunas ?? [], linhas: r.linhas ?? [] };
  }
  // Rótulo da coluna sai da MESMA formatação da célula: com `toLocaleString` o ano 2026 virava
  // cabeçalho "2.026" e a data ISO ficava crua.
  const perfisDim = perfisDaAmostra(colDims, r.linhas);
  const rotulo = (l: Record<string, unknown>, d: string) => celulaDado(l[d], perfisDim[d]).texto;
  const chaveCol = (l: Record<string, unknown>) => colDims.map((d) => rotulo(l, d)).join(SEP_CHAVE);
  const chaveLin = (l: Record<string, unknown>) => rowDims.map((d) => String(l[d] ?? "")).join(SEP_CHAVE);
  const chaves: string[] = [];
  for (const l of r.linhas) {
    const k = chaveCol(l);
    if (!chaves.includes(k)) chaves.push(k);
  }
  chaves.sort(cmpTexto);
  const multiVal = valores.length > 1;
  const nomeCol = (chave: string, v: string) => {
    const base = chave.split(SEP_CHAVE).join(" · ");
    return multiVal ? `${base} · ${v}` : base;
  };
  const colunas = [...rowDims, ...chaves.flatMap((k) => valores.map((v) => nomeCol(k, v)))];
  const grupos = new Map<string, Record<string, unknown>>();
  const ordem: string[] = [];
  for (const l of r.linhas) {
    const rk = chaveLin(l);
    if (!grupos.has(rk)) {
      const base: Record<string, unknown> = {};
      for (const d of rowDims) base[d] = l[d];
      grupos.set(rk, base);
      ordem.push(rk);
    }
    const g = grupos.get(rk)!;
    const k = chaveCol(l);
    for (const v of valores) g[nomeCol(k, v)] = l[v];
  }
  return { colunas, linhas: ordem.map((rk) => grupos.get(rk)!) };
}

// Compara dois valores de uma coluna. Coluna numérica compara por NÚMERO mesmo quando o valor chega
// como texto (DECIMAL): por texto, 9 vinha depois de 10. NULL/vazio fica sempre no fim.
function cmpValor(va: unknown, vb: unknown, perfil?: PerfilColuna): number {
  const vazioA = va === null || va === undefined || va === "";
  const vazioB = vb === null || vb === undefined || vb === "";
  if (vazioA || vazioB) return vazioA && vazioB ? 0 : vazioA ? 1 : -1;
  if (perfil?.tipo === "numero") {
    const na = comoNumero(va);
    const nb = comoNumero(vb);
    if (na !== null && nb !== null) return na - nb;
  }
  return cmpTexto(va, vb);
}

// Ordena as linhas por uma coluna (clique no cabeçalho). O vazio continua no fim nos dois sentidos:
// "não veio nada" não é o maior nem o menor valor da coluna.
function ordenarLinhas(
  linhas: Record<string, unknown>[],
  col: string,
  dir: "asc" | "desc",
  perfil?: PerfilColuna,
): Record<string, unknown>[] {
  const m = dir === "asc" ? 1 : -1;
  return [...linhas].sort((a, b) => {
    const va = a[col];
    const vb = b[col];
    const vazio = va === null || va === undefined || va === "" || vb === null || vb === undefined || vb === "";
    const r = cmpValor(va, vb, perfil);
    return vazio ? r : r * m;
  });
}

// Ordem PADRÃO das linhas de um bloco agregado, por uma ou mais colunas de dimensão. O cubo não
// define ordem de linha: a MESMA execução exibia as UFs em ordens diferentes em duas aberturas —
// e um relatório que se apresenta como carimbo de um instante precisa ser reproduzível (a conferência
// linha a linha contra a fonte, e a comparação de dois carimbos, dependem disso).
function ordemEstavel(
  linhas: Record<string, unknown>[],
  cols: string[],
  perfis: Record<string, PerfilColuna>,
): Record<string, unknown>[] {
  if (cols.length === 0) return linhas;
  return [...linhas].sort((a, b) => {
    for (const c of cols) {
      const r = cmpValor(a[c], b[c], perfis[c]);
      if (r !== 0) return r;
    }
    return 0;
  });
}

// Nome do arquivo exportado: nome da página + data/hora de São Paulo. "relatorio-92b4190d.xlsx"
// não dizia nem de qual relatório nem de quando era, e o arquivo circula por e-mail.
function nomeArquivo(nome: string): string {
  const slug =
    nome
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "")
      .replace(/[^A-Za-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .toLowerCase()
      .slice(0, 60) || "relatorio";
  // sv-SE → "YYYY-MM-DD HH:MM:SS" (ISO-like). Separador tolerante e defaults: um formato de
  // navegador diferente do esperado não pode derrubar o download.
  const [dia = "", hora = ""] = new Date()
    .toLocaleString("sv-SE", { timeZone: "America/Sao_Paulo" })
    .split(/[ T]/);
  return `${slug}-${dia}-${hora.replace(/:/g, "").slice(0, 4)}.xlsx`;
}

// Ação em curso: rótulo e `disabled` de cada botão saem daqui. Antes um único `carregando` fazia
// "Exportar Excel" trocar o rótulo do botão de carimbo para "Carimbando…".
type Acao = "abrir" | "gerar" | "carimbar" | "exportar" | null;

export default function Portal() {
  // Ordenação por bloco (índice) → { coluna, direção }. Clicar no cabeçalho ordena/inverte.
  const [ordCol, setOrdCol] = useState<Record<number, { col: string; dir: "asc" | "desc" }>>({});
  // Blocos com "mostrar todas as linhas" pedido explicitamente.
  const [expandido, setExpandido] = useState<Record<number, boolean>>({});
  const [catalogo, setCatalogo] = useState<ResumoPagina[]>([]);
  const [carregandoCatalogo, setCarregandoCatalogo] = useState(true);
  const [erroCatalogo, setErroCatalogo] = useState("");
  const [sel, setSel] = useState<number>(-1);
  const [etapa, setEtapa] = useState<"catalogo" | "params" | "vis">("catalogo");
  // O relatório aberto vive na URL (`?rel=<id da página>`): sem isso o F5 devolvia o operador ao
  // catálogo e o "voltar" do navegador saía do Portal inteiro em vez de voltar um passo.
  const [busca, setBusca] = useSearchParams();
  const relParam = busca.get("rel");
  const [consulta, setConsulta] = useState<ConsultaPagina | null>(null);
  // Execução que o operador disparou (check parametrizado). Consulta/carimbo/export usam ela →
  // o painel mostra os números DESTA run, não da última execução qualquer do pipeline.
  const [execId, setExecId] = useState<string | null>(null);
  // Quando o dado exibido foi congelado (fim da execução que produziu os snapshots). Sem isto a
  // tela mostrava números sem dizer de quando são — e um relatório imutável que não diz de quando
  // é não serve de evidência.
  const [congeladoEm, setCongeladoEm] = useState<string | null>(null);
  const [params, setParams] = useState<ParamLiberado[]>([]);
  const [escolhas, setEscolhas] = useState<Record<string, string>>({});
  const [relatorio, setRelatorio] = useState<Relatorio | null>(null);
  const [acao, setAcao] = useState<Acao>(null);
  // Execução em curso disparada pelo operador (com o instante de início, p/ o cronômetro).
  const [gerando, setGerando] = useState<{ desde: number; execucao: string | null } | null>(null);
  // Execução que não terminou dentro do tempo de espera: guardada para o operador CONFERIR de
  // novo em vez de disparar outra run das fontes.
  const [pendente, setPendente] = useState<string | null>(null);
  // Cache de relatório do dia: execuções de hoje com os mesmos parâmetros (modal ver/histórico/novo).
  const [mostrarCache, setMostrarCache] = useState(false);
  const [cacheDoDia, setCacheDoDia] = useState<api.ExecDoDia[]>([]);
  const [erro, setErro] = useState("");
  const [aviso, setAviso] = useState("");

  // --- Estado do fluxo de página parametrizada (definicao.execucao) ---
  // Modelo SIMPLES: a página só lista QUAIS params do pipeline o operador preenche (+ rótulo). A
  // definição de cada param (tipo/opções) vem do pipeline; o valor escolhido é indexado pelo `param`.
  const [paramsOperador, setParamsOperador] = useState<ParamOperador[]>([]);
  const [valores, setValores] = useState<Record<string, string>>({}); // param → valor escolhido
  const [carregandoParams, setCarregandoParams] = useState(false);
  // Carga dos parâmetros FALHOU. Sem este estado, `paramsOperador` vazio por erro era exibido como
  // "este relatório roda sem parâmetros" — e o botão de gerar seguia ativo. Rodar um pipeline cujos
  // parâmetros de escopo não se conhecem é o cenário que a plataforma proíbe: a execução leria a
  // base inteira e devolveria um número plausível e errado.
  const [erroParams, setErroParams] = useState("");
  // Parâmetro obrigatório em branco na última tentativa de gerar (marca o campo, não só o topo).
  const [faltante, setFaltante] = useState<string | null>(null);
  // Parâmetros que a PLATAFORMA preencheu (janela do mês atual), enquanto o operador não os trocou.
  // O valor sugerido não pode se passar por escolha de quem vai carimbar o relatório — e o fim da
  // janela é o 1º dia do mês seguinte (janela com fim exclusivo), que sem marca lê como "o relatório
  // vai até agosto".
  const [sugeridos, setSugeridos] = useState<Record<string, boolean>>({});

  // A marca "de sistema" NÃO filtra o Portal — e isso é regra, não descuido. Ela protege o fluxo
  // crítico dentro do STUDIO: o especialista não vê nem altera a definição. O resultado publicado
  // é justamente o que operador e todo mundo devem consumir. Por isso esta tela não pergunta o
  // perfil de ninguém: o catálogo publicado é o mesmo para todos.
  // Último relatório CARIMBADO de cada página: é por ele que o operador sabe se o de hoje já saiu.
  const [carimbos, setCarimbos] = useState<api.RelatorioResumo[]>([]);
  // A coluna tem TRÊS estados, e "nunca" é só um deles. `listarRelatorios` LANÇA quando a chamada
  // falha (o client deixou de transformar erro em lista vazia): sem estado próprio a promessa
  // rejeitava sem tratamento e TODA linha exibia "nunca" — uma afirmação ("esta página nunca foi
  // carimbada") no lugar de "não conseguimos saber". Numa tela de evidência isso é o pior erro:
  // o operador conclui que o relatório de hoje não saiu e gera outro.
  const [carimbosEstado, setCarimbosEstado] = useState<"carregando" | "ok" | "erro">("carregando");
  const [carimbosErro, setCarimbosErro] = useState("");

  const carregando = acao !== null;

  useEffect(() => {
    let vivo = true;
    api
      .listarRelatorios()
      .then((r) => {
        if (!vivo) return;
        setCarimbos(r);
        setCarimbosEstado("ok");
      })
      .catch((e: any) => {
        if (!vivo) return;
        setCarimbosErro(e?.message ?? "Falha ao carregar os relatórios carimbados.");
        setCarimbosEstado("erro");
      });
    return () => {
      vivo = false;
    };
  }, []);

  useEffect(() => {
    let vivo = true;
    api
      .catalogo()
      .then((c) => {
        if (!vivo) return;
        setCatalogo(c);
        // Deep link / F5: reabre o relatório que está na URL. Sem isto o operador recarregava a
        // página e voltava ao catálogo, tendo de achar o relatório de novo.
        const alvo = relParam ? c.findIndex((p) => p.id === relParam) : -1;
        if (alvo >= 0) void abrirRef.current?.(alvo);
      })
      .catch((e: any) =>
        vivo && setErroCatalogo(e?.message ?? "Não foi possível carregar o catálogo de relatórios."),
      )
      .finally(() => vivo && setCarregandoCatalogo(false));
    return () => {
      vivo = false;
    };
  }, []);

  // `abrir` é declarada depois; a ref evita reordenar o arquivo só para o deep link enxergá-la.
  const abrirRef = useRef<((i: number) => Promise<void>) | null>(null);

  // Voltar/avançar do navegador: `?rel=` é a fonte da verdade de qual relatório está aberto. Abrir
  // um relatório empilha um passo de histórico, mas nada lia a URL de volta — medido: depois do
  // Back o endereço ficava sem `rel` e o FORMULÁRIO continuava na tela. Tela e endereço discordando
  // é o que fazia o segundo Back sair do Portal inteiro em vez de voltar ao catálogo.
  useEffect(() => {
    if (carregandoCatalogo) return; // a abertura por deep link é resolvida na carga do catálogo
    if (!relParam) {
      if (sel >= 0) voltarCatalogo();
      return;
    }
    const alvo = catalogo.findIndex((p) => p.id === relParam);
    if (alvo >= 0) {
      if (alvo !== sel) void abrirRef.current?.(alvo);
      return;
    }
    // `rel` de página que não está mais publicada: limpa o endereço (replace — sem novo passo de
    // histórico) em vez de deixar um link que não abre nada.
    const q = new URLSearchParams(busca);
    q.delete("rel");
    setBusca(q, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [relParam, carregandoCatalogo]);

  const paginaAtual = sel >= 0 ? catalogo[sel] : undefined;
  const execAtual = paginaAtual?.execucao ?? null;

  // Todo o catálogo publicado, guardando o índice original (é ele que `abrir(i)` espera).
  const visiveis = useMemo(() => catalogo.map((p, i) => ({ p, i })), [catalogo]);

  const filtrosEscolhidos = useMemo<Filtro[]>(
    () =>
      Object.entries(escolhas)
        .filter(([, v]) => v && v !== TODOS)
        .map(([dimensao, valor]) => ({ dimensao, op: "igual", valor })),
    [escolhas],
  );

  // Cronômetro de 1s enquanto a execução roda: sem ele uma run de dois minutos parecia travada.
  const [agora, setAgora] = useState(() => Date.now());
  useEffect(() => {
    if (!gerando) return;
    setAgora(Date.now());
    const t = window.setInterval(() => setAgora(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [gerando]);

  // Execuções que produziram os snapshots exibidos. Cada bloco vem de uma view (e pode vir de uma
  // execução diferente): dizer "congelado em X" com blocos de origens distintas seria mentira.
  const origens = useMemo(() => {
    const ids = new Set<string>();
    for (const b of consulta?.blocos ?? []) {
      const e = b.resultado?.origem?.execucao_id;
      if (e) ids.add(e);
    }
    return [...ids];
  }, [consulta]);

  // Blocos que NÃO vieram deste disparo (fallback "última concluída" do pipeline do cubo):
  // só existe quando houve um disparo (execId) e o cubo lê outro pipeline sem execução irmã na
  // cadeia. Tem de aparecer — atribuir esses números ao disparo seria evidência falsa.
  const forasDoDisparo = useMemo(() => {
    if (!execId) return [] as string[];
    return (consulta?.blocos ?? [])
      .filter((b) => b.resultado?.origem?.resolucao === "ultima")
      .map((b) => b.titulo || curto(b.view_id));
  }, [consulta, execId]);

  // Várias execuções, mas todas do MESMO disparo (raiz + dependências da cadeia): o instante é um
  // só — não é o caso "blocos de origens distintas" que invalida o "congelado em".
  const mesmoDisparo =
    !!execId &&
    forasDoDisparo.length === 0 &&
    (consulta?.blocos ?? []).every((b) => {
      const o = b.resultado?.origem;
      return !o?.execucao_id || o.execucao_id === execId || o.resolucao === "cadeia";
    });

  const execExibida = execId ?? (origens.length === 1 ? origens[0] : null);

  // Relatório fixo (sem `execucao`) não passa por `aguardarExecucao`, então o instante do
  // congelamento vem da execução que gerou o snapshot consultado.
  useEffect(() => {
    if (!execExibida || congeladoEm) return;
    let vivo = true;
    api
      .consultarExecucao(execExibida)
      .then((d) => vivo && setCongeladoEm(d.concluida_em ?? null))
      .catch(() => {
        /* sem permissão/indisponível: a tela diz que o instante não está disponível */
      });
    return () => {
      vivo = false;
    };
  }, [execExibida, congeladoEm]);

  // Trilho de relatórios (etapa de parâmetros): a fila de pills não cabia na largura da tela e o
  // último era CORTADO no limite — sem barra, sem reticências, sem jeito de alcançá-lo. Agora a
  // fila rola na horizontal entre as setas; este efeito mantém o relatório aberto à vista.
  // Mede por `getBoundingClientRect` (não `offsetLeft`): o trilho não é `position:relative`, então
  // o offsetParent do pill é um ancestral qualquer e a conta daria um scroll errado.
  const trilhoRef = useRef<HTMLDivElement | null>(null);
  const abaAtivaRef = useRef<HTMLButtonElement | null>(null);
  // Há mais relatório fora da vista, de cada lado? Vira o esmaecimento da borda: sem ele o pill
  // recortado no limite continua parecendo defeito de layout em vez de "a fila continua".
  const [maisAbas, setMaisAbas] = useState({ esq: false, dir: false });
  function medirTrilho() {
    const box = trilhoRef.current;
    if (!box) return;
    const esq = box.scrollLeft > 2;
    const dir = box.scrollLeft + box.clientWidth < box.scrollWidth - 2;
    setMaisAbas((m) => (m.esq === esq && m.dir === dir ? m : { esq, dir }));
  }
  // As setas ROLAM a fila — é isso que uma seta ao lado de uma tira que rola parece fazer. Antes
  // elas TROCAVAM de relatório: um clique reflexo em "›" para alcançar o último pill (medido: 57px
  // fora da vista em 1440, 319px em 1180) descartava tudo o que já estava preenchido e disparava
  // outra carga de parâmetros. Trocar de relatório é o pill, que diz qual é.
  function rolarTrilho(passo: 1 | -1) {
    const box = trilhoRef.current;
    if (!box) return;
    const suave = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    box.scrollBy({
      left: passo * Math.max(180, Math.round(box.clientWidth * 0.7)),
      behavior: suave ? "smooth" : "auto",
    });
  }
  useEffect(() => {
    const box = trilhoRef.current;
    const el = abaAtivaRef.current;
    if (!box) return;
    if (el) {
      const rb = box.getBoundingClientRect();
      const re = el.getBoundingClientRect();
      if (re.left < rb.left + 12) box.scrollLeft -= rb.left + 12 - re.left;
      else if (re.right > rb.right - 12) box.scrollLeft += re.right - (rb.right - 12);
    }
    medirTrilho();
    // A largura do pill depende da fonte da marca: medir só antes dela carregar deixaria as setas
    // dizendo "não há mais fila" quando há (o `resize` da janela não cobre reflow de fonte).
    void document.fonts?.ready.then(() => medirTrilho());
    const aoRedimensionar = () => medirTrilho();
    window.addEventListener("resize", aoRedimensionar);
    return () => window.removeEventListener("resize", aoRedimensionar);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sel, etapa, catalogo.length]);

  // Rola até o carimbo recém-gerado: em 1180px o cartão de rastreio nascia abaixo da dobra e a
  // ação parecia não ter efeito.
  const traceRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!relatorio) return;
    const suave = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const t = window.setTimeout(
      () => traceRef.current?.scrollIntoView({ block: "nearest", behavior: suave ? "smooth" : "auto" }),
      60,
    );
    return () => window.clearTimeout(t);
  }, [relatorio]);

  // Abre uma página. Com `execucao` → carrega os parâmetros declarados (não consulta a página
  // ainda; a consulta vem após a execução). Sem `execucao` → consulta sem filtros para descobrir
  // os parâmetros liberados (fluxo original).
  async function abrir(i: number) {
    const item = catalogo[i];
    if (!item) return;
    setSel(i);
    setRelatorio(null);
    setEscolhas({});
    setParamsOperador([]);
    setValores({});
    setSugeridos({});
    setErroParams("");
    setFaltante(null);
    setMostrarCache(false);
    setCacheDoDia([]);
    setConsulta(null);
    setParams([]);
    setOrdCol({});
    setExpandido({});
    // Execução/instante são de OUTRA página: mantê-los faria o carimbo desta citar a run anterior.
    setExecId(null);
    setCongeladoEm(null);
    setPendente(null);
    setErro("");
    setAviso("");
    setEtapa("params");
    if (busca.get("rel") !== item.id) {
      const q = new URLSearchParams(busca);
      q.set("rel", item.id);
      setBusca(q);
    }

    if (item.execucao) {
      await prepararExecucao(item.id, item.execucao);
    } else {
      setAcao("abrir");
      try {
        const res = (await api.consultarPagina(item.id)) as ConsultaPagina;
        setConsulta(res);
        setParams(derivarParams(res.blocos));
      } catch (e: any) {
        setErro(e?.message ?? "Falha ao abrir página.");
        setConsulta(null);
      } finally {
        setAcao(null);
      }
    }
  }

  // Monta a lista de params do operador (na ordem da config da página), com as opções de cada
  // (estáticas `opcoes` ou dinâmicas `opcoes_fonte`) já resolvidas. Pré-preenche pares de datas
  // (início/fim) com o mês atual. As declarações (tipo/obrigatório/opções) vêm da PÁGINA publicada,
  // não de `GET /pipelines/{id}` — esse é restrito a especialista/admin e o operador levava 403.
  async function prepararExecucao(paginaId: string, cfg: ExecConfig) {
    setCarregandoParams(true);
    setErroParams("");
    try {
      const declarados = await api.parametrosDaPagina(paginaId);

      // Em paralelo: com 3 parâmetros de origem dinâmica, em série a etapa levava 3 idas ao servidor.
      const lista: ParamOperador[] = await Promise.all(
        declarados.map(async (p): Promise<ParamOperador> => {
          const decl = p.decl;
          const estaticas: Opcao[] = (decl.opcoes ?? []).map((o) => ({ valor: o, rotulo: o }));
          if (!decl.opcoes_fonte) return { nome: p.nome, rotulo: p.rotulo, decl, opcoes: estaticas };
          try {
            // origem "no" precisa do pipeline de origem p/ materializar o nó.
            const din = await api.opcoesDinamicas(decl.opcoes_fonte, cfg.pipeline_id);
            return { nome: p.nome, rotulo: p.rotulo, decl, opcoes: din };
          } catch {
            return { nome: p.nome, rotulo: p.rotulo, decl, opcoes: [], opcoesFalharam: true };
          }
        }),
      );
      setParamsOperador(lista);

      // Pré-preenche datas: um par início/fim (papel pelo nome) começa no mês atual.
      const jan = janelaMesAtual();
      const defaults: Record<string, string> = {};
      const marcados: Record<string, boolean> = {};
      for (const p of lista) {
        if (p.decl.tipo !== "data") continue;
        const papel = papelData(p.nome);
        if (papel === "inicio") defaults[p.nome] = jan.inicio;
        else if (papel === "fim") defaults[p.nome] = jan.fim;
        if (defaults[p.nome]) marcados[p.nome] = true;
      }
      setValores(defaults);
      setSugeridos(marcados);
    } catch (e: any) {
      setErro(e?.message ?? "Falha ao carregar os parâmetros do relatório.");
      setErroParams("indisponivel");
    } finally {
      setCarregandoParams(false);
    }
  }

  // Monta o objeto de parâmetros a executar a partir do que o operador informou, convertendo cada
  // valor pelo tipo declarado no pipeline. Parâmetros DERIVADOS (com `derivacao` no pipeline) NÃO
  // entram aqui — o backend os resolve automaticamente na execução.
  function montarParametros(): Record<string, unknown> {
    const out: Record<string, unknown> = {};
    for (const p of paramsOperador) {
      out[p.nome] = valorPorTipo(p.decl, (valores[p.nome] ?? "").trim());
    }
    return out;
  }

  // Espera a execução terminar e consulta a página COM ELA. Não terminou no tempo do poll →
  // guarda o id para conferir de novo (re-executar bateria nas fontes por nada).
  async function aguardarEConsultar(idExec: string) {
    if (!paginaAtual) return;
    const fim = await api.aguardarExecucao(idExec);
    if (fim?.status === "falha") {
      setPendente(null);
      setErro(
        "A execução do pipeline falhou — confira os parâmetros informados e gere novamente. " +
          `Execução ${curto(idExec)}.`,
      );
      return;
    }
    if (fim?.status === "executando") {
      setPendente(idExec);
      setErro(
        "A execução ainda está em andamento — nada foi perdido. Use “Conferir novamente” para " +
          "retomar quando ela terminar (gerar de novo faria o pipeline ler as fontes outra vez).",
      );
      return;
    }
    // Só "concluida" libera o relatório. Qualquer outro desfecho (estado novo do backend, resposta
    // sem status) NÃO pode cair no caminho de sucesso: a consulta seguinte leria o snapshot que
    // existir — possivelmente de uma execução anterior — e a tela o atribuiria a ESTA, que é
    // exatamente uma evidência falsa. Mesma regra do pill de status (`pillExecucao`).
    if (fim?.status !== "concluida") {
      setPendente(null);
      setErro(
        `A execução ${curto(idExec)} terminou num estado que esta tela não sabe interpretar` +
          `${fim?.status ? ` («${fim.status}»)` : ""} — nenhum relatório foi gerado. ` +
          "Confira em Execuções antes de gerar de novo.",
      );
      return;
    }
    setPendente(null);
    setExecId(idExec);
    setCongeladoEm(fim?.concluida_em ?? null);
    const res = (await api.consultarPagina(paginaAtual.id, null, idExec)) as ConsultaPagina;
    setConsulta(res);
    setOrdCol({});
    setExpandido({});
    setEtapa("vis");
  }

  // Executa o pipeline (poll até concluir) e consulta a página com a execução resultante.
  // É o caminho "gerar novo" (re-roda as fontes); usado direto quando não há cache do dia.
  async function executarEConsultar() {
    if (!paginaAtual || !execAtual) return;
    setMostrarCache(false);
    setAcao("gerar");
    setGerando({ desde: Date.now(), execucao: null });
    setErro("");
    setAviso("");
    setRelatorio(null);
    try {
      // Registra de qual página o operador disparou a run (origem da execução).
      const exec = await api.executarPipeline(
        execAtual.pipeline_id,
        montarParametros(),
        paginaAtual.id,
      );
      setGerando((g) => (g ? { ...g, execucao: exec.id } : g));
      await aguardarEConsultar(exec.id);
    } catch (e: any) {
      // Já existe execução em andamento com estes parâmetros (409): para o operador o relatório
      // é o mesmo, então a tela ACOMPANHA a que roda em vez de disparar outra — que a plataforma
      // recusaria de novo. É o "ver existente" do cache do dia, só que para uma ainda em voo.
      const emVoo = api.execucaoEmVoo(e);
      if (emVoo) {
        setAviso("Já havia uma execução em andamento com estes parâmetros — acompanhando essa.");
        setGerando({ desde: Date.now(), execucao: emVoo });
        try {
          await aguardarEConsultar(emVoo);
        } catch (e2: any) {
          setErro(e2?.message ?? "Falha ao gerar relatório.");
        }
        return;
      }
      setErro(e?.message ?? "Falha ao gerar relatório.");
    } finally {
      setGerando(null);
      setAcao(null);
    }
  }

  // Retoma uma execução que ainda estava rodando quando a espera estourou — sem re-executar.
  async function conferirNovamente() {
    if (!pendente) return;
    setAcao("gerar");
    setGerando({ desde: Date.now(), execucao: pendente });
    setErro("");
    try {
      await aguardarEConsultar(pendente);
    } catch (e: any) {
      setErro(e?.message ?? "Falha ao consultar o relatório.");
    } finally {
      setGerando(null);
      setAcao(null);
    }
  }

  // Reaproveita uma execução de HOJE (cache): consulta a página com ela, sem re-executar
  // (não bate de novo nas fontes). É o "ver existente / escolher do histórico do dia".
  async function verExistente(execIdReuso: string) {
    if (!paginaAtual) return;
    setMostrarCache(false);
    setAcao("gerar");
    setErro("");
    setAviso("");
    setRelatorio(null);
    try {
      setExecId(execIdReuso);
      setCongeladoEm(cacheDoDia.find((c) => c.id === execIdReuso)?.concluida_em ?? null);
      const res = (await api.consultarPagina(paginaAtual.id, null, execIdReuso)) as ConsultaPagina;
      setConsulta(res);
      setOrdCol({});
      setExpandido({});
      setEtapa("vis");
    } catch (e: any) {
      setErro(e?.message ?? "Falha ao abrir o relatório existente.");
    } finally {
      setAcao(null);
    }
  }

  // "Gerar relatório":
  //  • página parametrizada (com `execucao`): valida → procura relatório de HOJE com os mesmos
  //    parâmetros (cache do dia). Se houver, abre o modal (ver existente · histórico · gerar novo);
  //    senão, executa direto. • página simples: reconsulta com os filtros liberados.
  async function gerar() {
    if (!paginaAtual || carregando) return;
    // Parâmetros que não carregaram = escopo desconhecido: não se executa às cegas.
    if (erroParams) return;
    if (execAtual) {
      // Parâmetro obrigatório em branco é ERRO alto: ele define O QUE a execução abrange e, em
      // branco, o pipeline leria a base inteira e devolveria um resultado plausível e errado.
      const faltando = paramsOperador.find(
        (p) => p.decl.obrigatorio && !(valores[p.nome] ?? "").trim(),
      );
      if (faltando) {
        setFaltante(faltando.nome);
        setErro(
          `Informe «${faltando.rotulo}» para gerar o relatório — este parâmetro define o escopo ` +
            "da execução e não pode ficar em branco.",
        );
        document.getElementById(idCampo(faltando.nome))?.focus();
        return;
      }
      setFaltante(null);
      // Cache do dia: já existe execução concluída de hoje com estes mesmos parâmetros?
      setErro("");
      setAviso("");
      setAcao("gerar");
      try {
        const doDia = await api.execucoesDoDia(
          execAtual.pipeline_id,
          montarParametros(),
          paginaAtual.id,
        );
        if (doDia.length > 0) {
          setCacheDoDia(doDia);
          setMostrarCache(true);
          return; // aguarda a escolha do operador no modal
        }
      } catch {
        // Falha ao consultar o cache não bloqueia — segue para executar normalmente.
      } finally {
        setAcao(null);
      }
      await executarEConsultar();
    } else {
      setAcao("gerar");
      setErro("");
      setAviso("");
      setRelatorio(null);
      try {
        setExecId(null);
        setCongeladoEm(null);
        const res = (await api.consultarPagina(
          paginaAtual.id,
          filtrosEscolhidos,
        )) as ConsultaPagina;
        setConsulta(res);
        setOrdCol({});
        setExpandido({});
        setEtapa("vis");
      } catch (e: any) {
        setErro(e?.message ?? "Falha ao gerar relatório.");
      } finally {
        setAcao(null);
      }
    }
  }

  // Exporta a visão atual da página em .xlsx (não carimba) com os filtros escolhidos.
  async function exportarExcel() {
    if (!paginaAtual) return;
    setAcao("exportar");
    setErro("");
    setAviso("");
    try {
      const blob = await api.exportarPaginaXlsx(paginaAtual.id, filtrosEscolhidos, execId);
      const arquivo = nomeArquivo(consulta?.pagina ?? paginaAtual.nome);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = arquivo;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      // Confirmação explícita: o download do navegador é discreto e o operador reclicava.
      setAviso(`Excel baixado: ${arquivo}. É uma cópia da visão atual — não é o relatório carimbado.`);
    } catch (e: any) {
      setErro(e?.message ?? "Falha ao exportar Excel.");
    } finally {
      setAcao(null);
    }
  }

  // Carimba o relatório (imutável) com a cadeia de rastreio.
  async function carimbar() {
    if (!paginaAtual) return;
    setAcao("carimbar");
    setErro("");
    setAviso("");
    try {
      const rel = (await api.gerarRelatorio(paginaAtual.id, filtrosEscolhidos, execId)) as Relatorio;
      setRelatorio(rel);
    } catch (e: any) {
      setErro(e?.message ?? "Falha ao gerar relatório carimbado.");
    } finally {
      setAcao(null);
    }
  }

  abrirRef.current = abrir;

  function voltarCatalogo() {
    if (busca.has("rel")) {
      const q = new URLSearchParams(busca);
      q.delete("rel");
      setBusca(q, { replace: true });
    }
    setEtapa("catalogo");
    setConsulta(null);
    setRelatorio(null);
    setSel(-1);
    setExecId(null);
    setCongeladoEm(null);
    setPendente(null);
    setErro("");
    setAviso("");
  }

  // Resumo do que o operador informou — é por este texto que se confere antes de carimbar. Mostra
  // o RÓTULO da opção escolhida (o código interno "Operação: 1" não diz nada) e data em pt-BR;
  // parâmetro `segredo` NUNCA aparece em claro.
  const resumoParametros = useMemo(() => {
    const partes: string[] = [];
    for (const p of paramsOperador) {
      const v = (valores[p.nome] ?? "").trim();
      if (!v) continue;
      if (p.decl.segredo) {
        partes.push(`${p.rotulo}: ••••••`);
        continue;
      }
      const rotuloOpcao = p.opcoes.find((o) => o.valor === v)?.rotulo;
      // Data por extenso também aqui: é esta linha que descreve o escopo do dado congelado, e
      // "01/07/2026" x "07/01/2026" é justamente a confusão que o campo já corrigiu acima.
      const ehData = ["data", "as_of", "competencia"].includes(p.decl.tipo);
      const mostrado = rotuloOpcao ?? (ehData ? fmtDataExtenso(v) : v);
      partes.push(`${p.rotulo}: ${mostrado}`);
    }
    return partes;
  }, [paramsOperador, valores]);

  // Há valor sugerido pela plataforma ainda na tela? É o que liga a explicação nas instruções.
  const temSugestao = useMemo(
    () => paramsOperador.some((p) => sugeridos[p.nome] && (valores[p.nome] ?? "") !== ""),
    [paramsOperador, sugeridos, valores],
  );

  // Vista de cada bloco: colunas (já pivotadas quando a view define colunas), linhas ordenadas,
  // perfil por coluna e o recorte de linhas desenhado. Fica em `useMemo` porque perfilar coluna
  // percorre a amostra inteira — reperfilar a cada render de ordenação piscava a grade.
  const vistas = useMemo(() => {
    return (consulta?.blocos ?? []).map((b, i) => {
      const largo = ehLargo(b.tipo);
      const kpi = ehKpi(b.tipo);
      const base = largo
        ? { colunas: b.resultado?.colunas ?? [], linhas: b.resultado?.linhas ?? [] }
        : pivotar(b.resultado ?? { colunas: [], linhas: [] });
      const perfis = perfisDaAmostra(base.colunas, base.linhas);
      const ord = ordCol[i];
      // Bloco de DETALHE (tabela larga) mantém a ordem que veio: ali a ordem pode ser intencional
      // (ORDER BY no nó). Bloco agregado ganha ordem estável pelas dimensões de linha da view.
      const chaves = largo
        ? []
        : (b.resultado?.layout?.linhas ?? []).filter((c) => base.colunas.includes(c));
      const naturais = chaves.length > 0 ? chaves : largo ? [] : base.colunas.slice(0, 1);
      const todas =
        ord && base.colunas.includes(ord.col)
          ? ordenarLinhas(base.linhas, ord.col, ord.dir, perfis[ord.col])
          : ordemEstavel(base.linhas, naturais, perfis);
      const linhas = expandido[i] ? todas : todas.slice(0, LINHAS_POR_BLOCO);
      const colsNum = new Set<number>();
      base.colunas.forEach((c, j) => perfis[c]?.tipo === "numero" && colsNum.add(j));
      return { b, i, largo, kpi, cols: base.colunas, todas, linhas, perfis, colsNum, ord };
    });
  }, [consulta, ordCol, expandido]);

  const ordenarPor = (i: number, c: string) =>
    setOrdCol((s) => ({
      ...s,
      [i]: { col: c, dir: s[i]?.col === c && s[i]?.dir === "asc" ? "desc" : "asc" },
    }));

  // Painel de um bloco (cabeçalho colorido + contagem de linhas + grade), reusado pelas formas.
  const painel = (v: (typeof vistas)[number]) => {
    const cor = LINHA_CORES[v.i % LINHA_CORES.length];
    const titulo = v.b.titulo ?? `Bloco ${v.i + 1}`;
    const idTit = `pt-bloco-${v.i}`;
    const bv = {
      cols: v.cols,
      mini: v.linhas.map((l) => v.cols.map((c) => renderCelula((l as Record<string, unknown>)[c], v.perfis[c]))),
      kpi: kpiDoBloco(v.b.resultado ?? { colunas: [], linhas: [] }, titulo),
    };
    return (
      // `section` + nome: sem isso a grade era uma tabela anônima para o leitor de tela — e são
      // várias na mesma página.
      <section className={"mpanel" + (v.largo ? " obs-card" : "")} key={`${v.b.view_id}:${v.i}`} aria-labelledby={idTit}>
        <div className="pbh" style={{ cursor: "default", borderTop: "3px solid " + cor }}>
          <span className="vd" style={{ background: cor }} />
          <span className="pbt" id={idTit}>
            {titulo}
          </span>
          <span className="rzhint">{v.todas.length === 1 ? "1 linha" : `${num(v.todas.length)} linhas`}</span>
        </div>
        {/* Teto de altura: sem ele o cabeçalho `sticky` da grade não tem onde grudar (o scroll é
            da página) e um bloco de 200 linhas empurra as ações do relatório para fora da tela. */}
        <div className="pbbody" style={{ overflow: "auto", maxHeight: "60vh" }}>
          {v.todas.length === 0 ? (
            // Sem isto, um relatório de zero linhas mostrava apenas "Total" em branco — e o
            // operador não distinguia "nenhuma divergência" (a boa notícia de um check) de
            // "a tela quebrou".
            <p className="pb-vazio">
              Nenhuma linha para estes parâmetros — o que, num check, pode ser a boa notícia.{" "}
              <button type="button" className="lk" onClick={() => setEtapa("params")}>
                rever filtros
              </button>
            </p>
          ) : (
            <>
              <Bloco
                view={bv}
                tipo={v.largo ? "tabela" : "pivot"}
                onSort={(c) => ordenarPor(v.i, c)}
                sort={v.ord}
                total={linhaTotal(v.b.total, v.cols, v.todas, v.perfis)}
                colsNum={v.colsNum}
              />
              {v.todas.length > v.linhas.length && (
                <p className="rzhint" style={{ padding: "8px 2px 0" }}>
                  exibindo {num(v.linhas.length)} de {num(v.todas.length)} linhas ·{" "}
                  <button
                    type="button"
                    className="lk"
                    style={{ fontSize: 11 }}
                    onClick={() => setExpandido((s) => ({ ...s, [v.i]: true }))}
                  >
                    mostrar todas
                  </button>{" "}
                  · o Excel leva o conteúdo inteiro
                </p>
              )}
            </>
          )}
        </div>
      </section>
    );
  };

  // Modal "cache do dia": disparado a partir da etapa de parâmetros (gerar) e também visível na
  // visualização. Extraído num só lugar para renderizar em ambas as etapas (o `gerar` seta
  // `mostrarCache` sem trocar de etapa — se o modal só existisse na visualização, ficaria invisível).
  const modalCache = mostrarCache && (
    <div
      className="modal-fundo"
      role="dialog"
      aria-modal="true"
      aria-labelledby="pt-cache-tit"
      onClick={(e) => e.target === e.currentTarget && setMostrarCache(false)}
      onKeyDown={(e) => {
        if (e.key === "Escape") setMostrarCache(false);
      }}
    >
      <div className="modal-caixa">
        <h3 id="pt-cache-tit">Já existe relatório de hoje</h3>
        <p>
          {cacheDoDia.length === 1
            ? "Encontramos um relatório gerado hoje"
            : `Encontramos ${cacheDoDia.length} relatórios gerados hoje`}{" "}
          com estes mesmos parâmetros. Abrir um existente mostra o dado já congelado (não lê as
          fontes de novo); gerar novo re-executa o pipeline.
        </p>
        <ul className="cache-lista">
          {cacheDoDia.map((c, i) => (
            <li key={c.id}>
              <button
                autoFocus={i === 0}
                onClick={() => verExistente(c.id)}
                title={`Execução ${c.id} · congelada em ${fmtMomento(c.concluida_em)}`}
              >
                {/* Congelamento = FIM da execução: é o instante que o carimbo cita. */}
                <b>{fmtHora(c.concluida_em ?? c.iniciada_em)}</b>
                <span className="tag">
                  {c.origem === "agendado" ? "agendamento" : c.ator_nome || "manual"}
                </span>
                <span className="arr">ver →</span>
              </button>
            </li>
          ))}
        </ul>
        <div className="modal-acoes">
          <button className="btn-sec" onClick={() => setMostrarCache(false)}>
            Cancelar
          </button>
          <button className="btn-stamp" onClick={() => executarEConsultar()}>
            Gerar novo
          </button>
        </div>
      </div>
    </div>
  );

  // Faixa de erro (envelope pt-BR da API). `role="alert"` porque a validação de escopo obrigatório
  // é a mensagem mais crítica do produto — antes era texto solto com hex inline sem versão escura.
  const faixaErro = erro ? (
    <p className="exec-erro" role="alert" id="portal-erro" data-testid="portal-erro">
      {erro}
    </p>
  ) : null;

  // ---- CATÁLOGO ----
  if (etapa === "catalogo") {
    // Particiona o catálogo (uma única fonte) pelo que muda para o operador: precisa PREENCHER
    // algo ou não. Uma página pode ter `execucao` sem `operador` (o pipeline roda sem parâmetro) —
    // antes ela caía no grupo dos parametrizados e a contagem de parâmetros derrubava a tela toda.
    // Guarda o índice original (abrir(i)).
    const rotulos = (p: CatalogoItem) =>
      (p.execucao?.operador ?? []).map((o) => o.rotulo || o.param).filter(Boolean);
    const parametrizadas = visiveis.filter(({ p }) => rotulos(p).length > 0);
    const fixas = visiveis.filter(({ p }) => rotulos(p).length === 0);
    const nenhumaVisivel = visiveis.length === 0;

    // `GET /relatorios` devolve o NOME da página, não o id: a atribuição só é confiável quando o
    // nome é único no catálogo. Com nome repetido não atribui — mostrar o carimbo da página errada
    // numa tela de evidência é pior do que não mostrar carimbo nenhum.
    const repetido = new Map<string, number>();
    for (const p of catalogo) repetido.set(p.nome, (repetido.get(p.nome) ?? 0) + 1);
    const ultimoCarimbo = new Map<string, api.RelatorioResumo>();
    for (const r of carimbos) {
      // A API já devolve do mais recente para o mais antigo — o primeiro de cada nome é o último.
      if (!r.pagina || repetido.get(r.pagina) !== 1 || ultimoCarimbo.has(r.pagina)) continue;
      ultimoCarimbo.set(r.pagina, r);
    }

    const celulaCarimbo = (p: CatalogoItem) => {
      // Carregando e indisponível NÃO podem sair como "nunca": um é "ainda não sabemos", o outro é
      // "não conseguimos saber", e só o terceiro é o fato.
      if (carimbosEstado === "carregando")
        return (
          <span style={CARIMBO_FRACO} title="consultando os relatórios já carimbados">
            carregando…
          </span>
        );
      if (carimbosEstado === "erro")
        return (
          <span style={CARIMBO_FRACO} title={carimbosErro}>
            não disponível
          </span>
        );
      const rel = ultimoCarimbo.get(p.nome);
      // "nunca" é DADO (a página nunca foi carimbada), não decoração: fica em `--ink-2` — em
      // `--ink-3` o contraste sobre o branco não chega a 4,5:1 e a informação que decide se o
      // relatório de hoje já saiu era a mais apagada da linha. O itálico segue distinguindo
      // "não houve" de um instante.
      if (!rel?.criado_em)
        return (
          <span style={CARIMBO_FRACO} title="nenhum relatório desta página foi carimbado até agora">
            nunca
          </span>
        );
      return (
        <span title={`${fmtMomento(rel.criado_em)}${rel.criado_por ? ` · por ${rel.criado_por}` : ""}`}>
          {quandoCarimbo(rel.criado_em)}
          {rel.criado_por && (
            <span style={{ display: "block", fontSize: 11, color: "var(--ink-2)" }}>
              por {rel.criado_por}
            </span>
          )}
        </span>
      );
    };

    // Uma linha do catálogo. A ação principal deixa de existir só no hover: vira botão numa coluna
    // de ações. O nome acessível do botão NÃO repete o da página (ele descreve a linha por
    // `aria-describedby`) — dois controles com o mesmo nome tornariam a linha ambígua para quem
    // navega por lista de botões, no leitor de tela e no teste.
    const linha = (p: CatalogoItem, i: number, colunaMeio: ReactNode, testid?: string) => {
      const idNome = `pt-cat-${p.id}`;
      // O botão do catálogo ABRE o relatório — quem gera é o botão da tela seguinte. Rotulá-lo
      // "Gerar relatório" prometia execução num clique e ainda punha duas ações de mesmo nome em
      // etapas diferentes do mesmo fluxo.
      const rotuloAcao = p.execucao && rotulos(p).length > 0 ? "Informar filtros" : "Abrir relatório";
      return (
        <tr key={p.id} data-testid={testid} className="clic" onClick={() => abrir(i)}>
          <td className="dg-nome" id={idNome}>
            {/* Sublinhado permanente: `.dg-link` só sublinha no :hover, e um nome que só se revela
                clicável quando o mouse passa por cima não existe para quem navega por teclado. */}
            <button
              className="dg-link"
              style={{ textDecoration: "underline", textUnderlineOffset: 3 }}
              onClick={(e) => { e.stopPropagation(); abrir(i); }}
            >
              {p.nome}
            </button>
          </td>
          <td>{colunaMeio}</td>
          <td>{celulaCarimbo(p)}</td>
          <td className="dg-acts" onClick={(e) => e.stopPropagation()}>
            <button aria-describedby={idNome} onClick={() => abrir(i)}>
              {rotuloAcao}
            </button>
          </td>
        </tr>
      );
    };

    const cabecalho = (rotuloMeio: string) => (
      <thead>
        <tr>
          <th scope="col">Nome</th>
          <th scope="col">{rotuloMeio}</th>
          <th scope="col">Último relatório carimbado (GMT-3)</th>
          <th className="dg-acth" scope="col">
            Ações
          </th>
        </tr>
      </thead>
    );

    return (
      <>
        <div className="page-head">
          <h1 className="display">Portal</h1>
          <p className="sub">
            Relatórios prontos, publicados pelo especialista. Cada número vem de uma medida do cubo
            e é a foto de um instante — não é consulta ao vivo.
          </p>
        </div>
        <div className="pad">
          <div className="ro-banner">
            <span className="ro">read-only</span>
            <span>Perfil operador — você escolhe os filtros liberados e gera o relatório.</span>
          </div>
          {erroCatalogo && (
            <p className="exec-erro" role="alert" data-testid="portal-erro-catalogo">
              {erroCatalogo}
            </p>
          )}
          {faixaErro}
          {/* Falha PARCIAL dita como falha parcial: o catálogo está completo, uma coluna não. Sem
              esta linha o operador só teria as células "não disponível" para adivinhar o motivo. */}
          {carimbosEstado === "erro" && !carregandoCatalogo && (
            <p role="status" style={{ margin: "0 0 10px", fontSize: 12, color: "var(--ink-2)" }}>
              A coluna “Último relatório carimbado” não carregou ({carimbosErro}). O catálogo abaixo
              está completo — só o histórico de carimbos ficou indisponível.
            </p>
          )}
          {carregandoCatalogo ? (
            <p className="sub">Carregando o catálogo…</p>
          ) : nenhumaVisivel && !erroCatalogo ? (
            <div className="card lc-empty">
              <div className="big">Nenhum relatório publicado para você</div>
              <div>
                O especialista publica as páginas no Studio; elas aparecem aqui automaticamente.
              </div>
            </div>
          ) : (
            <>
              {parametrizadas.length > 0 && (
                <>
                  <div className="pt-secao" style={{ margin: "6px 0 10px" }}>
                    Relatórios · você informa os filtros
                  </div>
                  <table
                    className="dgrid"
                    aria-label="Relatórios em que você informa os filtros"
                    style={{ marginBottom: fixas.length > 0 ? 18 : 0 }}
                  >
                    {/* Os RÓTULOS dos filtros, não a contagem: "3 parâmetros" não ajuda a escolher. */}
                    {cabecalho("Filtros a informar")}
                    <tbody>
                      {parametrizadas.map(({ p, i }) =>
                        linha(p, i, rotulos(p).join(" · "), "check-card"),
                      )}
                    </tbody>
                  </table>
                </>
              )}
              {fixas.length > 0 && (
                <>
                  <div
                    className="pt-secao"
                    style={{ margin: parametrizadas.length > 0 ? "18px 0 10px" : "6px 0 10px" }}
                  >
                    Relatórios · sem filtros a informar
                  </div>
                  <table className="dgrid" aria-label="Relatórios sem filtros a informar">
                    {cabecalho("Tipo")}
                    <tbody>
                      {fixas.map(({ p, i }) =>
                        linha(
                          p,
                          i,
                          <span className="ro">
                            {p.execucao ? "roda sem parâmetros" : "relatório fixo"}
                          </span>,
                        ),
                      )}
                    </tbody>
                  </table>
                </>
              )}
            </>
          )}
        </div>
      </>
    );
  }

  // ---- PARÂMETROS ----
  if (etapa === "params") {
    // O trilho anda dentro do que este usuário VÊ (não sobre o catálogo cru): as setas ‹ › não
    // podem entregar uma página que o catálogo escondeu.
    // Esmaecimento só do lado que tem mais fila (nas pontas não fica véu sobre pill inteiro).
    const mascaraTrilho =
      maisAbas.esq && maisAbas.dir
        ? "linear-gradient(90deg, transparent, #000 20px, #000 calc(100% - 20px), transparent)"
        : maisAbas.dir
          ? "linear-gradient(90deg, #000 calc(100% - 20px), transparent)"
          : maisAbas.esq
            ? "linear-gradient(90deg, transparent, #000 20px)"
            : undefined;
    // Seta desabilitada precisa PARECER desabilitada (`.rail-arrow` não tem estado próprio), e com
    // a fila inteira à vista as duas somem sem tirar o espaço — piscar o layout a cada resize seria
    // pior. `visibility` (não `display`) mantém as pontas no lugar.
    const setaEstilo = (ativa: boolean): React.CSSProperties => ({
      visibility: maisAbas.esq || maisAbas.dir ? "visible" : "hidden",
      opacity: ativa ? 1 : 0.35,
      cursor: ativa ? "pointer" : "default",
    });
    return (
      <>
        <div className="page-head">
          {/* `paddingRight:0` neutraliza os 38px que o CSS reserva no trilho: eles vinham de quando
              o próprio trilho rolava com a seta sobreposta, e agora só desalinhavam o "›" da borda
              direita dos cartões abaixo. Ver o CSS pedido no relatório. */}
          <div
            className="report-rail"
            role="group"
            aria-label="Trocar de relatório"
            style={{ paddingRight: 0 }}
          >
            <button
              className="rail-arrow"
              aria-label="Mostrar relatórios à esquerda"
              onClick={() => rolarTrilho(-1)}
              disabled={!maisAbas.esq}
              style={setaEstilo(maisAbas.esq)}
            >
              ‹
            </button>
            {/* A fila rola DENTRO do trilho (as setas ficam sempre alcançáveis nas pontas). O
                padding vertical existe para o anel de foco do pill não ser recortado pelo
                `overflow` — ver o CSS equivalente no relatório. */}
            <div
              ref={trilhoRef}
              className="report-rail-fila"
              onScroll={medirTrilho}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 10,
                flex: 1,
                minWidth: 0,
                overflowX: "auto",
                padding: "3px 2px 6px",
                // O navegador rola o pill focado até a BORDA da fila, justamente onde o
                // esmaecimento come 20px — o anel de foco chegava desbotado. A margem de rolagem
                // vive no elemento que rola (o CSS a tinha no trilho, que não rola mais).
                scrollPaddingInline: 24,
                maskImage: mascaraTrilho,
                WebkitMaskImage: mascaraTrilho,
              }}
            >
              {visiveis.map(({ p, i }) => (
                <button
                  key={p.id}
                  ref={i === sel ? abaAtivaRef : undefined}
                  // O "ativo" existia só em cor: sem estado programático o leitor de tela não
                  // anunciava qual relatório está aberto.
                  aria-current={i === sel || undefined}
                  className={"report-tab" + (i === sel ? " on" : "")}
                  onClick={() => abrir(i)}
                >
                  {p.nome}
                </button>
              ))}
            </div>
            <button
              className="rail-arrow"
              aria-label="Mostrar relatórios à direita"
              onClick={() => rolarTrilho(1)}
              disabled={!maisAbas.dir}
              style={setaEstilo(maisAbas.dir)}
            >
              ›
            </button>
          </div>
          <h1 className="display">{paginaAtual?.nome}</h1>
          <p className="sub">
            {/* Não pedir "preencha os filtros" a quem não tem filtro para preencher. */}
            {!execAtual
              ? "Selecione os parâmetros liberados pelo especialista e gere o relatório. Você não edita o modelo — só consome."
              : !carregandoParams && !erroParams && paramsOperador.length === 0
                ? "Este relatório roda sem filtros: gerar executa o pipeline e congela o dado nesse instante."
                : "Preencha os filtros e gere o relatório: o pipeline roda com estes parâmetros e o dado é congelado nesse instante. Os valores derivados são resolvidos automaticamente."}
          </p>
        </div>
        <div className="pad">
          <button className="link-back" onClick={voltarCatalogo}>
            ← Catálogo
          </button>
          <div className="ro-banner">
            <span className="ro">read-only</span>
            <span>Perfil operador — parâmetros liberados pelo especialista.</span>
          </div>
          {faixaErro}
          {gerando && (
            <div className="note-line" role="status" aria-live="polite" data-testid="portal-gerando">
              <span className="statuspill exec">
                <span className="d" />
                em execução
              </span>
              <span>
                {gerando.execucao
                  ? `Executando o pipeline sobre as fontes · execução ${curto(gerando.execucao)}`
                  : "Iniciando a execução"}{" "}
                · <b className="num">{fmtDuracaoMs(agora - gerando.desde)}</b> — o relatório abre
                sozinho quando terminar.
              </span>
            </div>
          )}
          <div className="params-grid">
            {/* Formulário de verdade: Enter num campo gera o relatório (é uma tela de digitação).
                `noValidate`: com uma data digitada pela metade o navegador BLOQUEIA o submit e
                mostra a própria bolha, no idioma dele — nosso handler nem roda, e a recusa por
                escopo em branco (a mensagem mais importante do produto) não aparece. A validação é
                a nossa, em pt-BR, no envelope da tela. */}
            <form
              noValidate
              onSubmit={(e) => {
                e.preventDefault();
                void gerar();
              }}
            >
              <div className="ptable">
                {execAtual ? (
                  <>
                    {/* Filtros do operador: cada um é um parâmetro DECLARADO no pipeline. O widget
                        vem do `tipo` do parâmetro; opções (estáticas/dinâmicas) vêm do pipeline. */}
                    {carregandoParams && paramsOperador.length === 0 && (
                      <div className="prow">
                        <div className="plabel" style={ROTULO_CAMPO}>
                          Parâmetros
                        </div>
                        <div className="pval ro">Carregando…</div>
                      </div>
                    )}
                    {/* "Falhou" não pode se passar por "não pede nada": são estados diferentes e
                        levam a decisões opostas (um gera o relatório, o outro proíbe gerar). */}
                    {!carregandoParams && erroParams && (
                      <div className="prow">
                        <div className="plabel" style={ROTULO_CAMPO}>
                          Parâmetros
                        </div>
                        <div className="pval ro">
                          Não carregaram — não é possível saber o que este relatório pede, então
                          gerar está bloqueado. Recarregue a tela; se persistir, fale com o
                          especialista.
                        </div>
                      </div>
                    )}
                    {!carregandoParams && !erroParams && paramsOperador.length === 0 && (
                      <div className="prow">
                        <div className="plabel" style={ROTULO_CAMPO}>
                          Parâmetros
                        </div>
                        <div className="pval ro">
                          Este relatório roda sem parâmetros — clique em Gerar relatório.
                        </div>
                      </div>
                    )}
                    {paramsOperador.map((p) => {
                      const val = valores[p.nome] ?? "";
                      const setVal = (v: string) => {
                        setValores((s) => ({ ...s, [p.nome]: v }));
                        // A partir do 1º toque o valor é ESCOLHA do operador, não sugestão nossa.
                        setSugeridos((s) => (s[p.nome] ? { ...s, [p.nome]: false } : s));
                        if (faltante === p.nome) setFaltante(null);
                      };
                      const obrig = !!p.decl.obrigatorio;
                      const invalido = faltante === p.nome;
                      const id = idCampo(p.nome);
                      const idDica = `${id}-dica`;
                      // Dica do campo: só o que é PRÓPRIO dele. A frase do obrigatório ("define o
                      // escopo da execução — não pode ficar em branco") aparecia embaixo de CADA
                      // campo com `*`: três cópias idênticas na mesma tela, mais a das instruções e
                      // a da faixa de erro. O que o campo obrigatório precisa dizer, ele diz pelo
                      // `*`, pelo filete do rótulo e pelo `aria-required`; a regra completa fica nas
                      // instruções (uma vez) e a recusa aparece no próprio campo quando falta.
                      const dica = [
                        p.decl.descricao,
                        p.decl.segredo
                          ? "usada só nesta execução; fica redigida no histórico"
                          : obrig
                            ? null
                            : "em branco = sem filtro",
                        invalido ? "informe este parâmetro — ele define o escopo da execução" : null,
                        p.opcoesFalharam
                          ? "⚠ a lista de opções não carregou; informe o valor exato ou reabra o relatório"
                          : null,
                      ]
                        .filter(Boolean)
                        .join(" · ");
                      // Data escolhida, escrita por extenso, ACIMA do campo. `<input type="date">`
                      // desenha no locale do NAVEGADOR — o `lang="pt-BR"` do documento não manda
                      // nisso —, então num navegador em inglês 1º de julho aparece "07/01/2026" e é
                      // lido como 7 de janeiro. Nesta tela isso vira relatório carimbado do período
                      // errado, em silêncio. Por isso a leitura vem ANTES: o formato ambíguo nunca é
                      // a primeira coisa que se lê. O controle continua sendo o nativo (calendário,
                      // teclado, valor "YYYY-MM-DD" intacto); o que muda é a ordem da CONFERÊNCIA.
                      const ehData =
                        p.decl.tipo === "data" || p.decl.tipo === "as_of" || p.decl.tipo === "competencia";
                      const lida = ehData && val ? fmtDataExtenso(val) : null;
                      // O widget nativo (`date`/`month`) é o que desenha no locale do navegador: só
                      // nele a linha existe também EM BRANCO, para o campo não pular de lugar quando
                      // a data chega. Parâmetro de data com lista de opções não tem a ambiguidade.
                      const nativoData = ehData && !p.decl.segredo && p.opcoes.length === 0;
                      const mostrarLida = nativoData || !!lida;
                      const idLida = `${id}-lida`;
                      const comum = {
                        id,
                        "data-testid": `param-${p.nome}`,
                        "aria-required": obrig || undefined,
                        "aria-invalid": invalido || undefined,
                        // Campo de data referencia a leitura SEMPRE (ela também existe em branco):
                        // trocar o `describedby` conforme o valor mudaria a descrição do campo no
                        // meio do preenchimento. O da dica só entra quando existe dica — apontar
                        // para um elemento vazio faz o leitor de tela anunciar silêncio.
                        "aria-describedby":
                          [mostrarLida ? idLida : null, dica ? idDica : null]
                            .filter(Boolean)
                            .join(" ") || undefined,
                      } as const;
                      return (
                        <div className="prow" key={p.nome}>
                          <label className="plabel" htmlFor={id} style={obrig ? ROTULO_OBRIG : ROTULO_CAMPO}>
                            {p.rotulo}
                            {obrig ? (
                              <span className="req" aria-hidden="true">
                                {" "}
                                *
                              </span>
                            ) : null}
                          </label>
                          <div className="pval">
                            {/* `flex:1` inline porque `.pval` é flex e o campo empilhado (input +
                                dica) precisa ocupar a célula inteira — ver o snippet de CSS. */}
                            <div className="exec-param" style={{ flex: 1, minWidth: 0 }}>
                              {mostrarLida && (
                                <span
                                  id={idLida}
                                  style={lida ? DATA_LIDA : DATA_LIDA_VAZIA}
                                  data-testid={`param-lida-${p.nome}`}
                                >
                                  {p.decl.tipo === "competencia"
                                    ? lida
                                      ? `Competência: ${lida}`
                                      : "Competência: nenhum mês escolhido"
                                    : lida
                                      ? `Data: ${lida}${p.decl.tipo === "as_of" ? " (até o fim do dia)" : ""}`
                                      : "Data: nenhuma escolhida"}
                                  {/* Valor que a plataforma preencheu não pode se passar por escolha
                                      de quem vai carimbar o relatório — e o fim da janela sugerida é
                                      o 1º dia do mês seguinte, que sem marca lê como outro mês. */}
                                  {sugeridos[p.nome] && val ? (
                                    <span style={SUGERIDO} data-testid={`param-sugerido-${p.nome}`}>
                                      {" "}
                                      · sugerido
                                    </span>
                                  ) : null}
                                </span>
                              )}
                              {p.decl.segredo ? (
                                // Credencial: nunca em claro na tela. O valor é usado na execução e
                                // REDIGIDO pelo backend no histórico e na trilha de auditoria.
                                <input
                                  {...comum}
                                  type="password"
                                  autoComplete="new-password"
                                  value={val}
                                  placeholder="Credencial"
                                  onChange={(e) => setVal(e.target.value)}
                                />
                              ) : p.opcoes.length > 0 ? (
                                <select
                                  {...comum}
                                  value={val}
                                  disabled={carregandoParams}
                                  onChange={(e) => setVal(e.target.value)}
                                >
                                  {/* Opcional: vazio é "sem filtro" (a plataforma trata "" como sem
                                      valor); obrigatório: vazio não é escolha válida. */}
                                  <option value="">
                                    {carregandoParams ? "Carregando…" : obrig ? "Selecione" : "Todos"}
                                  </option>
                                  {p.opcoes.map((op) => (
                                    <option key={op.valor} value={op.valor}>
                                      {op.rotulo}
                                    </option>
                                  ))}
                                </select>
                              ) : p.decl.tipo === "data" || p.decl.tipo === "as_of" ? (
                                // as_of = corte no tempo (system time); seleciona uma DATA (resolve
                                // p/ o fim do dia no backend). Mesmo seletor da data comum.
                                <input {...comum} type="date" value={val} onChange={(e) => setVal(e.target.value)} />
                              ) : p.decl.tipo === "competencia" ? (
                                // competência = mês de negócio (valid time); seletor de MÊS → "YYYY-MM",
                                // que o backend expande em :{nome}_inicio / :{nome}_fim.
                                <input {...comum} type="month" value={val} onChange={(e) => setVal(e.target.value)} />
                              ) : p.decl.tipo === "inteiro" ? (
                                <input
                                  {...comum}
                                  type="number"
                                  value={val}
                                  placeholder="Número"
                                  onChange={(e) => setVal(e.target.value)}
                                />
                              ) : (
                                <input
                                  {...comum}
                                  value={val}
                                  placeholder="Digite o valor"
                                  onChange={(e) => setVal(e.target.value)}
                                />
                              )}
                              {dica && (
                                <span className="exec-param-opc" id={idDica}>
                                  {dica}
                                </span>
                              )}
                            </div>
                          </div>
                        </div>
                      );
                    })}
                  </>
                ) : params.length === 0 ? (
                  <div className="prow">
                    <div className="plabel" style={ROTULO_CAMPO}>
                      Parâmetros
                    </div>
                    <div className="pval ro">Nenhum filtro liberado — relatório fixo.</div>
                  </div>
                ) : (
                  params.map((p) => {
                    const id = idCampo(p.dimensao);
                    return (
                      <div className="prow" key={p.dimensao}>
                        <label className="plabel" htmlFor={id} style={ROTULO_CAMPO}>
                          {p.dimensao}
                        </label>
                        <div className="pval">
                          <div className="exec-param" style={{ flex: 1, minWidth: 0 }}>
                            <select
                              id={id}
                              data-testid={`param-${p.dimensao}`}
                              aria-describedby={`${id}-dica`}
                              value={escolhas[p.dimensao] ?? TODOS}
                              onChange={(e) =>
                                setEscolhas((s) => ({ ...s, [p.dimensao]: e.target.value }))
                              }
                            >
                              <option value={TODOS}>Todos</option>
                              {p.valores.map((v) => (
                                <option key={v} value={v}>
                                  {v}
                                </option>
                              ))}
                            </select>
                            {/* "Todos" é o padrão: dizer o que ele faz evita o operador achar que
                                deixou de escolher algo. */}
                            <span className="exec-param-opc" id={`${id}-dica`}>
                              em «Todos» esta dimensão não é filtrada
                            </span>
                          </div>
                        </div>
                      </div>
                    );
                  })
                )}
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 16, flexWrap: "wrap" }}>
                <button
                  className="btn-stamp"
                  type="submit"
                  disabled={carregando || !!erroParams}
                  title={
                    erroParams
                      ? "Os parâmetros deste relatório não carregaram — sem eles a execução não tem escopo definido."
                      : undefined
                  }
                >
                  <Icon name="run" cls="ic" /> {acao === "gerar" ? "Gerando…" : "Gerar relatório"}
                </button>
                {pendente && (
                  <button
                    type="button"
                    className="pag-btn"
                    onClick={() => void conferirNovamente()}
                    disabled={carregando}
                  >
                    Conferir novamente
                  </button>
                )}
              </div>
            </form>
            <div className="instr">
              <h4>Instruções · preenchimento</h4>
              <ul>
                {execAtual ? (
                  <>
                    <li>
                      <span className="ar">→</span>
                      <span>
                        Campos com <b>*</b> definem o <b>escopo</b> da execução (operação, período).
                        Em branco, o relatório não é gerado — em vez de trazer a base inteira.
                      </span>
                    </li>
                    <li>
                      <span className="ar">→</span>
                      <span>
                        Campo opcional em branco significa <b>sem filtro</b> para aquele parâmetro.
                      </span>
                    </li>
                    {/* Explica UMA vez de onde vem a data marcada "sugerido" — inclusive por que o
                        fim cai no 1º dia do mês seguinte, que era a leitura mais confusa da tela. */}
                    {temSugestao && (
                      <li>
                        <span className="ar">→</span>
                        <span>
                          As datas marcadas <b>sugerido</b> foram preenchidas por nós com o mês
                          atual: início no dia 1º e fim no dia 1º do mês seguinte — a janela sugerida
                          cobre o mês inteiro e o dia do fim não entra. Confira antes de gerar.
                        </span>
                      </li>
                    )}
                    <li>
                      <span className="ar">→</span>
                      <span>
                        <b>Gerar relatório</b> executa o pipeline e congela o dado. Se já existir um
                        relatório de hoje com os mesmos parâmetros, você pode <b>reaproveitá-lo</b>
                        {" "}sem reprocessar.
                      </span>
                    </li>
                    <li>
                      <span className="ar">→</span>
                      <span>
                        Depois você pode <b>carimbar</b> um relatório imutável com a cadeia de
                        rastreio até o nó de origem.
                      </span>
                    </li>
                  </>
                ) : (
                  <>
                    <li>
                      <span className="ar">→</span>
                      <span>
                        Escolha os <b>parâmetros liberados</b> nos menus — só esses estão abertos a você.
                      </span>
                    </li>
                    <li>
                      <span className="ar">→</span>
                      <span>
                        Deixe em <b>Todos</b> para não filtrar a dimensão.
                      </span>
                    </li>
                    <li>
                      <span className="ar">→</span>
                      <span>
                        <b>Gerar relatório</b> mostra o resultado sobre o <b>snapshot</b> da execução
                        — não é consulta ao vivo.
                      </span>
                    </li>
                    <li>
                      <span className="ar">→</span>
                      <span>
                        Depois você pode <b>carimbar</b> um relatório imutável com a cadeia de rastreio.
                      </span>
                    </li>
                  </>
                )}
              </ul>
            </div>
          </div>
        </div>
        {modalCache}
      </>
    );
  }

  // ---- VISUALIZAÇÃO ----
  const pivots = vistas.filter((v) => !v.largo && !v.kpi);
  const kpis = vistas.filter((v) => v.kpi);
  const largos = vistas.filter((v) => v.largo);
  const totalLinhas = vistas.reduce((s, v) => s + v.todas.length, 0);

  return (
    <>
      <div className="page-head">
        <button className="link-back" onClick={() => setEtapa("params")}>
          ← Parâmetros
        </button>
        <h1 className="display">{consulta?.pagina}</h1>
        <p className="sub">
          {execAtual
            ? resumoParametros.length > 0
              ? resumoParametros.join(" · ")
              : "Executado sem parâmetros."
            : filtrosEscolhidos.length
              ? filtrosEscolhidos.map((f) => `${f.dimensao}: ${celulaDado(f.valor).texto}`).join(" · ")
              : "Sem filtros — visão completa."}
          {consulta ? ` · página v${consulta.versao}` : ""}
        </p>
      </div>
      <div className="pad">
        <button className="link-back" onClick={voltarCatalogo}>
          ← Catálogo
        </button>

        {/* Carimbo do dado ANTES de qualquer número: sem isto a tela mostrava valores sem dizer de
            quando são, e um relatório imutável que parece dado ao vivo é pior que nenhum. */}
        <div className="note-line" data-testid="portal-carimbo">
          <span>
            {origens.length > 1 && !mesmoDisparo ? (
              <>
                Blocos congelados em <b className="num">{origens.length}</b> execuções diferentes —
                cada bloco cita a sua no rastreio do carimbo. Não é consulta ao vivo.
              </>
            ) : congeladoEm ? (
              <>
                Dado congelado em <b className="num">{fmtMomento(congeladoEm)}</b>
                {execExibida && (
                  <>
                    {" "}
                    · execução <b className="num">{curto(execExibida)}</b>
                  </>
                )}
                {mesmoDisparo && origens.length > 1 && (
                  <>
                    {" "}
                    · disparo com <b className="num">{origens.length}</b> execuções encadeadas
                    (dependências rodaram juntas — cada bloco cita a sua no rastreio)
                  </>
                )}{" "}
                · não é consulta ao vivo: recarregar a tela não muda estes números.
              </>
            ) : execExibida ? (
              <>
                Dado do snapshot da execução <b className="num">{curto(execExibida)}</b> — o instante
                do congelamento não está disponível. Não é consulta ao vivo.
              </>
            ) : (
              <>Dado do snapshot da última execução concluída. Não é consulta ao vivo.</>
            )}
          </span>
        </div>

        {forasDoDisparo.length > 0 && (
          <div className="note-line" role="status" data-testid="portal-fora-disparo">
            <span>
              Atenção: {forasDoDisparo.length === 1 ? "o bloco" : "os blocos"}{" "}
              <b>{forasDoDisparo.join(", ")}</b>{" "}
              {forasDoDisparo.length === 1 ? "veio" : "vieram"} da última execução concluída do
              próprio pipeline — fora deste disparo. O rastreio do carimbo cita a execução exata de
              cada bloco.
            </span>
          </div>
        )}

        {faixaErro}
        {aviso && (
          <p className="pag-info" role="status" style={{ margin: "0 0 10px" }}>
            {aviso}
          </p>
        )}

        {/* Render DIRIGIDO PELA CONFIG: cada bloco renderiza pelo seu `tipo` (pivot compacto /
            detalhe-observações em tabela larga / kpi), não por layout fixo. */}
        {vistas.length === 0 ? (
          <p className="pb-vazio">Esta página não tem blocos publicados — fale com o especialista.</p>
        ) : (
          <>
            {kpis.length > 0 && (
              // Linha de cartões com largura de cartão (não grade elástica): um KPI só, num grid
              // auto-fit, esticava por 1180px e virava um número de 30px perdido no vazio.
              <div style={{ display: "flex", gap: 14, flexWrap: "wrap", marginBottom: 14 }}>
                {kpis.map((v) => (
                  <div
                    className="mpanel"
                    style={{ flex: "0 1 300px", maxWidth: 380 }}
                    key={`kpi:${v.b.view_id}:${v.i}`}
                  >
                    <div
                      className="pbh"
                      style={{
                        cursor: "default",
                        borderTop: "3px solid " + LINHA_CORES[v.i % LINHA_CORES.length],
                      }}
                    >
                      <span className="vd" style={{ background: LINHA_CORES[v.i % LINHA_CORES.length] }} />
                      <span className="pbt">{v.b.titulo ?? `Bloco ${v.i + 1}`}</span>
                    </div>
                    <div className="pbbody">
                      <Bloco
                        tipo="valor"
                        view={{
                          cols: [],
                          mini: [],
                          kpi: kpiDoBloco(v.b.resultado ?? { colunas: [], linhas: [] }, v.b.titulo ?? "Total"),
                        }}
                      />
                    </div>
                  </div>
                ))}
              </div>
            )}
            {pivots.length > 0 && <div className="vis-grid">{pivots.map((v) => painel(v))}</div>}
            {largos.map((v) => painel(v))}
          </>
        )}

        <div style={{ display: "flex", gap: 10, marginTop: 16, flexWrap: "wrap", alignItems: "center" }}>
          <button
            className={relatorio ? "pag-btn" : "btn-stamp"}
            onClick={carimbar}
            disabled={carregando}
            data-testid="btn-carimbar"
          >
            <Icon name="stamp" cls="ic" />{" "}
            {acao === "carimbar"
              ? "Carimbando…"
              : relatorio
                ? "Gerar relatório carimbado novamente"
                : "Gerar relatório carimbado"}
          </button>
          <button
            className="pag-btn"
            data-testid="btn-exportar-excel"
            onClick={exportarExcel}
            disabled={carregando}
          >
            <Icon name="download" cls="ic" /> {acao === "exportar" ? "Exportando…" : "Exportar Excel"}
          </button>
          <span className="pag-info">
            {num(totalLinhas)} {totalLinhas === 1 ? "linha" : "linhas"} em {vistas.length}{" "}
            {vistas.length === 1 ? "bloco" : "blocos"}
          </span>
        </div>

        {relatorio && (
          <div className="trace-card" ref={traceRef} tabIndex={-1}>
            <div
              style={{
                display: "flex",
                alignItems: "baseline",
                justifyContent: "space-between",
                gap: 12,
              }}
            >
              <h4 className="display">
                Relatório carimbado · {relatorio.pagina ?? consulta?.pagina}
              </h4>
              <span className="immut">imutável</span>
            </div>
            <div style={{ fontSize: 12, color: "var(--ink-2)" }}>
              Parâmetros + dado congelados; rastreio do número até o nó de origem · id{" "}
              {curto(relatorio.id)}
            </div>
            <div className="trace-chain">
              <span className="step">relatório {curto(relatorio.id)}</span>
              <span className="arr">→</span>
              <span className="step">
                página v{relatorio.versao ?? consulta?.versao}
              </span>
              <span className="arr">→</span>
              {(relatorio.trace ?? []).map((t, i) => (
                <Fragment key={i}>
                  <span className="step">view {curto(t.view_id)}</span>
                  <span className="arr">→</span>
                  <span className="step">cubo</span>
                  <span className="arr">→</span>
                  <span className="step">execução {curto(t.origem?.execucao_id)}</span>
                  <span className="arr">→</span>
                  <span className="step term">nó {t.origem?.no_id ?? "—"} · snapshot Parquet</span>
                  {i < (relatorio.trace?.length ?? 0) - 1 && <span className="arr">·</span>}
                </Fragment>
              ))}
            </div>
            <div className="trace-foot">
              <span>
                Gerado no <b>Portal</b> (perfil operador)
              </span>
              <span>
                Blocos <b className="num">{vistas.length}</b>
              </span>
              {congeladoEm && (
                <span>
                  Dado congelado em <b className="num">{fmtMomento(congeladoEm)}</b>
                </span>
              )}
              {execExibida && (
                <span>
                  Execução <b className="num">{curto(execExibida)}</b>
                </span>
              )}
              <span>Consulte-o depois em Relatórios — o carimbo não muda.</span>
            </div>
          </div>
        )}
      </div>
      {modalCache}
    </>
  );
}
