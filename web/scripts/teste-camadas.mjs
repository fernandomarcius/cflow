// Test of the layer tabs of the pipeline list (src/cflow/surfaces/camadas.ts).
// Runs without a browser or a backend:
//     node scripts/teste-camadas.mjs      (or: npm run test:camadas)
//
// What it proves, and why each thing is here:
//  1. the five filters are the literal CONTRACT (Geral = no layer, no group AND no system flag;
//     Sistema = flag on; Bronze/Silver/Gold = the layer);
//  2. the tabs OVERLAP on purpose: a `sistema` + `silver` pipeline counts in BOTH. "Fixing" this with
//     an `AND NOT` was the first temptation, and it would hide the system silver from whoever
//     opens the Silver tab;
//  3. only "Geral" excludes, and it excludes BOTH things: an item with a layer is NOT "geral" even
//     without the system flag (otherwise Geral would repeat the whole list);
//  4. the tab order is the fixed one (Geral, Sistema, Bronze, Silver, Gold);
//  5. a missing/invalid query string falls back to the default tab, never to an unexplained empty list;
//  6. the count shown on each tab uses the SAME predicate that filters.
import { ABA_PADRAO, ABAS_CAMADA, abaDaApi, abaValida, contarPorAba, naAba } from "../src/cflow/surfaces/camadas.ts";

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

// --- 1/2/3: the predicate ---------------------------------------------------
const lista = [
  { id: "a", nome: "Daily summary", camada: null, sistema: false },
  { id: "b", nome: "without the fields" }, // old API response: neither `camada` nor `sistema`
  { id: "c", nome: "internal job", camada: null, sistema: true },
  { id: "d", nome: "bronze source", camada: "bronze", sistema: false },
  { id: "e", nome: "silver table", camada: "silver", sistema: false },
  { id: "f", nome: "silver of system", camada: "silver", sistema: true }, // overlaps
  { id: "g", nome: "gold table", camada: "gold", sistema: false },
  // A grouped item without a layer: the group keeps it out of Geral.
  { id: "h", nome: "grouped", camada: null, grupo: "any", sistema: false },
];
const naAbaIds = (aba) => lista.filter((p) => naAba(p, aba)).map((p) => p.id);

igual(naAbaIds("geral"), ["a", "b"], "Geral = sem camada, sem grupo E sem marca de sistema");
igual(naAbaIds("sistema"), ["c", "f"], "Sistema = marca ligada, independente da camada");
igual(naAbaIds("bronze"), ["d"], "Bronze = camada bronze");
igual(naAbaIds("silver"), ["e", "f"], "Silver inclui a silver marcada como sistema (sobreposição)");
igual(naAbaIds("gold"), ["g"], "Gold = camada gold");

ok(naAba(lista[5], "sistema") && naAba(lista[5], "silver"), "item sistema+silver conta nas DUAS abas");
ok(!naAba(lista[3], "geral"), "item com camada NÃO aparece em Geral");
ok(!naAba(lista[2], "geral"), "item de sistema NÃO aparece em Geral");
ok(!naAba(lista[7], "geral"), "item com grupo NÃO aparece em Geral, mesmo sem camada");
ok(naAba(lista[1], "geral"), "item sem `camada`/`sistema` cai em Geral");

// --- 4: tab order -----------------------------------------------------------
igual(
  ABAS_CAMADA.map((a) => a.chave),
  ["geral", "sistema", "bronze", "silver", "gold"],
  "ordem das abas é a fixada",
);
igual(
  ABAS_CAMADA.map((a) => a.rotulo),
  ["Geral", "Sistema", "Bronze", "Silver", "Gold"],
  "rótulos das abas capitalizados",
);
ok(ABA_PADRAO === "geral", "aba padrão é Geral");

// --- 5: query string --------------------------------------------------------
igual(abaValida("silver"), "silver", "?aba=silver abre em Silver");
igual(abaValida(null), ABA_PADRAO, "sem ?aba= abre na padrão");
igual(abaValida(""), ABA_PADRAO, "?aba= vazio abre na padrão");
igual(abaValida("bronzee"), ABA_PADRAO, "?aba= inválido abre na padrão (não em lista vazia)");
igual(abaValida("SILVER"), ABA_PADRAO, "a chave é minúscula — não normalizamos em silêncio");
igual(abaValida("geral"), "geral", "?aba=geral continua válido");

// --- 6: count matches the filter -------------------------------------------
const c = contarPorAba(lista);
igual(c, { geral: 2, sistema: 2, bronze: 1, silver: 2, gold: 1 }, "contagem por aba");
for (const a of ABAS_CAMADA)
  ok(
    c[a.chave] === lista.filter((p) => naAba(p, a.chave)).length,
    `contagem da aba ${a.chave} usa o mesmo predicado do filtro`,
  );
// The sum of the tabs is GREATER than the list when there is overlap: if it ever matches, someone
// turned the filters into a partition and the system silver vanished from one of the two tabs.
const soma = ABAS_CAMADA.reduce((s, a) => s + c[a.chave], 0);
ok(soma === lista.length - 1 + 1, `soma das abas (${soma}) = itens (${lista.length}) - 1 sem aba + 1 sobreposição`);

igual(contarPorAba([]), { geral: 0, sistema: 0, bronze: 0, silver: 0, gold: 0 }, "lista vazia");

// --- API key for GET /execucoes?aba= ----------------------------------------
igual(abaDaApi(""), null, "sem aba: sem filtro na API");
igual(abaDaApi("gold"), "gold", "a aba vai como está");

if (falhas) {
  console.error(`${falhas} falha(s)`);
  process.exit(1);
}
console.log("ok — camadas: contrato dos filtros, sobreposição, ordem, querystring e contagem.");
