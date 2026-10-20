// Teste do estado das listas de View e de Cubo a partir do `resumo` da API
// (src/cflow/surfaces/listasResumo.ts), sem navegador:
//     node --import ./scripts/resolver-ts.mjs scripts/teste-listas-resumo.mjs   (ou: npm run test:listas-resumo)
//
// Prova que a linha diz o mesmo que dizia quando era montada com detalhe + versões por item:
// rascunho à frente só quando a última versão passa a publicada; sem cubo/fonte vira "" (a tela
// já tratava); item sem resumo vira "não lido" (nunca zero); e as colunas de dependência são
// tudo-ou-nada.
import {
  dependentesDosCubos,
  estadoCuboDoResumo,
  estadosDoResumo,
  estadoViewDoResumo,
  usoEmPaginas,
} from "../src/cflow/surfaces/listasResumo.ts";

let falhas = 0;
function igual(real, esperado, oque) {
  if (JSON.stringify(real) !== JSON.stringify(esperado)) {
    falhas++;
    console.error(`FALHA: ${oque}\n  esperado ${JSON.stringify(esperado)}\n  veio     ${JSON.stringify(real)}`);
  }
}

const pub = (numero) => ({ numero, criado_em: "2026-09-26T12:00:00+00:00" });
const rv = (x) => ({
  cubo_id: "c1", linhas: 2, colunas: 1, valores: 3, filtros_liberados: 1,
  ultimo: 2, publicada: pub(2), paginas: 0, paginas_nao_verificadas: 0, ...x,
});

// --- View -------------------------------------------------------------------------------------
igual(
  estadoViewDoResumo(rv({})),
  { cuboId: "c1", agrupa: 2, divide: 1, medidas: 3, liberados: 1, publicada: { numero: 2, em: "2026-09-26T12:00:00+00:00" }, rascunhoAdiante: null },
  "view publicada, sem rascunho à frente",
);
igual(estadoViewDoResumo(rv({ ultimo: 5 })).rascunhoAdiante, 5, "rascunho à frente = número da última versão");
igual(estadoViewDoResumo(rv({ ultimo: 1, publicada: null })).rascunhoAdiante, null, "nunca publicada: não há 'à frente'");
igual(estadoViewDoResumo(rv({ ultimo: 1, publicada: null })).publicada, null, "nunca publicada");
igual(estadoViewDoResumo(rv({ cubo_id: null })).cuboId, "", "sem cubo vira '' (a tela diz 'sem cubo')");

// --- Cubo -------------------------------------------------------------------------------------
const rc = (x) => ({ dimensoes: 2, medidas: 1, pipeline_id: "p1", no_id: "saida", ultimo: 3, publicada: pub(2), views: 0, ...x });
igual(
  estadoCuboDoResumo(rc({})),
  { dims: 2, meds: 1, pipelineId: "p1", noId: "saida", publicada: { numero: 2, em: "2026-09-26T12:00:00+00:00" }, rascunhoAdiante: 3 },
  "cubo com rascunho à frente",
);
igual(estadoCuboDoResumo(rc({ pipeline_id: null, no_id: null })).pipelineId, "", "sem fonte vira ''");

// --- Fases por linha ----------------------------------------------------------------------------
igual(
  estadosDoResumo([{ id: "a", resumo: rv({}) }, { id: "b" }], estadoViewDoResumo).b,
  { fase: "erro" },
  "item sem resumo (API sem a rota) vira 'não lido', não zero",
);
igual(estadosDoResumo([{ id: "a", resumo: rv({}) }], estadoViewDoResumo).a.fase, "ok", "item com resumo: ok");

// --- Dependências: tudo-ou-nada -------------------------------------------------------------------
igual(
  usoEmPaginas([
    { id: "a", nome: "A", resumo: rv({ paginas: 2, paginas_nao_verificadas: 1 }) },
    { id: "b", nome: "B", resumo: rv({ paginas: 0, paginas_nao_verificadas: 1 }) },
  ]),
  { fase: "ok", dado: { mapa: { a: 2 }, ocultas: 1 } },
  "uso em páginas: mapa só com quem aparece, ocultas do servidor",
);
igual(usoEmPaginas([{ id: "a", nome: "A", resumo: rv({}) }, { id: "b", nome: "B" }]), { fase: "erro" }, "uso em páginas sem resumo em algum item: erro");
igual(usoEmPaginas([]), { fase: "ok", dado: { mapa: {}, ocultas: 0 } }, "lista vazia");
igual(
  dependentesDosCubos([{ id: "c1", nome: "C", resumo: rc({ views: 3 }) }, { id: "c2", nome: "D", resumo: rc({}) }]),
  { fase: "ok", dado: { c1: 3 } },
  "views que dependem de cada cubo",
);
igual(dependentesDosCubos([{ id: "c1", nome: "C" }]), { fase: "erro" }, "dependentes sem resumo: erro");

if (falhas) {
  console.error(`\n${falhas} falha(s) no estado das listas a partir do resumo.`);
  process.exit(1);
}
console.log("teste-listas-resumo: ok — linha igual à montada item a item; sem resumo, 'não lido'.");
