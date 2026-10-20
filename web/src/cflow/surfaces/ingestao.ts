// Descrição de uma FONTE DE INGESTÃO para a aba Bronze — lógica PURA, sem React.
//
// Vive num módulo próprio (como `camadas.ts`) para rodar sem navegador e sem banco:
// `scripts/teste-ingestao.mjs`. O que está aqui é o vocabulário que o usuário lê, e é justamente a
// parte em que errar não estoura — só imprime a coisa errada na tela.
//
// Fonte de ingestão NÃO é pipeline: não tem grafo, não tem versão, não tem editor. O que ela tem é
// um contrato declarativo (admin, por API/script), uma cadência e um histórico de execuções. Este
// módulo só descreve; quem monta a linha é `FontesIngestao.tsx`.
import type { FonteLake } from "../../api/client";
import { diaSP, fmtHora, num, pillExecucao, rotuloDia } from "../execucao/formato";
import { ABA_TODAS, abaValida } from "./tipos-conexao";

export { ABA_TODAS, abaValida };

// Classe de ACESSO da fonte, não a fonte em si. O valor cru (`http_file`) é vocabulário
// interno; a tela mostra pt-BR. Classe desconhecida devolve o valor CRU em vez de um rótulo
// inventado — quando o backend ganhar uma classe nova, a tela mostra o nome dela, não "outro".
const ROTULO_CLASSE: Record<string, string> = {
  http_file: "arquivo publicado",
  rest_api: "API REST",
  odata: "OData",
  portal_download: "download de portal",
};

export function rotuloClasse(classe: string | null | undefined): string {
  const c = (classe ?? "").trim();
  if (!c) return "classe não informada";
  return ROTULO_CLASSE[c] ?? c;
}

// Cadência do agendamento da fonte (`ingestao_fontes.cadencia`). `manual` é o default do banco e
// significa que NADA dispara sozinho — dizer só "manual" deixaria a pessoa esperando por um job que
// não existe.
const ROTULO_CADENCIA: Record<string, string> = {
  manual: "só manual",
  diaria: "diária",
  semanal: "semanal",
  mensal: "mensal",
  anual: "anual",
};

export function rotuloCadencia(cadencia: string | null | undefined): string {
  const c = (cadencia ?? "").trim();
  if (!c) return "cadência não informada";
  return ROTULO_CADENCIA[c] ?? c;
}

// Sub-linha da fonte: dataset (o nome pelo qual o pipeline a referencia), classe de acesso,
// cadência e até onde o incremental chegou. O dataset vem primeiro porque é a chave que aparece no
// nó `fonte lake` — é por ele que se liga a linha da lista ao grafo que a consome.
export function metaFonte(f: FonteLake): string {
  const partes = [f.dataset, rotuloClasse(f.classe), `recarga ${rotuloCadencia(f.cadencia)}`];
  // Watermark só entra quando existe: "watermark —" em 54 linhas é ruído, e em fonte nunca
  // executada seria pior que ruído (sugere que há uma marca onde não há).
  if (f.watermark) partes.push(`ingerido até ${f.watermark}`);
  return partes.join(" · ");
}

// Desfecho da última execução da fonte, no MESMO formato da listagem de pipelines: momento em
// America/Sao_Paulo com fuso explícito + pill de status.
//
// Três estados, não dois:
//  - executou e tem carimbo → momento + pill;
//  - tem status mas o carimbo não é legível → diz isso, não inventa data (o `diaSP` devolve null em
//    timestamp ilegível, e `rotuloDia(null!)` derrubava a linha inteira em Agendamentos);
//  - nunca executou → "nunca executado", que é estado LEGÍTIMO de uma fonte recém-cadastrada, e
//    fica SEM pill: pintar um pill cinza ali faria parecer um desfecho que não houve.
export function ultimaExecucaoFonte(f: FonteLake): {
  texto: string;
  status?: string;
  scls?: string;
} {
  const dia = momentoLegivel(f.ultima_execucao_em) ? diaSP(f.ultima_execucao_em) : null;
  const pill = f.ultimo_status ? pillExecucao(f.ultimo_status) : null;
  if (!f.ultima_execucao_em && !f.ultimo_status) return { texto: "nunca executado" };
  const texto = dia
    ? `última execução ${rotuloDia(dia)} ${fmtHora(f.ultima_execucao_em)} (GMT-3)`
    : "última execução sem horário registrado";
  return { texto, status: pill?.label, scls: pill?.cls };
}

// O carimbo é DATA de verdade? Não dá para confiar no `null` de `diaSP` para isso: `diaSP` só
// devolve `null` quando a formatação LANÇA, e `new Date("vazio").toLocaleDateString(...)` não lança
// — devolve a string "Invalid Date". Sem esta checagem a célula saía como
// "última execução undefined/undefined/Invalid Date Invalid Date (GMT-3)" (medido pelo teste deste
// módulo), que é pior do que dizer que não há horário: parece dado.
function momentoLegivel(iso: string | null | undefined): boolean {
  if (!iso) return false;
  return Number.isFinite(new Date(iso).getTime());
}

// Ordem da lista: fonte com FALHA na última execução primeiro, depois o resto na ordem do servidor
// (que já vem por nome). São 54 linhas — a que precisa de atenção não pode depender de rolar até
// achar a pill vermelha. Empate mantém a ordem de origem (`sort` estável no V8/Node ≥ 11).
export function ordenarFontes(fontes: FonteLake[]): FonteLake[] {
  return [...fontes].sort(
    (a, b) => Number(b.ultimo_status === "falha") - Number(a.ultimo_status === "falha"),
  );
}

// Quantas fontes falharam na última execução — é o número que justifica a faixa de aviso no topo do
// bloco. Zero não vira faixa (aviso permanente é aviso que ninguém lê).
export function contarFalhas(fontes: FonteLake[]): number {
  return fontes.filter((f) => f.ultimo_status === "falha").length;
}

// ── Quarentena da fonte ───────────────────────────────────────────────────────
//
// A DLQ da ingestão existia desde a 010 e não tinha leitor: `grep quarentena web/src` devolvia
// zero. Ela aparece AQUI, na linha da fonte que já está na tela, e não numa superfície própria —
// pelo mesmo motivo que justifica o bloco inteiro (ver o cabeçalho de `FontesIngestao.tsx`): a
// fonte não tem grafo, versão nem editor, e uma tela só para a quarentena obrigaria a pessoa a
// descobrir que ela existe antes de olhar.
//
// Os tipos moram aqui, não em `api/client.ts`: o cliente não conhece estas duas rotas ainda (ver
// as pendências do track). São o contrato de `GET /ingestao/quarentena/resumo` e de
// `GET /ingestao/quarentena`, com tudo além do essencial opcional — a tela não pode quebrar se um
// campo do resumo mudar de nome no servidor.

/** Uma linha de `GET /ingestao/quarentena/resumo` — a quarentena agregada por fonte. */
export interface ResumoQuarentena {
  fonte_id: string;
  fonte_nome?: string;
  dataset?: string;
  ativo?: boolean;
  total: number;
  ultimos_7d?: number;
  ultimos_30d?: number;
  primeiro_em?: string | null;
  ultimo_em?: string | null;
  codigos?: { codigo: string; contagem: number }[];
  // O campo que MUDA A DECISÃO: fatias que caíram na quarentena e nunca pousaram no bronze. Com
  // lote bom no bronze, a quarentena é ruído já resolvido; sem ele, é dado que não existe.
  fatias_sem_lote_bom?: number;
}

/** Uma linha crua de `GET /ingestao/quarentena?fonte_id=` — o drill-down por fonte. */
export interface ItemQuarentena {
  id: string;
  fonte_id: string;
  fonte_nome?: string;
  execucao_id?: string | null;
  chave_lote?: string | null;
  etapa?: string | null;
  codigo?: string | null;
  mensagem?: string | null;
  // Onde o payload CRU ficou guardado (gs://… ou caminho do lake local). A tela mostra a URI e
  // para aí: servir o byte de terceiro pela API abriria um caminho de egresso novo.
  uri?: string | null;
  criado_em?: string | null;
}

/** Resumo por `fonte_id`, para a linha da grade achar o seu em O(1) (são 54 linhas × N resumos). */
export function indexarQuarentena(resumos: ResumoQuarentena[]): Record<string, ResumoQuarentena> {
  const por: Record<string, ResumoQuarentena> = {};
  for (const r of resumos) if (r?.fonte_id) por[r.fonte_id] = r;
  return por;
}

function buracos(r: ResumoQuarentena): number {
  const n = r.fatias_sem_lote_bom;
  return typeof n === "number" && n > 0 ? n : 0;
}

/**
 * O selo de quarentena da linha — ou `null`, e aí a coluna nem existe.
 *
 * Três decisões, cada uma custou uma discussão:
 *
 *  1. **Sem quarentena, sem coluna.** Devolver "—" para 49 das 54 fontes só ocuparia largura
 *     (mesma regra da coluna de camada na listagem de pipelines). `GridLista` monta as colunas
 *     pela união dos rótulos, então `null` aqui já apaga a coluna sozinho quando ninguém tem.
 *  2. **Vermelho SÓ quando `fatias_sem_lote_bom > 0`.** Quarentena cuja fatia depois pousou no
 *     bronze é ruído já resolvido — pintar de vermelho o que já foi corrigido treina a pessoa a
 *     ignorar a cor, e aí o vermelho que importa também é ignorado. É a mesma disciplina do
 *     "404 = não publicado" da regra da 010: aviso que grita sempre é aviso que ninguém lê.
 *  3. **Nunca a classe `ok`.** Verde significa "terminou bem" em todo o produto. Quarentena não é
 *     desfecho bom nem quando está resolvida — é neutra (`off`), no máximo.
 */
export function pillQuarentena(
  r: ResumoQuarentena | null | undefined,
): { texto: string; cls: string; titulo: string } | null {
  if (!r || !(r.total > 0)) return null;
  const sem = buracos(r);
  return {
    texto: sem > 0 ? `quarentena ${num(r.total)} · ${num(sem)} sem bronze` : `quarentena ${num(r.total)}`,
    cls: sem > 0 ? "falha" : "off",
    titulo: tituloQuarentena(r),
  };
}

/**
 * A frase longa do `title`: o que o selo não cabe dizer — desde quando, com que frequência
 * recente, com que código e, sobretudo, o que aquilo significa para o dado.
 */
export function tituloQuarentena(r: ResumoQuarentena): string {
  const sem = buracos(r);
  const partes: string[] = [
    `${num(r.total)} ${r.total === 1 ? "linha" : "linhas"} na quarentena`,
  ];
  if (typeof r.ultimos_7d === "number") partes.push(`${num(r.ultimos_7d)} nos últimos 7 dias`);
  const primeiro = diaLegivel(r.primeiro_em);
  const ultimo = diaLegivel(r.ultimo_em);
  if (primeiro) partes.push(`desde ${rotuloDia(primeiro)}`);
  if (ultimo) partes.push(`última ${rotuloDia(ultimo)}`);
  const cods = (r.codigos ?? []).map((c) => `${c.codigo} (${num(c.contagem)})`).join(", ");
  if (cods) partes.push(cods);
  // A frase que separa ruído de buraco. Sem ela o número sozinho não decide nada: 200 linhas
  // quarentenadas cujas fatias todas pousaram no bronze não custam uma investigação.
  partes.push(
    sem > 0
      ? `${num(sem)} ${sem === 1 ? "fatia nunca pousou" : "fatias nunca pousaram"} no bronze — esse dado não existe em lugar nenhum`
      : "todas as fatias acabaram pousando no bronze — quarentena já resolvida",
  );
  return partes.join(" · ");
}

// O carimbo é data de verdade? Mesma armadilha de `momentoLegivel` acima: `diaSP` só devolve
// `null` quando a formatação LANÇA, e `new Date("vazio")` não lança — devolve "Invalid Date", que
// `rotuloDia` imprimiria como "Invalid Date/Invalid Date/Invalid Date".
function diaLegivel(iso: string | null | undefined): string | null {
  if (!momentoLegivel(iso)) return null;
  return diaSP(iso);
}

/** Quantas fontes têm fatia sem lote bom no bronze — o número que justifica a faixa de aviso. */
export function contarFontesComBuraco(resumos: ResumoQuarentena[]): number {
  return resumos.filter((r) => buracos(r) > 0).length;
}

/** Quantas FATIAS, somando todas as fontes, caíram na quarentena e nunca pousaram no bronze. */
export function contarFatiasSemBronze(resumos: ResumoQuarentena[]): number {
  return resumos.reduce((t, r) => t + buracos(r), 0);
}

// Sub-linha de uma linha crua da quarentena: etapa e código são o que se conserta; o momento diz
// se é de hoje ou de 2024. A MENSAGEM fica na coluna própria porque é longa e a URI, na dela.
export function metaQuarentena(it: ItemQuarentena): string {
  const partes = [it.chave_lote || "fatia sem chave", it.etapa || "etapa não registrada"];
  if (it.codigo) partes.push(it.codigo);
  return partes.join(" · ");
}

// ── Abas por CLASSE de acesso (tela própria "Fontes de Ingestão") ────────────
//
// Mesmo arranjo de `abasDeTipo`/`daAba` em `tipos-conexao.ts` — reusa `ABA_TODAS`/`abaValida` de
// lá (a normalização de aba desconhecida não muda por entidade). Ordem fixa segue a mesma do
// contrato declarativo; classe
// nova do backend entra no fim, ordenada, em vez de sumir da lista de abas.
const ORDEM_CLASSE = ["http_file", "rest_api", "odata", "portal_download"];

export function abasDeClasse(
  fontes: { classe?: string | null }[],
): { chave: string; rotulo: string; contagem: number }[] {
  const contagem = new Map<string, number>();
  for (const f of fontes) {
    const c = (f.classe ?? "").trim();
    contagem.set(c, (contagem.get(c) ?? 0) + 1);
  }
  const conhecidas = ORDEM_CLASSE.filter((c) => contagem.has(c));
  const desconhecidas = [...contagem.keys()].filter((c) => !ORDEM_CLASSE.includes(c)).sort();
  return [
    { chave: ABA_TODAS, rotulo: "Todas", contagem: fontes.length },
    ...[...conhecidas, ...desconhecidas].map((c) => ({
      chave: c,
      rotulo: rotuloClasse(c),
      contagem: contagem.get(c) ?? 0,
    })),
  ];
}

// Mesmo predicado que alimenta a contagem acima — duas contas separadas divergem no dia em que uma
// mudar (mesma disciplina de `daAba`).
export function daAbaClasse(
  f: { classe?: string | null },
  aba: string,
  abasValidas: string[],
): boolean {
  if (!aba || !abasValidas.includes(aba)) return true;
  return (f.classe ?? "").trim() === aba;
}

// ── Formulário de contrato ↔ JSON (`FormaContratoFonte.tsx`) ──────────────────
//
// Mesmo par `formDeConfig`/`montarConfig` de `Conexoes.tsx` (`NovaConexao`): um `FormContrato`
// achatado com os campos de TODAS as classes, convertido nos dois sentidos. `classe` decide quais
// campos de `acesso` a UI mostra; os demais ficam no estado mas viram `undefined` no JSON de saída
// (formulário sujo trocando de classe não vaza campo da classe anterior no contrato salvo).
//
// Textos multivalor (`schema_esperado`, `grao.chaves`, mapas como `variaveis`/`params`) entram como
// texto simples — uma linha por item, ou JSON cru para mapa — em vez de um repetidor de campos: o
// contrato já é JSON por natureza, e um textarea que se valida via `POST /fontes/validar`
// antes de salvar é menos código do que uma UI de linhas dinâmicas para cada mapa possível.
export interface FormContrato {
  classe: string;
  dataset: string;
  descricao: string;
  cadencia: string;
  // janela
  janelaTipo: string; // diaria | mensal | anual | periodo
  janelaInicio: string;
  passoMaxDias: string; // só periodo
  atrasoDias: string;
  alinharMes: boolean; // alinhar_a: "mes", só periodo
  fatiaCompetencia: boolean;
  // acesso — comuns a http_file/rest_api/odata
  urlPadrao: string;
  variaveis: string; // JSON textarea
  condicional: boolean; // http_file
  params: string; // JSON textarea (rest_api/odata)
  paramInicio: string; // rest_api
  paramFim: string; // rest_api
  formatoData: string; // rest_api
  paramsJanela: string; // JSON textarea, odata
  top: string; // odata
  maxPaginas: string; // odata
  paginar: boolean; // odata, default true
  campoItens: string; // odata
  paginaIndice: string; // portal_download
  padraoLink: string; // portal_download
  maxArquivos: string; // portal_download
  // formato
  formatoTipo: string; // csv | json | largura_fixa | parquet
  encoding: string;
  delimitador: string;
  compressao: string; // "" | zip | gzip
  arquivoNoZip: string;
  camposLarguraFixa: string; // JSON textarea (array de {nome,inicio,tamanho,tipo?})
  pularLinhas: string;
  linhaCabecalho: string;
  jsonPath: string;
  prefixoLinha: string;
  // schema_esperado — uma coluna por linha
  schemaEsperado: string;
  // grao
  graoEstado: string; // declarado | desconhecido | sem_chave
  graoChaves: string; // uma coluna por linha
  graoMotivo: string;
  graoDesempateColunas: string; // "coluna:asc" ou "coluna:desc" por linha
  graoDesempateMotivo: string;
  // limites
  timeoutSegundos: string;
  concorrencia: string;
  tentativas: string;
  // destino
  destinoConexaoId: string; // "" = backend local (default)
}

export const CLASSES_FONTE: { valor: string; rotulo: string; meta: string }[] = [
  { valor: "http_file", rotulo: "Arquivo publicado", meta: "URL por convenção (CVM, B3)" },
  { valor: "rest_api", rotulo: "API REST", meta: "JSON paginado por janela" },
  { valor: "odata", rotulo: "OData", meta: "$top/$skip (BCB Olinda)" },
  { valor: "portal_download", rotulo: "Download de portal", meta: "link descoberto por regex" },
];

export function estadoInicialContrato(classe: string = "http_file"): FormContrato {
  return {
    classe,
    dataset: "",
    descricao: "",
    cadencia: "manual",
    janelaTipo: "mensal",
    janelaInicio: "",
    passoMaxDias: "",
    atrasoDias: "",
    alinharMes: false,
    fatiaCompetencia: false,
    urlPadrao: "",
    variaveis: "",
    condicional: true,
    params: "",
    paramInicio: "",
    paramFim: "",
    formatoData: "",
    paramsJanela: "",
    top: "",
    maxPaginas: "",
    paginar: true,
    campoItens: "",
    paginaIndice: "",
    padraoLink: "",
    maxArquivos: "",
    formatoTipo: "csv",
    encoding: "",
    delimitador: "",
    compressao: "",
    arquivoNoZip: "",
    camposLarguraFixa: "",
    pularLinhas: "",
    linhaCabecalho: "",
    jsonPath: "",
    prefixoLinha: "",
    schemaEsperado: "",
    graoEstado: "desconhecido",
    graoChaves: "",
    graoMotivo: "",
    graoDesempateColunas: "",
    graoDesempateMotivo: "",
    timeoutSegundos: "",
    concorrencia: "",
    tentativas: "",
    destinoConexaoId: "",
  };
}

function linhasDe(texto: string): string[] {
  return texto
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);
}

function jsonDe(texto: string): Record<string, unknown> | undefined {
  const t = texto.trim();
  if (!t) return undefined;
  try {
    const v = JSON.parse(t);
    return v && typeof v === "object" && !Array.isArray(v) ? v : undefined;
  } catch {
    return undefined;
  }
}

function textoDeJson(v: unknown): string {
  if (!v || typeof v !== "object" || Array.isArray(v) || !Object.keys(v).length) return "";
  return JSON.stringify(v, null, 2);
}

function numOuUndef(s: string): number | undefined {
  const t = s.trim();
  if (!t) return undefined;
  const n = Number(t);
  return Number.isFinite(n) ? n : undefined;
}

/** `FormContrato` → JSON do contrato. Omite tudo vazio — campo em branco não vira `""`/`0` no
 * contrato salvo, que é o que faria o backend achar que "" é um valor de verdade. */
export function contratoDeForm(f: FormContrato): Record<string, unknown> {
  const janela: Record<string, unknown> = { tipo: f.janelaTipo };
  if (f.janelaInicio.trim()) janela.inicio = f.janelaInicio.trim();
  if (f.janelaTipo === "periodo") {
    const passo = numOuUndef(f.passoMaxDias);
    if (passo !== undefined) janela.passo_max_dias = passo;
    if (f.alinharMes) janela.alinhar_a = "mes";
  }
  const atraso = numOuUndef(f.atrasoDias);
  if (atraso !== undefined) janela.atraso_dias = atraso;
  if (f.fatiaCompetencia) janela.fatia_e_competencia = true;

  const acesso: Record<string, unknown> = {};
  if (f.classe !== "portal_download" && f.urlPadrao.trim()) acesso.url_padrao = f.urlPadrao.trim();
  const variaveis = jsonDe(f.variaveis);
  if (variaveis) acesso.variaveis = variaveis;
  if (f.classe === "http_file") acesso.condicional = f.condicional;
  if (f.classe === "rest_api" || f.classe === "odata") {
    const params = jsonDe(f.params);
    if (params) acesso.params = params;
  }
  if (f.classe === "rest_api") {
    if (f.paramInicio.trim()) acesso.param_inicio = f.paramInicio.trim();
    if (f.paramFim.trim()) acesso.param_fim = f.paramFim.trim();
    if (f.formatoData.trim()) acesso.formato_data = f.formatoData.trim();
  }
  if (f.classe === "odata") {
    const paramsJanela = jsonDe(f.paramsJanela);
    if (paramsJanela) acesso.params_janela = paramsJanela;
    const top = numOuUndef(f.top);
    if (top !== undefined) acesso.top = top;
    const maxPaginas = numOuUndef(f.maxPaginas);
    if (maxPaginas !== undefined) acesso.max_paginas = maxPaginas;
    if (!f.paginar) acesso.paginar = false;
    if (f.campoItens.trim()) acesso.campo_itens = f.campoItens.trim();
  }
  if (f.classe === "portal_download") {
    if (f.paginaIndice.trim()) acesso.pagina_indice = f.paginaIndice.trim();
    if (f.padraoLink.trim()) acesso.padrao_link = f.padraoLink.trim();
    const maxArquivos = numOuUndef(f.maxArquivos);
    if (maxArquivos !== undefined) acesso.max_arquivos = maxArquivos;
  }

  const formato: Record<string, unknown> = { tipo: f.formatoTipo };
  if (f.encoding.trim()) formato.encoding = f.encoding.trim();
  if (f.delimitador.trim()) formato.delimitador = f.delimitador;
  if (f.compressao) formato.compressao = f.compressao;
  if (f.arquivoNoZip.trim()) formato.arquivo_no_zip = f.arquivoNoZip.trim();
  if (f.formatoTipo === "largura_fixa") {
    try {
      const campos = f.camposLarguraFixa.trim() ? JSON.parse(f.camposLarguraFixa) : undefined;
      if (Array.isArray(campos) && campos.length) formato.campos = campos;
    } catch {
      /* inválido — `validar()` no backend recusa e a UI mostra a mensagem */
    }
    const pular = numOuUndef(f.pularLinhas);
    if (pular !== undefined) formato.pular_linhas = pular;
    const cab = numOuUndef(f.linhaCabecalho);
    if (cab !== undefined) formato.linha_cabecalho = cab;
  }
  if (f.formatoTipo === "json" && f.jsonPath.trim()) formato.json_path = f.jsonPath.trim();
  if (f.formatoTipo === "csv" && f.prefixoLinha.trim()) formato.prefixo_linha = f.prefixoLinha.trim();

  const contrato: Record<string, unknown> = {
    classe: f.classe,
    dataset: f.dataset.trim(),
    cadencia: f.cadencia,
    janela,
    acesso,
    formato,
  };
  if (f.descricao.trim()) contrato.descricao = f.descricao.trim();
  const schema = linhasDe(f.schemaEsperado);
  if (schema.length) contrato.schema_esperado = schema;

  const grao: Record<string, unknown> = { estado: f.graoEstado };
  if (f.graoEstado === "declarado") {
    grao.chaves = linhasDe(f.graoChaves);
    const desempateColunas = linhasDe(f.graoDesempateColunas)
      .map((l) => {
        const [coluna, ordem] = l.split(":").map((x) => x.trim());
        return coluna ? { coluna, ordem: ordem === "desc" ? "desc" : "asc" } : null;
      })
      .filter((x): x is { coluna: string; ordem: string } => x !== null);
    if (desempateColunas.length && f.graoDesempateMotivo.trim()) {
      grao.desempate = { colunas: desempateColunas, motivo: f.graoDesempateMotivo.trim() };
    }
  } else if (f.graoEstado === "sem_chave") {
    grao.chaves = [];
    if (f.graoMotivo.trim()) grao.motivo = f.graoMotivo.trim();
  } else {
    grao.chaves = [];
  }
  if (f.graoEstado !== "desconhecido") contrato.grao = grao;

  const limites: Record<string, unknown> = {};
  const timeout = numOuUndef(f.timeoutSegundos);
  if (timeout !== undefined) limites.timeout_segundos = timeout;
  const concorrencia = numOuUndef(f.concorrencia);
  if (concorrencia !== undefined) limites.concorrencia = concorrencia;
  const tentativas = numOuUndef(f.tentativas);
  if (tentativas !== undefined) limites.tentativas = tentativas;
  if (Object.keys(limites).length) contrato.limites = limites;

  return contrato;
}

/** JSON do contrato → `FormContrato`. Inverso de `contratoDeForm`; campo ausente no JSON volta ao
 * default de `estadoInicialContrato`, nunca `undefined` solto num controlled input do React. */
export function formDeContrato(c: Record<string, unknown>): FormContrato {
  const base = estadoInicialContrato(String(c.classe ?? "http_file"));
  const janela = (c.janela ?? {}) as Record<string, unknown>;
  const acesso = (c.acesso ?? {}) as Record<string, unknown>;
  const formato = (c.formato ?? {}) as Record<string, unknown>;
  const grao = (c.grao ?? {}) as Record<string, unknown>;
  const desempate = (grao.desempate ?? {}) as Record<string, unknown>;
  const limites = (c.limites ?? {}) as Record<string, unknown>;

  return {
    ...base,
    classe: String(c.classe ?? base.classe),
    dataset: String(c.dataset ?? ""),
    descricao: String(c.descricao ?? ""),
    cadencia: String(c.cadencia ?? base.cadencia),
    janelaTipo: String(janela.tipo ?? base.janelaTipo),
    janelaInicio: String(janela.inicio ?? ""),
    passoMaxDias: janela.passo_max_dias != null ? String(janela.passo_max_dias) : "",
    atrasoDias: janela.atraso_dias != null ? String(janela.atraso_dias) : "",
    alinharMes: janela.alinhar_a === "mes",
    fatiaCompetencia: janela.fatia_e_competencia === true,
    urlPadrao: String(acesso.url_padrao ?? ""),
    variaveis: textoDeJson(acesso.variaveis),
    condicional: acesso.condicional !== false,
    params: textoDeJson(acesso.params),
    paramInicio: String(acesso.param_inicio ?? ""),
    paramFim: String(acesso.param_fim ?? ""),
    formatoData: String(acesso.formato_data ?? ""),
    paramsJanela: textoDeJson(acesso.params_janela),
    top: acesso.top != null ? String(acesso.top) : "",
    maxPaginas: acesso.max_paginas != null ? String(acesso.max_paginas) : "",
    paginar: acesso.paginar !== false,
    campoItens: String(acesso.campo_itens ?? ""),
    paginaIndice: String(acesso.pagina_indice ?? ""),
    padraoLink: String(acesso.padrao_link ?? ""),
    maxArquivos: acesso.max_arquivos != null ? String(acesso.max_arquivos) : "",
    formatoTipo: String(formato.tipo ?? base.formatoTipo),
    encoding: String(formato.encoding ?? ""),
    delimitador: String(formato.delimitador ?? ""),
    compressao: String(formato.compressao ?? ""),
    arquivoNoZip: String(formato.arquivo_no_zip ?? ""),
    camposLarguraFixa: Array.isArray(formato.campos) ? JSON.stringify(formato.campos, null, 2) : "",
    pularLinhas: formato.pular_linhas != null ? String(formato.pular_linhas) : "",
    linhaCabecalho: formato.linha_cabecalho != null ? String(formato.linha_cabecalho) : "",
    jsonPath: String(formato.json_path ?? ""),
    prefixoLinha: String(formato.prefixo_linha ?? ""),
    schemaEsperado: Array.isArray(c.schema_esperado) ? (c.schema_esperado as string[]).join("\n") : "",
    graoEstado: String(grao.estado ?? "desconhecido"),
    graoChaves: Array.isArray(grao.chaves) ? (grao.chaves as string[]).join("\n") : "",
    graoMotivo: String(grao.motivo ?? ""),
    graoDesempateColunas: Array.isArray(desempate.colunas)
      ? (desempate.colunas as { coluna: string; ordem: string }[])
          .map((d) => `${d.coluna}:${d.ordem}`)
          .join("\n")
      : "",
    graoDesempateMotivo: String(desempate.motivo ?? ""),
    timeoutSegundos: limites.timeout_segundos != null ? String(limites.timeout_segundos) : "",
    concorrencia: limites.concorrencia != null ? String(limites.concorrencia) : "",
    tentativas: limites.tentativas != null ? String(limites.tentativas) : "",
    destinoConexaoId: "",
  };
}
