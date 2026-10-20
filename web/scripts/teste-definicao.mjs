// Teste da ida e volta da definição do pipeline pelo Studio (src/cflow/definicao.ts).
// Roda sem navegador e sem banco:
//     node --import ./scripts/resolver-ts.mjs scripts/teste-definicao.mjs   (ou: npm run test:definicao)
//
// O que ele prova, e por que cada coisa está aqui:
//  1. ler e montar de novo devolve a MESMA definição — inclusive o que o Studio não edita. É a
//     regressão que custou caro: `montante_publica: false` sumia no primeiro salvar e a cadeia
//     voltava a mover ponteiros congelados;
//  2. o que o canvas edita vence, e só o que ele edita muda;
//  3. nó e aresta novos (sem nada carregado) continuam saindo como antes;
//  4. o salvar da aba Parâmetros troca SÓ os parâmetros;
//  5. a limpeza de parâmetro que já existia continua valendo (default de segredo nunca vai, padrão
//     de inteiro vira número, metadado em branco some, `opcoes` de quem deixou de ser `opcoes` some,
//     mapa de outra origem da derivação some) — e o que a aba não edita passa intacto.
import {
  definicaoComParametros,
  extrasDaDefinicao,
  lerDefinicao,
  montarDefinicao,
  parametroParaSalvar,
} from "../src/cflow/definicao.ts";

let falhas = 0;
function ok(cond, oque) {
  if (!cond) {
    falhas++;
    console.error(`FALHA: ${oque}`);
  }
}
// JSON canônico: ordem de chave não é conteúdo (a definição mora em jsonb, que reordena), e
// `undefined` some no JSON que vai para a API.
function canon(v) {
  if (Array.isArray(v)) return v.map(canon);
  if (v && typeof v === "object")
    return Object.fromEntries(
      Object.keys(v)
        .filter((k) => v[k] !== undefined)
        .sort()
        .map((k) => [k, canon(v[k])]),
    );
  return v;
}
function igual(a, b, oque) {
  const A = JSON.stringify(canon(a));
  const B = JSON.stringify(canon(b));
  if (A !== B) {
    falhas++;
    console.error(`FALHA: ${oque}\n  esperado: ${B}\n  obtido:   ${A}`);
  }
}

// Uma raiz do jeito que ela existe em produção: marca de topo que o Studio não edita, fonte `publicado`
// com partição, saída gold particionada, escrita com `limpar_escopo`, aresta e parâmetro com campo
// que nenhum formulário conhece. O config é opaco para esta camada — tem de voltar byte a byte.
const RAIZ = {
  montante_publica: false,
  bibliotecas: ["b9d1c3f0-0000-4000-8000-000000000001"],
  nos: [
    {
      id: "ref_05",
      tipo: "fonte",
      rotulo: "Ref 5 (gold)",
      descricao: "lê o resumo congelado",
      config: {
        origem: "publicado",
        dataset: "ref05_resumo_previsto",
        camada: "gold",
        particao: { entidade_id: ":entidade_id", competencia: ":competencia_m1" },
      },
      posicao: { x: 40, y: 0 },
    },
    {
      id: "saida",
      tipo: "saida",
      rotulo: "Publica",
      config: {
        destino: "lake",
        camada: "gold",
        dataset: "antecipacao_lamina",
        particao: [
          { origem: "parametro", nome: "entidade_id" },
          { origem: "coluna", nome: "ObraId", chave: "item_id" },
        ],
      },
      posicao: { x: 40, y: 400 },
      // campo de nó que nenhum formulário edita
      origem_gerador: "build_raizes.py",
    },
    {
      id: "grava",
      tipo: "escrita_sql",
      config: {
        conexao_id: "c-escrita",
        modo: "tabela",
        schema: "dbo",
        tabela: "CFLOW_ANTECIPACAO_LAMINA",
        estrategia: "upsert",
        chave: ["operacaoId", "obraId"],
        limpar_escopo: { operacaoId: ":entidade_id" },
      },
    },
  ],
  arestas: [
    { de: "ref_05", para: "saida", rotulo: "resumo" },
    { de: "saida", para: "grava" },
  ],
  parametros: [
    { nome: "entidade_id", tipo: "inteiro", obrigatorio: true },
    { nome: "competencia", tipo: "competencia", obrigatorio: true, marca_de_gerador: "v3" },
  ],
};

// --- 1. ida e volta é identidade -----------------------------------------------------------------
{
  const volta = montarDefinicao(lerDefinicao(RAIZ));
  igual(volta, RAIZ, "ler + montar devolve a definição inteira, extras inclusive");
  ok(volta.montante_publica === false, "montante_publica:false sobrevive ao salvar");
  igual(extrasDaDefinicao(RAIZ), { montante_publica: false, bibliotecas: RAIZ.bibliotecas }, "extras do topo");
  // Ida e volta repetida não acumula nem perde nada.
  igual(montarDefinicao(lerDefinicao(volta)), RAIZ, "duas voltas = uma volta");
}

// --- 2. o que o canvas edita vence, e só isso muda -----------------------------------------------
{
  const lida = lerDefinicao(RAIZ);
  lida.nos[1] = { ...lida.nos[1], descricao: "nova descrição", config: { ...lida.nos[1].config, dataset: "outro" } };
  lida.arestas = lida.arestas.filter((a) => a.para !== "grava");
  const volta = montarDefinicao(lida);
  const saida = volta.nos.find((n) => n.id === "saida");
  ok(saida.descricao === "nova descrição", "a edição da descrição chega");
  ok(saida.config.dataset === "outro", "a edição do config chega");
  ok(saida.origem_gerador === "build_raizes.py", "o extra do nó editado continua");
  ok(volta.arestas.length === 1 && volta.arestas[0].rotulo === "resumo", "aresta removida some; extra da outra fica");
  ok(volta.montante_publica === false, "editar nó não mexe no topo");
}

// --- 3. nó e aresta novos saem como antes --------------------------------------------------------
{
  const lida = lerDefinicao({ nos: [], arestas: [] });
  const volta = montarDefinicao({
    ...lida,
    nos: [{ id: "n1", tipo: "sql", sql: "SELECT 1 AS um", posicao: { x: 40, y: 0 } }],
    arestas: [{ de: "n1", para: "n2" }],
  });
  igual(
    volta,
    { nos: [{ id: "n1", tipo: "sql", sql: "SELECT 1 AS um", posicao: { x: 40, y: 0 } }], arestas: [{ de: "n1", para: "n2" }], parametros: [] },
    "pipeline novo: nada inventado",
  );
  igual(lerDefinicao(null).extras, {}, "definição ausente não quebra");
}

// --- 4. o salvar da aba Parâmetros troca SÓ os parâmetros ----------------------------------------
{
  const novos = [{ nome: "entidade_id", tipo: "inteiro", obrigatorio: true, rotulo: "Operação" }];
  const volta = definicaoComParametros(RAIZ, novos);
  igual(volta, { ...RAIZ, parametros: novos }, "nós, arestas e topo intactos");
  ok(volta.montante_publica === false, "montante_publica:false sobrevive ao salvar da aba Parâmetros");
  igual(definicaoComParametros(undefined, []), { nos: [], arestas: [], parametros: [] }, "sem definição");
}

// --- 5. limpeza de parâmetro: o que existia + o que a aba não edita ------------------------------
{
  // o que a aba não edita passa
  const p = parametroParaSalvar({ nome: " competencia ", tipo: "competencia", obrigatorio: true, marca_de_gerador: "v3" });
  igual(p, { nome: "competencia", tipo: "competencia", obrigatorio: true, marca_de_gerador: "v3" }, "extra do parâmetro fica");

  // credencial: nem default nem valor de preview vão para a definição (a trilha é permanente)
  igual(
    parametroParaSalvar({ nome: "senha_api", tipo: "texto", segredo: true, default: "x", valor_preview: "y" }),
    { nome: "senha_api", tipo: "texto", obrigatorio: false, segredo: true },
    "segredo sem default/valor_preview",
  );

  // default de inteiro vira número; texto que não é inteiro segue como texto
  ok(parametroParaSalvar({ nome: "op", tipo: "inteiro", default: " 26 " }).default === 26, "default inteiro vira número");
  ok(parametroParaSalvar({ nome: "op", tipo: "inteiro", default: "x" }).default === "x", "default não inteiro fica texto");

  // metadado em branco some
  const branco = parametroParaSalvar({ nome: "a", tipo: "texto", rotulo: "  ", descricao: "", valor_preview: "" });
  ok(!("rotulo" in branco) && !("descricao" in branco) && !("valor_preview" in branco), "metadado em branco some");

  // `opcoes` de quem deixou de ser `opcoes` some (é campo editado, não extra)
  ok(!("opcoes" in parametroParaSalvar({ nome: "a", tipo: "texto", opcoes: ["x"] })), "opcoes some fora do tipo opcoes");
  igual(parametroParaSalvar({ nome: "a", tipo: "opcoes", opcoes: ["x", " ", "y"] }).opcoes, ["x", "y"], "opcoes sem vazio");

  // opcoes_fonte: forma por origem, com extra preservado
  igual(
    parametroParaSalvar({
      nome: "op",
      tipo: "inteiro",
      opcoes_fonte: { conexao_id: "c1", coluna_valor: " id ", coluna_rotulo: "apelido", sql: " ", ttl: 60 },
    }).opcoes_fonte,
    { conexao_id: "c1", coluna_valor: "id", coluna_rotulo: "apelido", ttl: 60 },
    "opcoes_fonte de conexão: sql em branco some, extra fica",
  );
  igual(
    parametroParaSalvar({ nome: "op", tipo: "inteiro", opcoes_fonte: { origem: "no", conexao_id: "c1", no_id: " n3 ", coluna: "id" } }).opcoes_fonte,
    { origem: "no", conexao_id: "", no_id: "n3", coluna_valor: "id" },
    "opcoes_fonte de nó",
  );
  ok(!("opcoes_fonte" in parametroParaSalvar({ nome: "op", tipo: "texto", opcoes_fonte: { conexao_id: "" } })), "fonte incompleta some");

  // derivação: o mapa de OUTRA origem some; `normalizar` (sem campo na aba) fica
  igual(
    parametroParaSalvar({
      nome: "sistema",
      tipo: "texto",
      derivacao: { origem: "fonte", de: "entidade_id", campo_id: "id", campo_valor: "sis", conexao_id: " c9 ", mapa: "velho", normalizar: "sistema", juntar: "" },
    }).derivacao,
    { origem: "fonte", de: "entidade_id", campo_id: "id", campo_valor: "sis", conexao_id: "c9", normalizar: "sistema" },
    "derivação de fonte: sem mapa, com normalizar",
  );
  igual(
    parametroParaSalvar({ nome: "s", tipo: "texto", derivacao: { de: "a", campo_id: "b", campo_valor: "c", mapa: " m ", substituicoes: [{ de: "X", para: "Y" }, { de: " ", para: "Z" }], caixa_alta: true } }).derivacao,
    { de: "a", campo_id: "b", campo_valor: "c", mapa: "m", substituicoes: [{ de: "X", para: "Y" }], caixa_alta: true },
    "derivação de arquivo (default)",
  );
}

// --- 6. derivado de calendário: só `de` e `meses`; campo de mapa não vai --------------
{
  igual(
    parametroParaSalvar({
      nome: "competencia_m1",
      tipo: "texto",
      derivacao: { origem: "competencia", de: " competencia ", meses: -1, mapa: "velho", campo_id: "id", campo_valor: "v" },
    }),
    { nome: "competencia_m1", tipo: "texto", obrigatorio: false, derivacao: { origem: "competencia", de: "competencia", meses: -1 } },
    "derivado de calendário: de + meses, sem campos de mapa",
  );
  ok(
    !("meses" in parametroParaSalvar({ nome: "m", tipo: "texto", derivacao: { origem: "competencia", de: "c" } }).derivacao),
    "sem meses não inventa um número — a validação recusa alto",
  );
  ok(
    parametroParaSalvar({ nome: "m", tipo: "texto", derivacao: { origem: "competencia", de: "c", meses: -2, marca: 1 } }).derivacao.marca === 1,
    "extra da derivação de calendário fica, como nas outras origens",
  );
}

if (falhas) {
  console.error(`\n${falhas} falha(s) na ida e volta da definição.`);
  process.exit(1);
}
console.log("ok — ida e volta da definição: o que o Studio não edita volta intacto.");
