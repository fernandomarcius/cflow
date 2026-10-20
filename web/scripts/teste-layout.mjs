// Teste do layout automático do grafo (src/cflow/layout.ts). Roda sem navegador e sem banco:
//     node scripts/teste-layout.mjs      (ou: pnpm run test:layout)
//
// O que ele prova, e por que cada coisa está aqui:
//  1. "sem-cruzamentos" NUNCA piora o que se vê — nem cruzamento de linhas, nem linha passando por
//     baixo de um card. As duas juntas: baixar cruzamento enfiando a linha longa embaixo dos cards
//     do meio não arruma nada, e foi o primeiro resultado errado desta função;
//  2. dois cards da mesma coluna nunca ficam a menos de um passo um do outro (o refino troca alturas
//     entre um card e um corredor de aresta, que têm vãos diferentes — já encostou uma vez);
//  3. o dado nunca anda para trás: toda aresta sai de uma coluna à ESQUERDA da do destino (o modo
//     encosta o nó no consumidor, então a coluna muda entre os modos — o que não pode mudar é o
//     sentido da leitura);
//  4. grafo vazio, sem arestas, com ciclo ou com aresta órfã não quebram nem somem com nó.
import { autoLayout, medirLayout } from "../src/cflow/layout.ts";

// PRNG determinístico — teste com Math.random falha em dia de azar e passa no dia seguinte.
let semente = 12345;
const rnd = () => ((semente = (semente * 1664525 + 1013904223) >>> 0) / 4294967296);

function grafoAleatorio(nNos, nArestas) {
  const nos = Array.from({ length: nNos }, (_, i) => ({ id: `n${i}` }));
  const arestas = [];
  const vistas = new Set();
  let tentativas = 0;
  while (arestas.length < nArestas && tentativas < nArestas * 40) {
    tentativas++;
    const a = Math.floor(rnd() * nNos);
    const b = Math.floor(rnd() * nNos);
    if (a >= b) continue; // aresta sempre para a frente: garante grafo acíclico
    const chave = `${a}>${b}`;
    if (vistas.has(chave)) continue;
    vistas.add(chave);
    arestas.push({ de: `n${a}`, para: `n${b}` });
  }
  return { nos, arestas };
}

// Menor distância vertical entre dois nós da MESMA coluna.
function vaoMinimo(nos, pos) {
  const porColuna = {};
  nos.forEach((n) => ((porColuna[pos[n.id].x] ||= []).push(pos[n.id].y)));
  let pior = Infinity;
  for (const ys of Object.values(porColuna)) {
    ys.sort((a, b) => a - b);
    for (let i = 1; i < ys.length; i++) pior = Math.min(pior, ys[i] - ys[i - 1]);
  }
  return pior;
}

let falhas = 0;
const falhou = (msg) => {
  console.log(`  FALHA: ${msg}`);
  falhas++;
};

let somaCamadas = 0;
let somaSem = 0;
let sobCamadas = 0;
let sobSem = 0;
let piorTempo = 0;
console.log("nós arestas | camadas (cruz/sob) | sem-cruz (cruz/sob) |  vão | ms");
for (const [nNos, nArestas] of [
  [6, 7],
  [8, 12],
  [10, 14],
  [12, 20],
  [15, 22],
  [20, 34],
  [30, 50],
  [40, 70],
]) {
  for (let rodada = 0; rodada < 4; rodada++) {
    const { nos, arestas } = grafoAleatorio(nNos, nArestas);
    const posCamadas = autoLayout(nos, arestas, "camadas");
    const t0 = process.hrtime.bigint();
    const posSem = autoLayout(nos, arestas, "sem-cruzamentos");
    const ms = Number(process.hrtime.bigint() - t0) / 1e6;
    piorTempo = Math.max(piorTempo, ms);

    const mCamadas = medirLayout(nos, arestas, posCamadas);
    const mSem = medirLayout(nos, arestas, posSem);
    const vao = vaoMinimo(nos, posSem);
    somaCamadas += mCamadas.cruzamentos;
    somaSem += mSem.cruzamentos;
    sobCamadas += mCamadas.sobCard;
    sobSem += mSem.sobCard;
    // Passo vertical: acima de 8 nós o editor abre os cards recolhidos e o passo encolhe.
    const passo = nNos > 8 ? 150 : 470;
    // Custo do que se vê: linha por baixo de card pesa mais que cruzamento limpo (o card e a linha
    // somem juntos). Mesmo peso usado pelo layout ao escolher o arranjo.
    const custo = (m) => m.cruzamentos + 3 * m.sobCard;

    if (rodada === 0)
      console.log(
        `${String(nNos).padStart(3)} ${String(nArestas).padStart(7)} | ` +
          `${String(mCamadas.cruzamentos).padStart(8)}/${String(mCamadas.sobCard).padEnd(9)} | ` +
          `${String(mSem.cruzamentos).padStart(9)}/${String(mSem.sobCard).padEnd(9)} | ` +
          `${String(vao).padStart(4)} | ${ms.toFixed(1).padStart(5)}`,
      );
    if (custo(mSem) > custo(mCamadas))
      falhou(
        `${nNos} nós: sem-cruzamentos piorou (${mCamadas.cruzamentos} cruz/${mCamadas.sobCard} sob → ` +
          `${mSem.cruzamentos}/${mSem.sobCard})`,
      );
    if (vao < passo - 1) falhou(`${nNos} nós: cards encostados na mesma coluna (vão ${vao} < ${passo})`);
    for (const a of arestas)
      if (posSem[a.de].x >= posSem[a.para].x)
        falhou(`${a.de} → ${a.para}: o dado andaria para trás (coluna ${posSem[a.de].x} → ${posSem[a.para].x})`);
  }
}
console.log(
  `\ncruzamentos: camadas=${somaCamadas} sem-cruzamentos=${somaSem} ` +
    `(redução ${(100 * (1 - somaSem / somaCamadas)).toFixed(1)}%)` +
    ` | linhas sob card: ${sobCamadas} → ${sobSem}` +
    ` | pior tempo ${piorTempo.toFixed(0)}ms`,
);

// --- Casos de borda ---------------------------------------------------------------------------
const bordas = [
  ["vazio", [], []],
  ["sem arestas", [{ id: "a" }, { id: "b" }], []],
  [
    "ciclo",
    [{ id: "a" }, { id: "b" }, { id: "c" }],
    [
      { de: "a", para: "b" },
      { de: "b", para: "c" },
      { de: "c", para: "a" },
    ],
  ],
  ["aresta órfã", [{ id: "a" }], [{ de: "a", para: "sumiu" }]],
];
for (const [nome, nos, arestas] of bordas) {
  const pos = autoLayout(nos, arestas, "sem-cruzamentos");
  const faltando = nos.filter((n) => !pos[n.id] || !Number.isFinite(pos[n.id].x) || !Number.isFinite(pos[n.id].y));
  if (faltando.length) falhou(`${nome}: nó sem posição válida (${faltando.map((n) => n.id).join(", ")})`);
}

// O X clássico: duas fontes ligadas em ordem trocada. Se este não desembaraça, o botão não serve.
const nosX = [{ id: "f1" }, { id: "f2" }, { id: "a" }, { id: "b" }];
const arestasX = [
  { de: "f1", para: "b" },
  { de: "f2", para: "a" },
];
if (medirLayout(nosX, arestasX, autoLayout(nosX, arestasX, "sem-cruzamentos")).cruzamentos !== 0)
  falhou("o X de duas fontes cruzadas não foi desfeito");

console.log(falhas ? `\n${falhas} FALHA(S)` : "\nOK — nenhuma falha");
process.exit(falhas ? 1 : 0);
