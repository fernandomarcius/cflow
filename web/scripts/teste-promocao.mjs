// Teste da lógica pura da tela de Promoção (src/cflow/surfaces/promocao.ts), sem navegador nem banco:
//     node --import ./scripts/resolver-ts.mjs scripts/teste-promocao.mjs   (ou: npm run test:promocao)
//
// Prova: o arquivo que não é pacote é recusado com frase que diz o que fazer (e nunca vai ao
// servidor); o resumo do plano conta cada ação como o servidor a devolveu; e o botão de importar só
// habilita quando há o que gravar.
import { lerPacote, nomeArquivo, resumirPlano, ROTULO_ACAO } from "../src/cflow/surfaces/promocao.ts";

let falhas = 0;
function ok(cond, oque) {
  if (!cond) {
    falhas++;
    console.error(`FALHA: ${oque}`);
  }
}

// --- lerPacote ---------------------------------------------------------------------------------
ok(/não é JSON/.test(lerPacote("{nao json").erro ?? ""), "JSON quebrado");
ok(/não é um pacote/.test(lerPacote("[1,2]").erro ?? ""), "lista não é pacote");
ok(/Versão de pacote não reconhecida \(3\)/.test(lerPacote('{"versao":3}').erro ?? ""), "versão 3");
ok(/precisa das listas/.test(lerPacote('{"versao":2,"pipelines":{}}').erro ?? ""), "pipelines não lista");
ok(lerPacote('{"versao":1,"conexoes":[],"pipelines":[]}').pacote?.versao === 1, "v1 aceito");
ok(lerPacote('{"versao":2}').pacote?.versao === 2, "v2 sem listas é lido (servidor completa)");

// --- resumirPlano ------------------------------------------------------------------------------
const plano = {
  versao_pacote: 2,
  atualizar: false,
  conexoes: [
    { nome: "a", tipo: "postgres", acao: "reusar" },
    { nome: "b", tipo: "arquivo", acao: "criar" },
  ],
  pipelines: [
    { nome: "p1", acao: "criar", diferencas: [] },
    { nome: "p2", acao: "manter", diferencas: [] },
    { nome: "p3", acao: "manter_diferente", diferencas: ["definicao"] },
    { nome: "p4", acao: "atualizar", diferencas: ["camada"] },
  ],
  pendencias: [],
};
const r = resumirPlano(plano);
ok(r.criar === 1 && r.manter === 1 && r.diferentes === 1 && r.atualizar === 1, "contagem por ação");
ok(r.conexoesNovas === 1 && r.conexoesReusadas === 1, "contagem de conexões");
ok(r.temEscrita === true, "há o que gravar");
ok(
  resumirPlano({ ...plano, conexoes: [plano.conexoes[0]], pipelines: [plano.pipelines[1], plano.pipelines[2]] })
    .temEscrita === false,
  "só manter/diferente e conexão reusada: nada a gravar",
);
ok(Object.keys(ROTULO_ACAO).length === 4 && !/_/.test(Object.values(ROTULO_ACAO).join("")), "rótulo sem código da API");

// --- nomeArquivo -------------------------------------------------------------------------------
ok(nomeArquivo(new Date(2026, 8, 1, 7, 5)) === "cflow-promocao-2026-09-01-0705.json", "nome do arquivo");

if (falhas) {
  console.error(`\n${falhas} falha(s) na lógica da Promoção.`);
  process.exit(1);
}
console.log("ok — promoção: pacote lido, plano resumido, importar só com o que gravar.");
