// Teste do mapa da corrida (src/cflow/surfaces/mapaCorrida.ts). Roda sem navegador e sem banco:
//     node --import ./scripts/resolver-ts.mjs scripts/teste-mapa-corrida.mjs   (ou: pnpm run test:mapa-corrida)
//
// O que ele prova, e por que cada coisa está aqui:
//  1. o estado do nó-pipeline não é o status cru: a corrida PRÉ-CRIA as execuções, então
//     "executando" sem nó começado é fila e "falha" sem nó começado é "não rodou" — senão o mapa
//     mostraria várias falhas onde houve uma;
//  2. as arestas viram `de` do nó, com o tipo (`publicado` sai tracejada), e aresta para quem não
//     está na resposta é ignorada;
//  3. a duração é a do trabalho (desde o primeiro nó), nunca desde o disparo;
//  4. a ordem dos nós é por NOME: ordenar pelo início faria o mapa se rearranjar durante o poll;
//  5. ao abrir, a tela seleciona o que falhou, senão o que roda, senão a raiz;
//  6. legibilidade: a ligação redundante some sem apagar caminho; o caminho do foco acende só o
//     que passa por ele; por família, toda seta vai da esquerda para a direita e as faixas não se
//     sobrepõem; sem famílias suficientes, o agrupamento não se aplica em vez de inventar faixas.
import {
  alvoInicial,
  caminhoDoFoco,
  colunasPorFamilia,
  corridaEmAndamento,
  desenhoDaCorrida,
  estadoNaCorrida,
  familiaDe,
  grupoDaCorrida,
  nosDaCorrida,
  realceDoFoco,
  reduzirArestas,
  resumoEstados,
  rotuloEstadoNaCorrida,
  temArestaPublicado,
  vizinhosNaCorrida,
} from "../src/cflow/surfaces/mapaCorrida.ts";

let falhas = 0;
function ok(cond, oque) {
  if (!cond) {
    falhas++;
    console.error(`FALHA: ${oque}`);
  }
}
function igual(a, b, oque) {
  const mesmo = JSON.stringify(a) === JSON.stringify(b);
  if (!mesmo) {
    falhas++;
    console.error(`FALHA: ${oque}\n  esperado: ${JSON.stringify(b)}\n  obtido:   ${JSON.stringify(a)}`);
  }
}

const DISPARO = "2026-09-15T10:00:00-03:00";
const ex = (id, { status = "concluida", nome = `Pipe ${id}`, comecou = null, fim = null, raiz = false } = {}) => ({
  id,
  pipeline_id: `p-${id}`,
  pipeline_nome: nome,
  versao: 3,
  status,
  erro: status === "falha" ? "quebrou" : null,
  iniciada_em: DISPARO,
  concluida_em: fim,
  comecou_em: comecou,
  origem: "manual",
  agendamento_id: null,
  agendamento_nome: null,
  pagina_id: null,
  pagina_nome: null,
  ator_nome: "Teste",
  cadeia_id: "cad",
  cadeia_raiz: raiz,
  reexecucao_de: null,
  casa_filtro: true,
});

// --- 1: estado do nó-pipeline ----------------------------------------------
igual(estadoNaCorrida(ex("a", { comecou: DISPARO, fim: DISPARO })), "ok", "concluída → ok");
igual(estadoNaCorrida(ex("a", { status: "executando" })), "pendente", "executando sem nó = fila");
igual(
  estadoNaCorrida(ex("a", { status: "executando", comecou: DISPARO })),
  "executando",
  "executando com nó começado = trabalhando",
);
igual(estadoNaCorrida(ex("a", { status: "falha" })), "skip", "falha sem nó começado = não rodou");
igual(
  estadoNaCorrida(ex("a", { status: "falha", comecou: DISPARO, fim: DISPARO })),
  "falha",
  "falha com nó começado = falha",
);
igual(rotuloEstadoNaCorrida("skip").label, "não rodou", "rótulo de quem a corrida não alcançou");
igual(rotuloEstadoNaCorrida("pendente").cls, "off", "fila é cinza, não azul");

// --- 2/3/4: nós do grafo ----------------------------------------------------
const corrida = {
  cadeia_id: "cad",
  raiz_id: "r",
  execucoes: [
    ex("r", { nome: "Zeta raiz", raiz: true, comecou: "2026-09-15T10:02:00-03:00", fim: "2026-09-15T10:02:10-03:00" }),
    ex("b", { nome: "Beta", comecou: "2026-09-15T10:01:00-03:00", fim: "2026-09-15T10:01:30-03:00" }),
    ex("a", { nome: "Alfa", comecou: "2026-09-15T10:00:05-03:00", fim: "2026-09-15T10:00:50-03:00" }),
  ],
  arestas: [
    { de: "a", para: "b", tipo: "pipeline" },
    { de: "b", para: "r", tipo: "pipeline" },
    { de: "a", para: "r", tipo: "publicado" },
    { de: "fantasma", para: "r", tipo: "pipeline" },
  ],
};
const nos = nosDaCorrida(corrida);
igual(nos.map((n) => n.nome), ["Alfa", "Beta", "Zeta raiz"], "ordem por nome, estável no poll");
const noR = nos.find((n) => n.id === "r");
igual([...noR.de].sort(), ["a", "b"], "predecessores da raiz, sem o fantasma");
igual(noR.deTipo, { b: "pipeline", a: "publicado" }, "tipo da aresta por predecessor");
igual(noR.tipo, "raiz · v3", "a raiz se anuncia no card");
igual(nos.find((n) => n.id === "b").tipo, "v3", "dependência mostra só a versão");
igual(nos.find((n) => n.id === "b").duracao_ms, 30000, "duração desde o 1º nó, não desde o disparo");
igual(nos.find((n) => n.id === "b").metrica, "concluída", "métrica é o estado, não linhas");
igual(nos.find((n) => n.id === "a").de, [], "fonte da corrida não tem predecessor");
ok(temArestaPublicado(corrida), "corrida com leitura publicado");
ok(!temArestaPublicado({ ...corrida, arestas: [] }), "sem aresta, sem legenda de publicado");

// --- 5: seleção inicial ------------------------------------------------------
igual(alvoInicial(corrida), "r", "tudo concluído → a raiz");
const comFalha = {
  ...corrida,
  execucoes: [
    ...corrida.execucoes.filter((e) => e.id !== "b"),
    ex("b", { status: "falha", comecou: DISPARO, fim: DISPARO }),
  ],
};
igual(alvoInicial(comFalha), "b", "o que falhou vem primeiro");
const rodando = {
  ...corrida,
  execucoes: [
    ...corrida.execucoes.filter((e) => e.id !== "b"),
    ex("b", { status: "executando", comecou: DISPARO }),
  ],
};
igual(alvoInicial(rodando), "b", "sem falha, o que roda agora");
ok(corridaEmAndamento(rodando), "corrida em andamento");
ok(!corridaEmAndamento(corrida), "corrida encerrada");

// --- resumo e grupo -----------------------------------------------------------
const mista = {
  ...corrida,
  execucoes: [
    ex("r", { raiz: true, status: "falha" }), // não rodou
    ex("b", { status: "falha", comecou: DISPARO, fim: DISPARO }),
    ex("a", { comecou: DISPARO, fim: DISPARO }),
  ],
};
igual(resumoEstados(mista), "1 com falha · 1 concluída · 1 não rodou", "falha primeiro, zerados omitidos");
igual(grupoDaCorrida(corrida).raiz.id, "r", "o grupo usa a raiz marcada");
igual(grupoDaCorrida(corrida).filhas.length, 2, "as outras são filhas");
const semRaiz = {
  ...corrida,
  raiz_id: null,
  execucoes: [ex("x"), { ...ex("y"), iniciada_em: "2026-09-15T10:05:00-03:00" }],
};
igual(grupoDaCorrida(semRaiz).raiz.id, "y", "sem raiz visível, a mais recente faz o papel");
igual(grupoDaCorrida({ ...corrida, execucoes: [] }), null, "corrida vazia não vira grupo");

// --- 6: legibilidade ---------------------------------------------------------
// Ligação redundante: a→r some porque a→b→r já está desenhado; o TIPO das que ficam não muda.
const essenciais = reduzirArestas(corrida.arestas.filter((a) => a.de !== "fantasma"));
igual(
  essenciais.map((a) => `${a.de}>${a.para}:${a.tipo}`),
  ["a>b:pipeline", "b>r:pipeline"],
  "a→r some (redundante com a→b→r)",
);
igual(reduzirArestas([{ de: "x", para: "y" }]), [{ de: "x", para: "y" }], "ligação única nunca é redundante");

// Caminho do foco: em a→b→r mais c→r, focar b acende a, b, r e as duas setas do caminho; c e a
// seta c→r ficam de fora. A seta a→r (pula o foco) também não acende.
const grafo = [
  { id: "a", de: [] },
  { id: "b", de: ["a"] },
  { id: "c", de: [] },
  { id: "r", de: ["b", "c", "a"] },
];
const foco = caminhoDoFoco(grafo, "b");
igual([...foco.nos].sort(), ["a", "b", "r"], "foco acende ancestrais e descendentes");
igual([...foco.arestas].sort(), ["a->b", "b->r"], "só as setas que passam pelo foco");

// Família pelo prefixo do nome.
igual(familiaDe("LOAD SALES"), "load", "família de LOAD");
igual(familiaDe("fato_a"), "fato", "família de fato");
igual(familiaDe("ref_04_valor"), "ref", "família de ref");
igual(familiaDe("etl — recálculo de parcela"), "etl", "família do etl");

// Por família: duas LOAD → dois fatos (um lê o outro) → duas refs → raiz. Toda seta anda para a
// direita, as faixas saem na ordem de dependência e não se sobrepõem.
const pipes = [
  { id: "w1", nome: "LOAD A", de: [] },
  { id: "w2", nome: "LOAD B", de: [] },
  { id: "f1", nome: "fato_um", de: ["w1"] },
  { id: "f2", nome: "fato_dois", de: ["f1", "w2"] },
  { id: "r1", nome: "ref_01", de: ["f2"] },
  { id: "r2", nome: "ref_02", de: ["w1"] },
  { id: "m", nome: "final", de: ["r1", "r2"] },
];
const porFamilia = colunasPorFamilia(pipes, "m");
ok(porFamilia, "há famílias para agrupar");
igual(porFamilia.faixas.map((f) => f.rotulo), ["LOAD · 2", "fato · 2", "ref · 2", "raiz · 1"], "faixas em ordem");
for (const p of pipes) {
  for (const d of p.de) ok(porFamilia.colunas[d] < porFamilia.colunas[p.id], `seta ${d}→${p.id} anda para a direita`);
}
igual(porFamilia.colunas.f2 - porFamilia.colunas.f1, 1, "dentro da faixa, quem lê fica depois");
const cortes = porFamilia.faixas.map((f) => [f.coluna, f.coluna + f.largura]);
for (let i = 1; i < cortes.length; i++) ok(cortes[i][0] >= cortes[i - 1][1], "faixas não se sobrepõem");

// Famílias que se leem mutuamente (x_a → y_a → x_b) viram UMA faixa: em faixas separadas uma seta
// voltaria para trás.
const ciclo = colunasPorFamilia(
  [
    { id: "xa", nome: "x_a", de: [] },
    { id: "ya", nome: "y_a", de: ["xa"] },
    { id: "xb", nome: "x_b", de: ["ya"] },
    { id: "yb", nome: "y_b", de: [] },
  ],
  null,
);
igual(ciclo.faixas.length, 1, "famílias em ciclo compartilham a faixa");
ok(ciclo.colunas.xa < ciclo.colunas.ya && ciclo.colunas.ya < ciclo.colunas.xb, "e as setas seguem para a direita");

// Sem duas famílias de dois ou mais, não há o que agrupar — cai nas colunas por dependência.
igual(
  colunasPorFamilia(
    [
      { id: "a", nome: "Mapa-A", de: [] },
      { id: "b", nome: "Mapa-B", de: ["a"] },
    ],
    "b",
  ),
  null,
  "uma família só não vira faixa",
);

// O desenho: todas as setas para a direita nos dois modos, e faixa só no modo família.
const nosPipes = pipes.map((p) => ({ ...p, estado: "ok", tipo: "v1", executou: true, orfao: false }));
for (const modo of ["familia", "dependencia"]) {
  const d = desenhoDaCorrida(nosPipes, "m", modo);
  for (const p of pipes) {
    for (const de of p.de) ok(d.posicoes[de].x < d.posicoes[p.id].x, `${modo}: ${de}→${p.id} para a direita`);
  }
  igual(d.porFamilia, modo === "familia", `${modo}: agrupamento aplicado só quando pedido`);
  igual(d.faixas.length > 0, modo === "familia", `${modo}: faixa só por família`);
}

// Vizinhos pelo conjunto COMPLETO de ligações (o painel lista a redundante também).
const viz = vizinhosNaCorrida(corrida, "r");
igual(viz.depende.map((v) => `${v.nome}:${v.tipo}`), ["Alfa:publicado", "Beta:pipeline"], "depende de, com o tipo");
igual(vizinhosNaCorrida(corrida, "a").usadoPor.map((v) => v.nome), ["Beta", "Zeta raiz"], "usado por");

// Destaque só quando separa algo: focar a raiz acende tudo, e aí não se destaca nada.
ok(realceDoFoco(grafo, "b") !== null, "foco no meio destaca");
igual(realceDoFoco([{ id: "a", de: [] }, { id: "r", de: ["a"] }], "r"), null, "foco que acende tudo não destaca");
igual(realceDoFoco(grafo, null), null, "sem foco, sem destaque");
igual(realceDoFoco(grafo, "inexistente"), null, "foco fora do mapa, sem destaque");

if (falhas) {
  console.error(`\n${falhas} falha(s) no teste do mapa da corrida.`);
  process.exit(1);
}
console.log("teste-mapa-corrida: ok");
