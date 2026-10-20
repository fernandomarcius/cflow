// Teste da lógica pura do carimbo de obsoleto na tela (src/cflow/surfaces/carimbo.ts).
// Roda sem navegador e sem banco:
//     node --import ./scripts/resolver-ts.mjs scripts/teste-carimbo.mjs   (ou: npm run test:carimbo)
//
// O que ele prova:
//  1. as saídas lake saem da definição com a camada default e as chaves NA ORDEM declarada, pela
//     mesma regra do engine (`chave` ou `nome` minúsculo) — a tela e o caminho nomeiam o mesmo
//     segmento;
//  2. o escopo digitado vira a lista `chave=valor` que a API recebe, na ordem, e o que nem chega a
//     ser pedido é recusado com mensagem;
//  3. a tela não decide: as ações liberadas são as que o servidor liberou, e a razão de não poder é
//     a mensagem que ele devolveu.
import {
  acoesDoPlano,
  escopoComoTexto,
  lerEscopo,
  resumoDescoberta,
  rotuloEstado,
  saidasLake,
} from "../src/cflow/surfaces/carimbo.ts";

let falhas = 0;
function igual(a, b, oque) {
  const A = JSON.stringify(a);
  const B = JSON.stringify(b);
  if (A !== B) {
    falhas++;
    console.error(`FALHA: ${oque}\n  esperado: ${B}\n  obtido:   ${A}`);
  }
}

// --- 1. saídas lake ------------------------------------------------------------------------------
igual(
  saidasLake({
    nos: [
      { id: "src", tipo: "sql" },
      {
        id: "out",
        tipo: "saida",
        rotulo: "Publica",
        config: {
          destino: "lake",
          camada: "gold",
          dataset: "vendas",
          particao: [
            { origem: "parametro", nome: "Entidade_ID" },
            { origem: "parametro", nome: "data_registro" },
            { origem: "coluna", nome: "OBRAID", chave: "item_id" },
          ],
        },
      },
      // mesmo dataset/camada de novo: não repete
      { id: "out2", tipo: "saida", config: { destino: "lake", camada: "gold", dataset: "vendas" } },
      // sem camada = silver; sem partição = chaves vazias
      { id: "s", tipo: "saida", config: { destino: "LAKE", dataset: "cri_geral" } },
      // arquivo e sem dataset ficam de fora
      { id: "a", tipo: "saida", config: { destino: "arquivo", nome: "x" } },
      { id: "b", tipo: "saida", config: { destino: "lake" } },
    ],
  }),
  [
    { noId: "out", rotulo: "Publica", dataset: "vendas", camada: "gold", chaves: ["entidade_id", "data_registro", "item_id"] },
    { noId: "s", rotulo: "s", dataset: "cri_geral", camada: "silver", chaves: [] },
  ],
  "saídas lake da definição",
);
igual(saidasLake(null), [], "definição ausente");
igual(saidasLake({ nos: "x" }), [], "nós que não são lista");

// --- 2. escopo digitado --------------------------------------------------------------------------
igual(lerEscopo("entidade_id=11\n data_registro = 2026-08-06 "), { itens: ["entidade_id=11", "data_registro=2026-08-06"], erro: null }, "linhas, na ordem");
igual(lerEscopo("entidade_id=11, item_id=A_80_X").itens, ["entidade_id=11", "item_id=A_80_X"], "vírgula separa");
igual(lerEscopo("chave=a=b").itens, ["chave=a=b"], "o 1º '=' separa; o resto é valor (o servidor recusa '=' no valor)");
igual(lerEscopo("   ").erro !== null, true, "vazio recusado");
igual(lerEscopo("entidade_id").erro, '"entidade_id" não está no formato chave=valor.', "sem =");
igual(lerEscopo("=11").erro !== null, true, "sem chave");
igual(lerEscopo("entidade_id=").erro !== null, true, "sem valor");
igual(lerEscopo("a=1\na=2").erro, 'A chave "a" aparece duas vezes.', "chave repetida");

// --- 3. a tela repete o servidor -----------------------------------------------------------------
igual(
  acoesDoPlano({ estado: "vigente", pode_carimbar: true, pode_descarimbar: false, bloqueio: null }),
  { carimbar: true, descarimbar: false, motivo: null },
  "vigente e carimbável",
);
igual(
  acoesDoPlano({
    estado: "vigente",
    pode_carimbar: false,
    pode_descarimbar: false,
    bloqueio: { codigo: "escopo_do_layout_vigente", mensagem: "O layout entidade_id ainda é publicado por 'carga'." },
  }).motivo,
  "O layout entidade_id ainda é publicado por 'carga'.",
  "bloqueio: a razão é a do servidor",
);
igual(
  acoesDoPlano({ estado: "obsoleto", pode_carimbar: false, pode_descarimbar: true, bloqueio: null }),
  { carimbar: false, descarimbar: true, motivo: null },
  "carimbado: só desfazer",
);
igual(
  acoesDoPlano({ estado: "ausente", pode_carimbar: false, pode_descarimbar: false, bloqueio: null }).motivo,
  rotuloEstado("ausente"),
  "ausente sem bloqueio: diz o estado",
);

// --- 4. descoberta dos escopos do layout anterior -------------------------------------------------
const layout = (execucoes, sem_indice = 0) => [{ chaves: ["entidade_id"], execucoes, sem_indice }];
const esc = (op, estado, pode_carimbar) => ({ particao: [`entidade_id=${op}`], estado, pode_carimbar });
igual(
  resumoDescoberta({
    escopos: [esc("2", "vigente", true), esc("11", "obsoleto", false), esc("26", "vigente", false)],
    ausentes: 0,
    truncado: false,
    layouts_anteriores: layout(14),
  }),
  {
    carimbaveis: [["entidade_id=2"]],
    texto: "3 escopos de layout anterior: 1 a carimbar, 1 já carimbado, 1 bloqueado (14 execuções consultadas).",
    avisos: [],
  },
  "carimbáveis são os que o servidor liberou; o resto é contado",
);
igual(
  resumoDescoberta({ escopos: [], ausentes: 0, truncado: false, layouts_anteriores: [] }).texto,
  "Nenhuma execução gravou este dataset em outro layout: não há escopo de layout anterior.",
  "sem layout anterior",
);
igual(
  resumoDescoberta({ escopos: [], ausentes: 2, truncado: true, layouts_anteriores: layout(1, 1) }),
  {
    carimbaveis: [],
    texto: "Nenhum ponteiro de layout anterior no lake (1 execução consultada).",
    avisos: [
      "1 execução concluída sem índice no lake: os escopos de coluna dela não estão na lista.",
      "A lista parou no teto de execuções consultadas: pode haver mais escopos.",
      "2 escopos sem ponteiro no lake — nada a carimbar neles.",
    ],
  },
  "cobertura incompleta é dita, nunca uma lista vazia com cara de completa",
);
igual(escopoComoTexto(["entidade_id=11", "lote=A"]), "entidade_id=11\nlote=A", "escopo volta ao campo, uma chave por linha");
igual(lerEscopo(escopoComoTexto(["entidade_id=11", "lote=A"])).itens, ["entidade_id=11", "lote=A"], "ida e volta com o campo");

if (falhas) {
  console.error(`\n${falhas} falha(s) na lógica do carimbo.`);
  process.exit(1);
}
console.log("OK — nenhuma falha");
