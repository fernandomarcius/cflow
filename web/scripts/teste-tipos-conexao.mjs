// Teste das abas por TIPO da listagem de conexões (src/cflow/surfaces/tipos-conexao.ts).
// Roda sem navegador e sem banco:
//     node scripts/teste-tipos-conexao.mjs      (ou: pnpm run test:tipos-conexao)
//
// O que ele prova, e por que cada coisa está aqui:
//  1. só aparece aba de tipo PRESENTE no cadastro — é a regra que o seletor substituído já
//     seguia, e sete abas com quatro vazias seria ruído (diferente das camadas, onde as cinco são
//     fixas porque o vocabulário é fechado e precisa ser visto inteiro);
//  2. a aba "Todas" conta a lista INTEIRA e vem primeiro;
//  3. a contagem de cada aba usa o MESMO predicado que filtra — aba dizendo "(2)" e abrindo com 5
//     linhas é a régua mentindo, e é exatamente o que duas contas separadas produzem;
//  4. as abas por tipo PARTICIONAM (ao contrário das camadas, que se sobrepõem): a soma das abas
//     de tipo é o tamanho da lista, porque uma conexão tem um tipo só;
//  5. `objeto` — o destino do lake, criado por script e não pelo formulário — aparece na listagem
//     e no filtro. Ele não está em `TIPOS` (o que se pode criar no Studio), e confundir as duas
//     listas esconderia a conexão do bucket de quem precisa vê-la;
//  6. tipo DESCONHECIDO não some: ganha aba própria com o valor cru. Um tipo novo no backend não
//     pode deixar a conexão alcançável só pela aba "Todas";
//  7. querystring inválida (`?t=xyz`, link antigo) cai em "Todas", nunca numa lista vazia sem
//     explicação.
import {
  ABA_TODAS,
  abaValida,
  abasDeTipo,
  daAba,
  rotuloTipo,
} from "../src/cflow/surfaces/tipos-conexao.ts";

let falhas = 0;
function ok(cond, oque) {
  if (!cond) {
    falhas++;
    console.error(`FALHA: ${oque}`);
  }
}
function igual(a, b, oque) {
  if (JSON.stringify(a) !== JSON.stringify(b)) {
    falhas++;
    console.error(`FALHA: ${oque}\n  esperado: ${JSON.stringify(b)}\n  obtido:   ${JSON.stringify(a)}`);
  }
}

const lista = [
  { tipo: "postgres" },
  { tipo: "postgres" },
  { tipo: "sqlserver" },
  { tipo: "api_rest" },
  { tipo: "smtp" },
  { tipo: "objeto" },
];

const abas = abasDeTipo(lista);
const chaves = abas.map((a) => a.chave);

// 1 + 2: "Todas" primeiro, com a lista inteira; e nenhuma aba de tipo ausente.
igual(chaves[0], ABA_TODAS, "a primeira aba é 'Todas'");
igual(abas[0].contagem, lista.length, "'Todas' conta a lista inteira");
ok(!chaves.includes("arquivo"), "tipo ausente do cadastro NÃO vira aba");
ok(!chaves.includes("api_query"), "tipo ausente do cadastro NÃO vira aba (api_query)");

// Ordem fixada: bancos, APIs, arquivo, e a infraestrutura por último.
igual(chaves, ["", "postgres", "sqlserver", "api_rest", "smtp", "objeto"], "ordem das abas");

// 3: contagem exibida == itens que o predicado deixa passar, aba por aba.
for (const a of abas) {
  const filtrados = lista.filter((c) => daAba(c, a.chave, chaves)).length;
  igual(filtrados, a.contagem, `aba '${a.chave || "todas"}': contagem bate com o filtro`);
}

// 4: tipo PARTICIONA — a soma das abas de tipo é a lista inteira (nada se sobrepõe, nada some).
const soma = abas.filter((a) => a.chave !== ABA_TODAS).reduce((s, a) => s + a.contagem, 0);
igual(soma, lista.length, "as abas de tipo particionam a lista");

// 5: o destino do lake aparece, com rótulo em pt-BR.
ok(chaves.includes("objeto"), "a conexão 'objeto' (lake) tem aba");
igual(rotuloTipo("objeto"), "Objeto (lake)", "rótulo pt-BR do tipo objeto");

// 6: tipo que este módulo não conhece continua alcançável, com o valor CRU como rótulo.
const comNovo = abasDeTipo([...lista, { tipo: "kafka" }]);
ok(comNovo.some((a) => a.chave === "kafka"), "tipo desconhecido ganha aba própria");
igual(rotuloTipo("kafka"), "kafka", "tipo desconhecido mostra o valor cru, não 'outro'");
igual(comNovo.at(-1).chave, "kafka", "tipo desconhecido entra no fim da régua");

// 7: querystring estranha cai em "Todas" — e o predicado, por consequência, não filtra nada.
igual(abaValida("xyz", chaves), ABA_TODAS, "aba inválida cai em 'Todas'");
igual(abaValida(null, chaves), ABA_TODAS, "aba ausente cai em 'Todas'");
igual(abaValida("smtp", chaves), "smtp", "aba válida é preservada");
igual(lista.filter((c) => daAba(c, "xyz", chaves)).length, lista.length, "aba inválida não filtra");

// Lista vazia: só 'Todas', com zero. Não quebra e não inventa aba.
igual(abasDeTipo([]), [{ chave: "", rotulo: "Todas", contagem: 0 }], "lista vazia");

// Conexão sem tipo não derruba a régua (o backend não deveria produzir, mas a tela não é o lugar
// de descobrir isso com um crash).
const semTipo = abasDeTipo([{ tipo: "" }]);
igual(semTipo.at(-1).rotulo, "sem tipo", "tipo vazio tem rótulo próprio");

if (falhas) {
  console.error(`\n${falhas} falha(s)`);
  process.exit(1);
}
console.log("OK — nenhuma falha");
