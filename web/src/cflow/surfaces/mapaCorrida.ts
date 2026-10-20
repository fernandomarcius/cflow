// Mapa da CORRIDA — lógica PURA, sem React.
//
// A lista de execuções mostra a corrida como uma linha com acordeão: diz QUEM rodou, não quem
// esperou quem. O mapa desenha cada pipeline da corrida como um nó e as dependências como arestas
// (`GET /corridas/{cadeia_id}`), no mesmo grafo read-only do detalhe de execução.
//
// Aqui fica o que a tela decide a partir da resposta, testado sem navegador e sem banco
// (`scripts/teste-mapa-corrida.mjs`, mesmo arranjo de `cadeias.ts`).
import type {
  CorridaMapa,
  ExecucaoItem,
  GrupoExecucoes,
  TipoArestaCorrida,
} from "../../api/client";
import type { NoDoGrafo } from "../execucao/ExecGrafo";
import type { EstadoNo } from "../execucao/formato";
import { autoLayout, passoVertical } from "../layout";
import { tempoNaCorrida } from "./cadeias";

const ms = (iso: string) => new Date(iso).getTime();

// Estado do NÓ-pipeline. Três casos pedem cuidado, todos pela mesma razão: a corrida PRÉ-CRIA as
// execuções no disparo , então o status sozinho mente —
//  - `executando` sem nó começado é FILA (esperando as dependências), não trabalho;
//  - `falha` sem nó começado é "não rodou": a corrida parou antes de chegar nela, e pintá-la de
//    vermelho faria o mapa mostrar várias falhas onde houve uma.
export function estadoNaCorrida(e: ExecucaoItem): EstadoNo {
  const t = tempoNaCorrida(e);
  if (e.status === "executando") return t.tipo === "aguardando" ? "pendente" : "executando";
  if (e.status === "concluida") return "ok";
  if (e.status === "falha") return t.tipo === "sem_nos" ? "skip" : "falha";
  return "indefinido";
}

// Pill e rótulo do estado na corrida. Não reusa `pillNo`: lá `skip` é "não executado" (nó que o
// pipeline pulou) e aqui é o pipeline que a corrida não alcançou — palavras diferentes de propósito.
export function rotuloEstadoNaCorrida(estado: EstadoNo): { cls: string; label: string } {
  switch (estado) {
    case "ok":
      return { cls: "ok", label: "concluída" };
    case "falha":
      return { cls: "falha", label: "falha" };
    case "executando":
      return { cls: "exec", label: "em execução" };
    case "pendente":
      return { cls: "off", label: "aguardando" };
    case "skip":
      return { cls: "skip", label: "não rodou" };
    default:
      return { cls: "skip", label: "estado desconhecido" };
  }
}

export function raizDaCorrida(c: CorridaMapa): ExecucaoItem | null {
  return c.execucoes.find((e) => e.id === c.raiz_id) ?? null;
}

// A corrida no formato de GRUPO da lista — é o que deixa a tela reusar `janelaCorrida` sem uma
// segunda conta de "quando a corrida começou e terminou". Sem raiz visível, a mais recente faz o
// papel (a mesma regra da lista para cadeia sem a marca).
export function grupoDaCorrida(c: CorridaMapa): GrupoExecucoes | null {
  if (c.execucoes.length === 0) return null;
  const raiz =
    raizDaCorrida(c) ??
    [...c.execucoes].sort((a, b) => ms(b.iniciada_em ?? "0") - ms(a.iniciada_em ?? "0"))[0];
  const filhas = c.execucoes.filter((e) => e.id !== raiz.id);
  return {
    chave: c.cadeia_id,
    raiz,
    filhas,
    total_no_grupo: c.execucoes.length,
    casam_filtro: c.execucoes.length,
  };
}

// Os nós do grafo. Ordenados por NOME, e não pela ordem em que rodaram: durante o acompanhamento
// o `comecou_em` vai sendo preenchido, e ordenar por ele fazia os nós trocarem de linha a cada
// atualização — o mapa não pode se rearranjar debaixo de quem está olhando.
export function nosDaCorrida(c: CorridaMapa): NoDoGrafo[] {
  const ids = new Set(c.execucoes.map((e) => e.id));
  const de = new Map<string, string[]>();
  const tipos = new Map<string, Record<string, TipoArestaCorrida>>();
  for (const a of c.arestas) {
    if (!ids.has(a.de) || !ids.has(a.para)) continue;
    de.set(a.para, [...(de.get(a.para) ?? []), a.de]);
    tipos.set(a.para, { ...(tipos.get(a.para) ?? {}), [a.de]: a.tipo });
  }
  return [...c.execucoes]
    .sort((a, b) => a.pipeline_nome.localeCompare(b.pipeline_nome, "pt-BR") || a.id.localeCompare(b.id))
    .map((e) => {
      const estado = estadoNaCorrida(e);
      const t = tempoNaCorrida(e);
      return {
        id: e.id,
        // A duração é a do trabalho, do primeiro nó ao fim — nunca desde o disparo, que soma a fila
        // das dependências (ver `tempoNaCorrida`).
        tipo: e.id === c.raiz_id ? `raiz · v${e.versao}` : `v${e.versao}`,
        nome: e.pipeline_nome,
        estado,
        duracao_ms: t.tipo === "rodou" && t.fim ? Math.max(0, ms(t.fim) - ms(t.inicio)) : null,
        erro: e.erro,
        // No estado `executando` é o que dá o "há quanto tempo" do card.
        iniciado_em: t.tipo === "rodou" ? t.inicio : null,
        executou: true,
        orfao: false,
        de: de.get(e.id) ?? [],
        deTipo: tipos.get(e.id),
        metrica: rotuloEstadoNaCorrida(estado).label,
      };
    });
}

// Quem o mapa seleciona ao abrir, sem clique: o pipeline que FALHOU (é o que se vem ver), senão o
// que está rodando agora, senão a raiz.
export function alvoInicial(c: CorridaMapa): string | null {
  const nos = nosDaCorrida(c);
  return (
    nos.find((n) => n.estado === "falha")?.id ??
    nos.find((n) => n.estado === "executando")?.id ??
    c.raiz_id ??
    nos[0]?.id ??
    null
  );
}

// "4 concluídas · 1 com falha · 1 não rodou" — falha primeiro, como `rotuloComposicao`, e parte
// zerada não é anunciada. Conta a corrida INTEIRA (raiz inclusive): aqui não há pill de linha-mãe
// separada que a conte.
export function resumoEstados(c: CorridaMapa): string {
  const conta = (estado: EstadoNo) =>
    c.execucoes.filter((e) => estadoNaCorrida(e) === estado).length;
  const partes: string[] = [];
  const falha = conta("falha");
  const exec = conta("executando");
  const fila = conta("pendente");
  const ok = conta("ok");
  const naoRodou = conta("skip");
  if (falha) partes.push(`${falha} com falha`);
  if (exec) partes.push(`${exec} em execução`);
  if (fila) partes.push(`${fila} aguardando`);
  if (ok) partes.push(`${ok} concluída${ok === 1 ? "" : "s"}`);
  if (naoRodou) partes.push(`${naoRodou} não rodou`);
  return partes.join(" · ");
}

export function temArestaPublicado(c: CorridaMapa): boolean {
  return c.arestas.some((a) => a.tipo === "publicado");
}

export function corridaEmAndamento(c: CorridaMapa): boolean {
  return c.execucoes.some((e) => e.status === "executando");
}

// ─── Legibilidade ─────────────────────────────────────────────────────────────────────────────
// Numa corrida grande o mapa virava novelo. Medido numa corrida real: 38
// pipelines e 116 ligações, 61 delas saindo de quatro pipelines `LOAD_*`, e 15 nós empilhados numa coluna
// só. Três recursos atacam isso, cada um por um lado: ocultar a ligação REDUNDANTE (34 das 116),
// agrupar as colunas por FAMÍLIA de pipeline e DESTACAR o caminho do pipeline selecionado.

type ArestaMin = { de: string; para: string };

// Redução transitiva: some a seta A→C quando A→B→C já está desenhado. Não esconde dependência — o
// caminho continua visível, e a ordem em que as coisas precisam rodar é a mesma. A lista completa
// de quem cada pipeline lê fica no painel (`vizinhosNaCorrida`), e a tela deixa voltar ao completo.
export function reduzirArestas<A extends ArestaMin>(arestas: A[]): A[] {
  const seguintes = new Map<string, string[]>();
  for (const a of arestas) seguintes.set(a.de, [...(seguintes.get(a.de) ?? []), a.para]);
  const alcancaPorOutroCaminho = (de: string, para: string) => {
    const pilha = (seguintes.get(de) ?? []).filter((x) => x !== para);
    const vistos = new Set(pilha);
    while (pilha.length) {
      const x = pilha.pop()!;
      if (x === para) return true;
      for (const y of seguintes.get(x) ?? []) {
        if (!vistos.has(y)) {
          vistos.add(y);
          pilha.push(y);
        }
      }
    }
    return false;
  };
  return arestas.filter((a) => !alcancaPorOutroCaminho(a.de, a.para));
}

// O que acende quando um pipeline está selecionado: tudo de que ele depende (para trás) e tudo que
// depende dele (para frente). A aresta acende se estiver num desses caminhos — a que liga um
// ancestral a um descendente PULANDO o foco não passa por ele, e fica apagada.
export function caminhoDoFoco(
  nos: { id: string; de: string[] }[],
  foco: string,
): { nos: Set<string>; arestas: Set<string> } {
  const porId = new Map(nos.map((n) => [n.id, n]));
  const seguintes = new Map<string, string[]>();
  for (const n of nos) for (const p of n.de) seguintes.set(p, [...(seguintes.get(p) ?? []), n.id]);
  const percorrer = (inicio: string, proximos: (id: string) => string[]) => {
    const vistos = new Set<string>([inicio]);
    const pilha = [inicio];
    while (pilha.length) {
      const x = pilha.pop()!;
      for (const y of proximos(x)) {
        if (!vistos.has(y)) {
          vistos.add(y);
          pilha.push(y);
        }
      }
    }
    return vistos;
  };
  const antes = percorrer(foco, (id) => porId.get(id)?.de ?? []);
  const depois = percorrer(foco, (id) => seguintes.get(id) ?? []);
  const arestas = new Set<string>();
  for (const n of nos) {
    for (const p of n.de) {
      if ((antes.has(p) && antes.has(n.id)) || (depois.has(p) && depois.has(n.id))) {
        arestas.add(`${p}->${n.id}`);
      }
    }
  }
  return { nos: new Set([...antes, ...depois]), arestas };
}

// O destaque que a TELA aplica: o caminho do foco, mas só quando ele separa alguma coisa. Focar a
// raiz (o que a abertura faz) acende tudo — todo pipeline é ancestral dela —, e aí o "destaque" vira
// todas as setas em negrito: o novelo de antes, mais escuro. Nesse caso não se destaca nada.
export function realceDoFoco(
  nos: { id: string; de: string[] }[],
  foco: string | null,
): { nos: Set<string>; arestas: Set<string> } | null {
  if (!foco || !nos.some((n) => n.id === foco)) return null;
  const caminho = caminhoDoFoco(nos, foco);
  return caminho.nos.size >= nos.length ? null : caminho;
}

// Família = o primeiro pedaço do nome: `LOAD SALES` → load, `fato_a` → fato, `ref_04_…` →
// ref, `etl — recálculo…` → etl. É convenção de nome, não metadado — por isso a tela deixa trocar
// para as colunas por dependência, e sem família com dois ou mais membros o agrupamento não se aplica.
export function familiaDe(nome: string): string {
  const t = nome.trim();
  return (t.split(/[\s_—–-]+/)[0] || t).toLowerCase();
}

export interface FaixaMapa {
  id: string;
  rotulo: string;
  coluna: number; // primeira coluna da faixa
  largura: number; // quantas colunas ela ocupa
  membros: string[];
}

const RAIZ = " raiz";

function profundidades(nos: { id: string; de: string[] }[], dentro?: Set<string>): Record<string, number> {
  const porId = new Map(nos.map((n) => [n.id, n]));
  const prof: Record<string, number> = {};
  const calc = (id: string, vendo: Set<string>): number => {
    if (prof[id] != null) return prof[id];
    if (vendo.has(id)) return 0;
    vendo.add(id);
    const preds = (porId.get(id)?.de ?? []).filter((p) => porId.has(p) && (!dentro || dentro.has(p)));
    prof[id] = preds.length ? 1 + Math.max(...preds.map((p) => calc(p, vendo))) : 0;
    return prof[id];
  };
  nos.forEach((n) => calc(n.id, new Set()));
  return prof;
}

// Componentes fortemente conexos do grafo de FAMÍLIAS (Tarjan). Duas famílias que se leem uma à
// outra (A lê B e B lê A, por pipelines diferentes) não cabem em faixas ordenadas — uma seta
// voltaria para trás —, então viram UMA faixa. Nos pipelines o grafo é acíclico; nas famílias não
// precisa ser.
function componentes(familias: string[], adj: Map<string, Set<string>>): string[][] {
  const indice = new Map<string, number>();
  const baixo = new Map<string, number>();
  const pilha: string[] = [];
  const naPilha = new Set<string>();
  const saida: string[][] = [];
  let cont = 0;
  const visitar = (v: string) => {
    indice.set(v, cont);
    baixo.set(v, cont);
    cont++;
    pilha.push(v);
    naPilha.add(v);
    for (const w of adj.get(v) ?? []) {
      if (!indice.has(w)) {
        visitar(w);
        baixo.set(v, Math.min(baixo.get(v)!, baixo.get(w)!));
      } else if (naPilha.has(w)) {
        baixo.set(v, Math.min(baixo.get(v)!, indice.get(w)!));
      }
    }
    if (baixo.get(v) === indice.get(v)) {
      const grupo: string[] = [];
      let w: string;
      do {
        w = pilha.pop()!;
        naPilha.delete(w);
        grupo.push(w);
      } while (w !== v);
      saida.push(grupo.sort());
    }
  };
  familias.forEach((f) => indice.has(f) || visitar(f));
  return saida;
}

// Colunas agrupadas por família: cada faixa é um bloco de colunas contíguo, as faixas vêm em ordem
// de dependência (quem é lido antes de quem lê) e, DENTRO da faixa, a coluna é a profundidade só
// entre os membros dela. Toda seta vai da esquerda para a direita. A raiz tem faixa própria, no fim.
// `null` = não há o que agrupar (menos de duas famílias com dois ou mais pipelines).
export function colunasPorFamilia(
  nos: { id: string; nome: string; de: string[] }[],
  raizId: string | null,
): { colunas: Record<string, number>; faixas: FaixaMapa[] } | null {
  const fam: Record<string, string> = {};
  for (const n of nos) fam[n.id] = n.id === raizId ? RAIZ : familiaDe(n.nome);
  const contagem = new Map<string, number>();
  for (const f of Object.values(fam)) contagem.set(f, (contagem.get(f) ?? 0) + 1);
  if ([...contagem].filter(([f, k]) => f !== RAIZ && k >= 2).length < 2) return null;

  const familias = [...contagem.keys()].sort();
  const adj = new Map<string, Set<string>>(familias.map((f) => [f, new Set<string>()]));
  for (const n of nos) {
    for (const p of n.de) {
      if (fam[p] != null && fam[p] !== fam[n.id]) adj.get(fam[p])!.add(fam[n.id]);
    }
  }
  const comps = componentes(familias, adj);
  const compDe = new Map<string, number>();
  comps.forEach((c, i) => c.forEach((f) => compDe.set(f, i)));

  // Ordem das faixas: topológica sobre as faixas; empate pela profundidade do membro mais raso e,
  // por fim, pelo nome — a mesma corrida sai sempre com a mesma ordem.
  const prof = profundidades(nos);
  const rasa = comps.map((c) =>
    Math.min(...nos.filter((n) => c.includes(fam[n.id])).map((n) => prof[n.id] ?? 0)),
  );
  const entrada = comps.map(() => 0);
  const saidas = comps.map(() => new Set<number>());
  comps.forEach((c, i) =>
    c.forEach((f) =>
      (adj.get(f) ?? new Set()).forEach((g) => {
        const j = compDe.get(g)!;
        if (j !== i && !saidas[i].has(j)) {
          saidas[i].add(j);
          entrada[j]++;
        }
      }),
    ),
  );
  const chave = (i: number) => [comps[i].includes(RAIZ) ? 1 : 0, rasa[i], comps[i].join(",")] as const;
  const menor = (a: number, b: number) => {
    const [ra, pa, na] = chave(a);
    const [rb, pb, nb] = chave(b);
    return ra - rb || pa - pb || na.localeCompare(nb);
  };
  const prontas = comps.map((_, i) => i).filter((i) => entrada[i] === 0);
  const ordem: number[] = [];
  while (prontas.length) {
    prontas.sort(menor);
    const i = prontas.shift()!;
    ordem.push(i);
    for (const j of saidas[i]) if (--entrada[j] === 0) prontas.push(j);
  }

  const colunas: Record<string, number> = {};
  const faixas: FaixaMapa[] = [];
  let inicio = 0;
  for (const i of ordem) {
    const membros = nos.filter((n) => comps[i].includes(fam[n.id]));
    const ids = new Set(membros.map((n) => n.id));
    const dentro = profundidades(membros, ids);
    const largura = 1 + Math.max(0, ...membros.map((n) => dentro[n.id] ?? 0));
    membros.forEach((n) => (colunas[n.id] = inicio + (dentro[n.id] ?? 0)));
    // Rótulo com a grafia do próprio nome (LOAD, fato, ref…), não a chave em minúsculas.
    const nomes = comps[i].map((f) =>
      f === RAIZ
        ? "raiz"
        : (membros.find((n) => fam[n.id] === f)?.nome.trim().split(/[\s_—–-]+/)[0] ?? f),
    );
    faixas.push({
      id: comps[i].join("+"),
      rotulo: `${nomes.join(" + ")} · ${membros.length}`,
      coluna: inicio,
      largura,
      membros: membros.map((n) => n.id),
    });
    inicio += largura;
  }
  return { colunas, faixas };
}

// Geometria do mapa: a mesma do grafo de execução (nó de 180 × ~64, passo 210 × 96 — `ExecGrafo`).
const PASSO_X_MAPA = 210;
const PASSO_Y_MAPA = 96;
const ALTURA_NO = 64;
const TOPO_FAIXA = 30; // espaço do rótulo da faixa acima do primeiro nó

export interface DesenhoMapa {
  posicoes: Record<string, { x: number; y: number }>;
  faixas: { id: string; rotulo: string; x: number; y: number; largura: number; altura: number }[];
  porFamilia: boolean; // o agrupamento pedido se aplicou (senão caiu nas colunas por dependência)
}

// Posições do mapa. A ORDEM dentro de cada coluna vem do layout "Sem cruzar linhas" do Studio
// (`layout.ts`); por família, as colunas são as de `colunasPorFamilia` e o algoritmo só ordena. As
// coordenadas que ele devolve estão na escala dos cards do Studio: aqui viram coluna (posto do x) e
// altura proporcional ao passo do mapa.
export function desenhoDaCorrida(
  nos: NoDoGrafo[],
  raizId: string | null,
  modo: "familia" | "dependencia",
): DesenhoMapa {
  if (!nos.length) return { posicoes: {}, faixas: [], porFamilia: false };
  const arestas = nos.flatMap((n) => n.de.map((de) => ({ de, para: n.id })));
  const fam = modo === "familia" ? colunasPorFamilia(nos, raizId) : null;
  const pos = autoLayout(
    nos.map((n) => ({ id: n.id })),
    arestas,
    "sem-cruzamentos",
    fam?.colunas,
  );
  const xs = [...new Set(Object.values(pos).map((p) => p.x))].sort((a, b) => a - b);
  const escala = PASSO_Y_MAPA / passoVertical(nos.length);
  const minY = Math.min(...Object.values(pos).map((p) => p.y));
  const topo = fam ? TOPO_FAIXA : 0;
  const posicoes: Record<string, { x: number; y: number }> = {};
  for (const n of nos) {
    const p = pos[n.id];
    const coluna = fam ? fam.colunas[n.id] : xs.indexOf(p.x);
    posicoes[n.id] = {
      x: 24 + coluna * PASSO_X_MAPA,
      y: 24 + topo + Math.round((p.y - minY) * escala),
    };
  }
  const fundo = Math.max(...Object.values(posicoes).map((p) => p.y)) + ALTURA_NO;
  const faixas = (fam?.faixas ?? []).map((f) => ({
    id: f.id,
    rotulo: f.rotulo,
    x: 24 + f.coluna * PASSO_X_MAPA - 12,
    y: 12,
    largura: f.largura * PASSO_X_MAPA - 6,
    altura: fundo - 12 + 16,
  }));
  return { posicoes, faixas, porFamilia: !!fam };
}

export interface VizinhoCorrida {
  id: string;
  nome: string;
  tipo: TipoArestaCorrida;
}

// De quem o pipeline depende e quem depende dele, pelas ligações COMPLETAS — o mapa pode estar
// mostrando só as essenciais, e o painel é onde a lista inteira fica.
export function vizinhosNaCorrida(
  c: CorridaMapa,
  id: string,
): { depende: VizinhoCorrida[]; usadoPor: VizinhoCorrida[] } {
  const nome = new Map(c.execucoes.map((e) => [e.id, e.pipeline_nome]));
  const ordenar = (v: VizinhoCorrida[]) => v.sort((a, b) => a.nome.localeCompare(b.nome, "pt-BR"));
  return {
    depende: ordenar(
      c.arestas
        .filter((a) => a.para === id && nome.has(a.de))
        .map((a) => ({ id: a.de, nome: nome.get(a.de)!, tipo: a.tipo })),
    ),
    usadoPor: ordenar(
      c.arestas
        .filter((a) => a.de === id && nome.has(a.para))
        .map((a) => ({ id: a.para, nome: nome.get(a.para)!, tipo: a.tipo })),
    ),
  };
}
