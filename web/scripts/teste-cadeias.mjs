// Teste do acordeão de corrida da lista de execuções (src/cflow/surfaces/cadeias.ts).
// Roda sem navegador e sem banco:
//     node --import ./scripts/resolver-ts.mjs scripts/teste-cadeias.mjs   (ou: pnpm run test:cadeias)
//
// O que ele prova, e por que cada coisa está aqui:
//  1. execução fora de cadeia continua sendo UMA linha comum — o acordeão é só para quem tem
//     dependências, e não pode virar decoração em toda linha da tela;
//  2. com filtro ativo, o acordeão mostra SÓ as filhas que casam: mostrar as outras faria a tela
//     contradizer o filtro que a pessoa acabou de aplicar;
//  3. a mãe é cabeçalho MESMO sem casar o filtro (filtrar "falha" com a raiz concluída), e é
//     marcada como contexto — sem isso ela passaria por resultado do filtro, que é a mentira que o
//     agrupamento existe para evitar;
//  4. o resumo "N de M no filtro" só aparece quando há recorte — "7 de 7" em toda linha é ruído;
//  5. corrida com falha é detectável pela mãe FECHADA, sem repintar a pill dela (verde é "esta
//     terminou bem"; herdar o vermelho da filha mentiria sobre o desfecho da própria execução);
//  6. o estado do acordeão vai e volta da querystring, e sai da URL quando nada está aberto.
import {
  abertasDaUrl,
  abertasParaUrl,
  alternar,
  composicaoCorrida,
  corridaTemFalha,
  ehCorridaUnica,
  filhasVisiveis,
  janelaCorrida,
  raizEhContexto,
  resumoCorrida,
  rotuloComposicao,
  tempoNaCorrida,
  totalDeExecucoesNaPagina,
} from "../src/cflow/surfaces/cadeias.ts";

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

const ex = (id, status = "concluida", casa = true) => ({
  id,
  pipeline_id: `p-${id}`,
  pipeline_nome: `Pipe ${id}`,
  versao: 1,
  status,
  erro: status === "falha" ? "quebrou" : null,
  iniciada_em: "2026-07-15T10:00:00-03:00",
  concluida_em: "2026-07-15T10:01:00-03:00",
  origem: "manual",
  agendamento_id: null,
  agendamento_nome: null,
  pagina_id: null,
  pagina_nome: null,
  ator_nome: "Teste",
  cadeia_id: null,
  cadeia_raiz: false,
  reexecucao_de: null,
  casa_filtro: casa,
});

// --- 1: execução solta é linha comum ----------------------------------------
const solta = { chave: "e1", raiz: ex("e1"), filhas: [], total_no_grupo: 1, casam_filtro: 1 };
ok(ehCorridaUnica(solta), "execução fora de cadeia é corrida única (sem acordeão)");
igual(filhasVisiveis(solta), [], "corrida única não tem filhas");

// --- 2/3/4: filtro recorta as filhas, mas nunca esconde a mãe ---------------
// Corrida de 3: a raiz concluiu, uma dependência falhou. O filtro é "falha".
const comFiltro = {
  chave: "c1",
  raiz: ex("raiz", "concluida", false), // a mãe NÃO casa o filtro
  filhas: [ex("dep1", "falha", true), ex("dep2", "concluida", false)],
  total_no_grupo: 3,
  casam_filtro: 1,
};
ok(!ehCorridaUnica(comFiltro), "cadeia tem acordeão");
igual(
  filhasVisiveis(comFiltro).map((f) => f.id),
  ["dep1"],
  "com filtro, só a filha que casa aparece",
);
ok(raizEhContexto(comFiltro), "a mãe que não casa o filtro é marcada como contexto");
ok(
  resumoCorrida(comFiltro) === "+2 pipelines · 1 de 3 no filtro",
  `resumo diz o recorte, obtido: ${resumoCorrida(comFiltro)}`,
);

// Sem filtro: todas casam, e o resumo NÃO anuncia recorte nenhum.
const semFiltro = {
  chave: "c2",
  raiz: ex("raiz2"),
  filhas: [ex("dep3"), ex("dep4")],
  total_no_grupo: 3,
  casam_filtro: 3,
};
ok(!raizEhContexto(semFiltro), "sem filtro a mãe é resultado, não contexto");
ok(
  resumoCorrida(semFiltro) === "+2 pipelines",
  `sem recorte o resumo não diz "3 de 3", obtido: ${resumoCorrida(semFiltro)}`,
);
igual(filhasVisiveis(semFiltro).length, 2, "sem filtro todas as filhas aparecem");

// Singular/plural — "+1 pipelines" é o tipo de detalhe que faz a tela parecer descuidada.
const umaFilha = {
  chave: "c3",
  raiz: ex("r3"),
  filhas: [ex("d5")],
  total_no_grupo: 2,
  casam_filtro: 2,
};
ok(resumoCorrida(umaFilha) === "+1 pipeline", `singular, obtido: ${resumoCorrida(umaFilha)}`);

// --- 5: falha escondida no acordeão é detectável pela linha fechada ---------
ok(corridaTemFalha(comFiltro), "corrida com dependência falha é detectável");
ok(!corridaTemFalha(semFiltro), "corrida toda concluída não é marcada como falha");
// E o inverso: a mãe falhou, as filhas não.
ok(
  corridaTemFalha({ ...semFiltro, raiz: ex("r4", "falha") }),
  "falha na própria mãe também conta",
);

// --- barra de composição: descreve a CORRIDA, não o recorte do filtro ---------
// `comFiltro` tem dep2 fora do filtro: ela conta mesmo assim (o recorte já é dito em texto), e a
// mãe fica de fora (o desfecho dela é a pill da própria linha).
igual(
  composicaoCorrida(comFiltro),
  { concluida: 1, executando: 0, falha: 1 },
  "composição conta todas as filhas, casando o filtro ou não, e não conta a mãe",
);
ok(
  rotuloComposicao(composicaoCorrida(comFiltro)) === "1 com falha · 1 concluída",
  `rótulo põe a falha primeiro e usa singular, obtido: ${rotuloComposicao(composicaoCorrida(comFiltro))}`,
);
ok(
  rotuloComposicao(composicaoCorrida(semFiltro)) === "2 concluídas",
  `parte zerada não é anunciada, obtido: ${rotuloComposicao(composicaoCorrida(semFiltro))}`,
);
igual(
  composicaoCorrida({ ...semFiltro, filhas: [ex("d6", "executando"), ex("d7")] }),
  { concluida: 1, executando: 1, falha: 0 },
  "execução em andamento tem parte própria",
);

// --- tempo na corrida: a mãe mede a corrida, a filha mede o que RODOU ----------
// O disparo pré-cria todas no mesmo instante: `iniciada_em` é igual para as três. É o caso medido
// no dev — a filha "b" aparecia com 78 s e rodou 2 s.
const disparo = "2026-07-15T10:00:00-03:00";
const corridaT = {
  chave: "t1",
  raiz: {
    ...ex("r"),
    iniciada_em: disparo,
    comecou_em: "2026-07-15T10:01:19-03:00",
    concluida_em: "2026-07-15T10:01:21-03:00",
  },
  filhas: [
    {
      ...ex("a"),
      iniciada_em: disparo,
      comecou_em: "2026-07-15T10:00:01-03:00",
      concluida_em: "2026-07-15T10:00:53-03:00",
    },
    {
      ...ex("b"),
      iniciada_em: disparo,
      comecou_em: "2026-07-15T10:01:16-03:00",
      concluida_em: "2026-07-15T10:01:18-03:00",
    },
  ],
  total_no_grupo: 3,
  casam_filtro: 3,
};
igual(
  janelaCorrida(corridaT),
  { inicio: disparo, fim: "2026-07-15T10:01:21-03:00" },
  "a mãe mede do disparo ao fim da última execução da corrida",
);
igual(
  tempoNaCorrida(corridaT.filhas[1]),
  {
    tipo: "rodou",
    inicio: "2026-07-15T10:01:16-03:00",
    fim: "2026-07-15T10:01:18-03:00",
    esperaMs: 76000,
  },
  "a filha mede só o que rodou (2 s); os 76 s de fila ficam à parte",
);
const emCurso = {
  ...corridaT,
  filhas: [
    ...corridaT.filhas,
    { ...ex("c", "executando"), iniciada_em: disparo, concluida_em: null, comecou_em: null },
  ],
};
igual(janelaCorrida(emCurso).fim, null, "com uma execução rodando, a corrida está em curso");
igual(
  tempoNaCorrida(emCurso.filhas[2]),
  { tipo: "aguardando" },
  "rodando sem nenhum nó começado = aguardando as dependências",
);
igual(
  tempoNaCorrida({ ...ex("d", "falha"), comecou_em: null }),
  { tipo: "sem_nos" },
  "terminou sem nenhum nó começar = a corrida parou antes dela",
);

// --- contagem da página: corridas ≠ execuções -------------------------------
igual(totalDeExecucoesNaPagina([solta, comFiltro, semFiltro]), 1 + 2 + 3, "execuções visíveis na página");

// --- 6: estado do acordeão na querystring ----------------------------------
igual([...abertasDaUrl("a,b")], ["a", "b"], "lê as abertas da URL");
igual([...abertasDaUrl(null)], [], "URL sem o parâmetro = nada aberto");
igual([...abertasDaUrl("")], [], "parâmetro vazio = nada aberto");
igual([...abertasDaUrl(" a , , b ")], ["a", "b"], "ignora espaço e item vazio");
ok(abertasParaUrl(new Set()) === null, "nada aberto sai da URL (não vira `?ab=`)");
ok(abertasParaUrl(new Set(["x"])) === "x", "uma aberta");
igual([...alternar(new Set(["x"]), "y")], ["x", "y"], "alternar abre");
igual([...alternar(new Set(["x", "y"]), "x")], ["y"], "alternar fecha");
// Ida e volta pela URL preserva o estado — é o que faz o link compartilhado abrir igual.
const abertas = new Set(["c1", "c2"]);
igual([...abertasDaUrl(abertasParaUrl(abertas))], [...abertas], "ida e volta pela querystring");

if (falhas) {
  console.error(`\n${falhas} falha(s)`);
  process.exit(1);
}
console.log("OK — nenhuma falha");
