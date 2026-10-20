// Layout automático do grafo do Studio (fonte → saída). Dois modos, um mesmo cálculo de camadas:
//
//  - "camadas"         — dentro da camada, a ordem é a de chegada dos nós. É o layout histórico:
//                        previsível e estável (quem criou o nó sabe onde ele vai cair), mas as
//                        arestas se cruzam livremente.
//  - "sem-cruzamentos" — reordena cada camada para minimizar o cruzamento das linhas (Sugiyama:
//                        nós virtuais nas arestas longas + heurística da mediana + transposição por
//                        pares vizinhos), e depois alinha cada nó com os vizinhos para endireitar
//                        as cadeias.
//
// Nenhum dos dois é "o certo": o primeiro preserva a ordem em que a pessoa montou o grafo, o segundo
// troca essa previsibilidade por linhas legíveis. A escolha é de quem edita — por isso são dois
// botões, e não um heurístico decidindo sozinho.

export interface ArestaLayout {
  de: string;
  para: string;
}
export interface PosLayout {
  x: number;
  y: number;
}
export type ModoLayout = "camadas" | "sem-cruzamentos";

// Geometria. Os passos são dimensionados pelo CARD (300px de largura, 392 nos nós de efeito; altura
// típica ~400px com o editor aberto) mais um respiro — antes eram 680×720, mais que o dobro do
// necessário: um pipeline de 15 nós ocupava vários milhares de pixels e o enquadramento abria num
// zoom em que nenhum rótulo se lia.
const PASSO_X = 460;
const PASSO_Y = 470;
// Grafo grande abre com os cards RECOLHIDOS (~90px de altura em vez de ~400): o passo tem de
// acompanhar, senão sobra um vão de 380px entre cards de 90 e o grafo continua sem caber.
const PASSO_X_COMPACTO = 430; // > 392px do card de efeito (http/escrita_sql)
const PASSO_Y_COMPACTO = 150;
// A partir daqui o canvas não comporta todos os editores abertos (ver `compacto` no contexto).
export const LIMIAR_COMPACTO = 8;

// O passo vertical que `autoLayout` usou para N nós. Quem reescala as posições para outra geometria
// (o mapa da corrida, com nós de ~64px) precisa dele, e repetir o número lá deixaria as duas cópias
// divergirem no dia em que este mudar.
export function passoVertical(totalNos: number): number {
  return totalNos > LIMIAR_COMPACTO ? PASSO_Y_COMPACTO : PASSO_Y;
}

// Tamanho do card usado na geometria (a largura real varia: 300, 392 nos nós de efeito). Serve para
// dois testes: onde a linha SAI/ENTRA (alça na lateral, meia altura) e que retângulo ela não pode
// atravessar.
const LARGURA_CARD = 320;
const ALTURA_CARD_COMPACTO = 130;
const ALTURA_CARD_ABERTO = 420;
// Folga entre o card e o corredor vizinho. O corredor não é um card — pede só a passagem —, mas
// precisa de espaço REAL: com uma fatia proporcional ao passo, o corredor caía dentro da altura do
// card ao lado e a linha longa passava por baixo dele.
const FOLGA_CORREDOR = 40;
// Dois corredores lado a lado só precisam não se confundir.
const FATIA_ENTRE_CORREDORES = 0.35;
// Linha correndo por baixo de um card esconde as duas coisas (a linha e o card) — pesa mais que um
// cruzamento limpo, que pelo menos deixa ver as duas.
const PESO_SOB_CARD = 3;
// Passadas da heurística da mediana (alternando descida/subida) e rodadas do alinhamento vertical.
const PASSADAS_MEDIANA = 12;
const PASSADAS_TRANSPOR = 4;
const RODADAS_ALINHAR = 4;
const PASSADAS_REFINO = 6;
// Teto do refino geométrico (custo ~ arestas²): acima disto o clique demoraria mais do que o ganho
// justifica, e a ordenação por camadas já entrega a maior parte do resultado.
const MAX_ARESTAS_REFINO = 240;

// Profundidade = caminho mais longo desde uma fonte. Ciclo (grafo inválido, mas o rascunho permite)
// não trava: o nó já em visita conta como 0.
function profundidades(nos: { id: string }[], arestas: ArestaLayout[]): Record<string, number> {
  const preds: Record<string, string[]> = {};
  nos.forEach((n) => (preds[n.id] = []));
  arestas.forEach((a) => preds[a.para]?.push(a.de));
  const prof: Record<string, number> = {};
  const calc = (id: string, vendo: Set<string>): number => {
    if (prof[id] != null) return prof[id];
    if (vendo.has(id)) return 0;
    vendo.add(id);
    const p = preds[id] || [];
    prof[id] = p.length ? 1 + Math.max(...p.map((x) => calc(x, vendo))) : 0;
    return prof[id];
  };
  nos.forEach((n) => calc(n.id, new Set()));
  return prof;
}

// Empurra cada nó para a coluna MAIS TARDE possível: uma antes do seu primeiro consumidor. A
// profundidade por caminho mais longo prende toda fonte na coluna 0 — e uma fonte lida só lá na
// frente (o `op_ref` do Check de Cancelamento é consumido cinco colunas depois) vira uma linha
// atravessando o grafo inteiro por baixo dos cards do meio. Encostada no consumidor, a mesma
// ligação é um traço curto. Nó sem consumidor (a saída) não se move, e ninguém passa à frente de
// quem o alimenta: percorrendo do fundo para a frente, o consumidor já tem a coluna definitiva.
function compactarCamadas(
  nos: { id: string }[],
  arestas: ArestaLayout[],
  prof: Record<string, number>,
): Record<string, number> {
  const seguintes: Record<string, string[]> = {};
  nos.forEach((n) => (seguintes[n.id] = []));
  arestas.forEach((a) => {
    if (prof[a.de] != null && prof[a.para] != null && prof[a.para] > prof[a.de])
      seguintes[a.de].push(a.para);
  });
  const novo = { ...prof };
  const daFrenteParaTras = nos.map((n) => n.id).sort((a, b) => (prof[b] || 0) - (prof[a] || 0));
  for (const id of daFrenteParaTras) {
    const s = seguintes[id] || [];
    if (!s.length) continue; // sem consumidor: fica onde está
    const alvo = Math.min(...s.map((x) => novo[x])) - 1;
    if (alvo > (novo[id] || 0)) novo[id] = alvo;
  }
  return novo;
}

function mediana(vals: number[]): number {
  if (!vals.length) return -1;
  const v = [...vals].sort((a, b) => a - b);
  const m = v.length >> 1;
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
}

// Cruzamentos entre duas camadas vizinhas: par de arestas cruza quando a ordem das pontas se
// inverte de um lado para o outro. Contagem direta por pares — os grafos aqui têm dezenas de
// arestas, não milhares.
function contarCruzamentosArcos(arcos: [string, string][], ix: Record<string, number>): number {
  let c = 0;
  for (let i = 0; i < arcos.length; i++) {
    const [u1, v1] = arcos[i];
    for (let j = i + 1; j < arcos.length; j++) {
      const [u2, v2] = arcos[j];
      if ((ix[u1] - ix[u2]) * (ix[v1] - ix[v2]) < 0) c++;
    }
  }
  return c;
}

// Cruzamento VISÍVEL: interseção própria dos dois segmentos que ligam os cards. É esta a métrica que
// interessa — a ordenação por camadas conta cruzamento por índice, e uma aresta que pula camadas é
// desenhada em linha reta de ponta a ponta, ignorando o corredor reservado no meio do caminho.
// Duas arestas que compartilham ponta não contam (elas se encontram no nó, não se cruzam).
function segmentosCruzam(p1: PosLayout, p2: PosLayout, p3: PosLayout, p4: PosLayout): boolean {
  const orient = (a: PosLayout, b: PosLayout, c: PosLayout) =>
    (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x);
  const d1 = orient(p3, p4, p1);
  const d2 = orient(p3, p4, p2);
  const d3 = orient(p1, p2, p3);
  const d4 = orient(p1, p2, p4);
  return ((d1 > 0 && d2 < 0) || (d1 < 0 && d2 > 0)) && ((d3 > 0 && d4 < 0) || (d3 < 0 && d4 > 0));
}

// O segmento atravessa o retângulo do card? (Não basta olhar as pontas: a linha longa entra por um
// lado e sai pelo outro sem que nenhuma ponta esteja dentro.)
function segmentoCruzaCaixa(p: PosLayout, q: PosLayout, r: PosLayout, w: number, h: number): boolean {
  const x1 = r.x;
  const y1 = r.y;
  const x2 = r.x + w;
  const y2 = r.y + h;
  if (Math.max(p.x, q.x) < x1 || Math.min(p.x, q.x) > x2) return false;
  if (Math.max(p.y, q.y) < y1 || Math.min(p.y, q.y) > y2) return false;
  const cantos: PosLayout[] = [
    { x: x1, y: y1 },
    { x: x2, y: y1 },
    { x: x2, y: y2 },
    { x: x1, y: y2 },
  ];
  for (let i = 0; i < 4; i++) if (segmentosCruzam(p, q, cantos[i], cantos[(i + 1) % 4])) return true;
  return false;
}

// A linha nasce na alça da DIREITA do card de origem e morre na alça da ESQUERDA do destino, na
// meia altura — medir de canto superior esquerdo a canto superior esquerdo deslocava a geometria
// inteira e escondia justamente as passagens rentes ao card.
const saida = (p: PosLayout, altura: number): PosLayout => ({ x: p.x + LARGURA_CARD, y: p.y + altura / 2 });
const entrada = (p: PosLayout, altura: number): PosLayout => ({ x: p.x, y: p.y + altura / 2 });

// Quantas arestas se cruzam e quantas passam por baixo de um card. As duas coisas juntas são o que
// se vê: baixar cruzamento empurrando linha para debaixo dos cards não arruma nada.
export function medirLayout(
  nos: { id: string }[],
  arestas: ArestaLayout[],
  pos: Record<string, PosLayout>,
): { cruzamentos: number; sobCard: number } {
  const altura = nos.length > LIMIAR_COMPACTO ? ALTURA_CARD_COMPACTO : ALTURA_CARD_ABERTO;
  const seg = arestas.filter((a) => pos[a.de] && pos[a.para]);
  let cruzamentos = 0;
  for (let i = 0; i < seg.length; i++) {
    for (let j = i + 1; j < seg.length; j++) {
      const a = seg[i];
      const b = seg[j];
      if (a.de === b.de || a.para === b.para || a.de === b.para || a.para === b.de) continue;
      if (
        segmentosCruzam(
          saida(pos[a.de], altura),
          entrada(pos[a.para], altura),
          saida(pos[b.de], altura),
          entrada(pos[b.para], altura),
        )
      )
        cruzamentos++;
    }
  }
  let sobCard = 0;
  for (const a of seg) {
    const p = saida(pos[a.de], altura);
    const q = entrada(pos[a.para], altura);
    for (const n of nos) {
      if (n.id === a.de || n.id === a.para || !pos[n.id]) continue;
      if (segmentoCruzaCaixa(p, q, pos[n.id], LARGURA_CARD, altura)) sobCard++;
    }
  }
  return { cruzamentos, sobCard };
}

// Métrica única de comparação entre arranjos.
const custoVisual = (m: { cruzamentos: number; sobCard: number }) => m.cruzamentos + PESO_SOB_CARD * m.sobCard;

// Só os cruzamentos (usado pelo teste e por diagnóstico).
export function contarCruzamentos(
  nos: { id: string }[],
  arestas: ArestaLayout[],
  pos: Record<string, PosLayout>,
): number {
  return medirLayout(nos, arestas, pos).cruzamentos;
}

function layoutCamadas(
  nos: { id: string }[],
  prof: Record<string, number>,
  passoX: number,
  passoY: number,
): Record<string, PosLayout> {
  const naCamada: Record<number, number> = {};
  const pos: Record<string, PosLayout> = {};
  nos.forEach((n) => {
    const d = prof[n.id] || 0;
    const linha = naCamada[d] || 0;
    pos[n.id] = { x: 40 + d * passoX, y: 20 + linha * passoY };
    naCamada[d] = linha + 1;
  });
  return pos;
}

function layoutSemCruzamentos(
  nos: { id: string }[],
  arestas: ArestaLayout[],
  profOriginal: Record<string, number>,
  passoX: number,
  passoY: number,
  camadasFixas = false,
): Record<string, PosLayout> {
  // Grafo grande abre com os cards recolhidos: a altura usada na geometria acompanha.
  const alturaCard = nos.length > LIMIAR_COMPACTO ? ALTURA_CARD_COMPACTO : ALTURA_CARD_ABERTO;
  // Camadas FIXAS (quem chama já decidiu a coluna de cada nó — o mapa da corrida agrupa por
  // família): só a ordem dentro da coluna é otimizada; nada é empurrado de coluna.
  const prof = camadasFixas ? profOriginal : compactarCamadas(nos, arestas, profOriginal);
  const nivelMax = nos.reduce((m, n) => Math.max(m, prof[n.id] || 0), 0);
  const camadas: string[][] = Array.from({ length: nivelMax + 1 }, () => []);
  nos.forEach((n) => camadas[prof[n.id] || 0].push(n.id));

  // Aresta que pula camadas vira uma corrente de nós VIRTUAIS, um por camada atravessada. Sem isso
  // a contagem de cruzamentos ignora justamente as linhas longas — as que mais atravessam o grafo.
  const virtual = new Set<string>();
  const arcos: [string, string][][] = camadas.map(() => []);
  let seq = 0;
  for (const a of arestas) {
    const d0 = prof[a.de];
    const d1 = prof[a.para];
    if (d0 == null || d1 == null || d1 <= d0) continue; // aresta que não avança (ciclo) fica de fora
    let ant = a.de;
    for (let d = d0 + 1; d < d1; d++) {
      const v = ` virtual-${seq++}`;
      virtual.add(v);
      camadas[d].push(v);
      arcos[d - 1].push([ant, v]);
      ant = v;
    }
    arcos[d1 - 1].push([ant, a.para]);
  }

  const anteriores: Record<string, string[]> = {};
  const seguintes: Record<string, string[]> = {};
  arcos.forEach((camada) =>
    camada.forEach(([u, v]) => {
      (seguintes[u] ||= []).push(v);
      (anteriores[v] ||= []).push(u);
    }),
  );

  // Duas sementes de ordenação: a ordem de CHEGADA (a mesma do modo "camadas") e a ordenada pela
  // heurística da mediana. As duas passam pelo mesmo refino geométrico e vence a de menor
  // cruzamento visível — assim este modo nunca devolve algo pior que o layout de sempre, que é o
  // risco de otimizar só o índice de camada (a aresta longa é desenhada reta, ponta a ponta,
  // ignorando o corredor que os nós virtuais reservaram no meio).
  const ordemChegada = camadas.map((c) => c.slice());
  let ordem = camadas.map((c) => c.slice());
  const ix: Record<string, number> = {};
  const reindexar = () => ordem.forEach((c) => c.forEach((id, i) => (ix[id] = i)));
  reindexar();
  const totalCruzamentos = () => arcos.reduce((s, arc) => s + contarCruzamentosArcos(arc, ix), 0);

  // Trocar dois vizinhos só mexe nos arcos que tocam esses dois nós. Contar todos os pares da camada
  // a cada tentativa é o que fazia a reorganização de um grafo grande travar por segundos.
  const arcosDe: Record<string, { camada: number; idx: number }[]> = {};
  arcos.forEach((arc, camada) =>
    arc.forEach(([u, v], idx) => {
      (arcosDe[u] ||= []).push({ camada, idx });
      (arcosDe[v] ||= []).push({ camada, idx });
    }),
  );
  const custoLocalArcos = (ids: string[]) => {
    const porCamada = new Map<number, Set<number>>();
    ids.forEach((id) =>
      (arcosDe[id] || []).forEach(({ camada, idx }) => {
        let s = porCamada.get(camada);
        if (!s) porCamada.set(camada, (s = new Set()));
        s.add(idx);
      }),
    );
    let c = 0;
    porCamada.forEach((marc, camada) => {
      const arc = arcos[camada];
      for (const i of marc) {
        const [u1, v1] = arc[i];
        for (let j = 0; j < arc.length; j++) {
          if (j === i || (marc.has(j) && j > i)) continue; // par marcado-marcado conta uma vez só
          const [u2, v2] = arc[j];
          if ((ix[u1] - ix[u2]) * (ix[v1] - ix[v2]) < 0) c++;
        }
      }
    });
    return c;
  };

  let melhor = ordem.map((c) => c.slice());
  let melhorCusto = totalCruzamentos();

  for (let p = 0; p < PASSADAS_MEDIANA && melhorCusto > 0; p++) {
    // Mediana: cada nó tende à posição mediana dos vizinhos do lado que está fixo na passada.
    const descendo = p % 2 === 0;
    const seqCamadas = descendo
      ? Array.from({ length: ordem.length - 1 }, (_, k) => k + 1)
      : Array.from({ length: ordem.length - 1 }, (_, k) => ordem.length - 2 - k);
    for (const i of seqCamadas) {
      const chave: Record<string, number> = {};
      ordem[i].forEach((id, k) => {
        const viz = (descendo ? anteriores[id] : seguintes[id]) || [];
        const m = mediana(viz.map((v) => ix[v]).filter((v) => v != null));
        chave[id] = m >= 0 ? m : k; // sem vizinho do lado fixo: fica onde está
      });
      ordem[i] = ordem[i].slice().sort((a, b) => chave[a] - chave[b]); // sort estável (ES2019+)
      reindexar();
    }
    // Transposição: troca vizinhos adjacentes enquanto isso baixar o cruzamento. Pega o que a
    // mediana não vê (ela move em bloco, não desempata par a par).
    for (let volta = 0; volta < PASSADAS_TRANSPOR; volta++) {
      let mudou = false;
      for (let i = 0; i < ordem.length; i++) {
        for (let k = 0; k + 1 < ordem[i].length; k++) {
          const a = ordem[i][k];
          const b = ordem[i][k + 1];
          const antes = custoLocalArcos([a, b]);
          ordem[i][k] = b;
          ordem[i][k + 1] = a;
          ix[b] = k;
          ix[a] = k + 1;
          if (custoLocalArcos([a, b]) < antes) mudou = true;
          else {
            ordem[i][k] = a;
            ordem[i][k + 1] = b;
            ix[a] = k;
            ix[b] = k + 1;
          }
        }
      }
      if (!mudou) break;
    }
    const custo = totalCruzamentos();
    if (custo < melhorCusto) {
      melhorCusto = custo;
      melhor = ordem.map((c) => c.slice());
    }
  }
  const ordemMediana = melhor;

  // --- Coordenada vertical + refino sobre o cruzamento VISÍVEL ---------------------------------
  const xDe: Record<string, number> = {};
  nos.forEach((n) => (xDe[n.id] = 40 + (prof[n.id] || 0) * passoX));

  const montar = (y: Record<string, number>) => {
    const alturasReais = nos.map((n) => y[n.id] ?? 0);
    const minY = alturasReais.length ? Math.min(...alturasReais) : 0;
    const pos: Record<string, PosLayout> = {};
    nos.forEach((n) => (pos[n.id] = { x: xDe[n.id], y: Math.round(20 + (y[n.id] ?? 0) - minY) }));
    return pos;
  };

  let posFinal = layoutCamadas(nos, prof, passoX, passoY); // piso: o layout de sempre
  let custoFinal = custoVisual(medirLayout(nos, arestas, posFinal));
  const considerar = (pos: Record<string, PosLayout>) => {
    const custo = custoVisual(medirLayout(nos, arestas, pos));
    if (custo < custoFinal) {
      custoFinal = custo;
      posFinal = pos;
    }
  };

  for (const candidata of [ordemMediana, ordemChegada]) {
    const ord = candidata.map((c) => c.slice());
    // Refina e RE-ESPAÇA: o refino troca as alturas de dois vizinhos, e o vão de um card é maior
    // que o de um corredor de aresta — sem recalcular, um nó que trocou de lugar com um corredor
    // herdaria o vão estreito e encostaria no vizinho. A última palavra é sempre do espaçamento,
    // e cada estado espaçado é medido: o re-espaço pode desfazer parte do que o refino ganhou.
    let y = alturas(ord, virtual, anteriores, seguintes, passoY, alturaCard);
    considerar(montar(y));
    for (let passo = 0; passo < 2; passo++) {
      refinarGeometrico(ord, y, xDe, arestas, nos, alturaCard);
      y = alturas(ord, virtual, anteriores, seguintes, passoY, alturaCard);
      considerar(montar(y));
    }
  }
  return posFinal;
}

// Altura de cada nó da camada: fatia cheia para card, fatia estreita para corredor de aresta (nó
// virtual). Depois puxa cada nó para a mediana da altura dos vizinhos — é o que endireita uma cadeia
// fonte → sql → saída em vez de deixá-la em escada — sem quebrar a ordem nem o vão mínimo.
function alturas(
  ordem: string[][],
  virtual: Set<string>,
  anteriores: Record<string, string[]>,
  seguintes: Record<string, string[]>,
  passoY: number,
  alturaCard: number,
): Record<string, number> {
  // Vão entre dois vizinhos da mesma camada. Card↔corredor não é proporcional ao passo: é a altura
  // do card mais uma folga, senão o corredor nasce dentro do card ao lado e a linha longa passa
  // por baixo dele — que é exatamente o defeito que o corredor existe para evitar.
  const vao = (a: string, b: string) => {
    const va = virtual.has(a);
    const vb = virtual.has(b);
    if (va && vb) return passoY * FATIA_ENTRE_CORREDORES;
    if (va || vb) return alturaCard + FOLGA_CORREDOR;
    return passoY;
  };
  const y: Record<string, number> = {};
  ordem.forEach((c) => {
    let acc = 0;
    c.forEach((id, k) => {
      if (k) acc += vao(c[k - 1], id);
      y[id] = acc;
    });
  });

  for (let r = 0; r < RODADAS_ALINHAR; r++) {
    const descendo = r % 2 === 0;
    const seqCamadas = descendo
      ? Array.from({ length: ordem.length - 1 }, (_, k) => k + 1)
      : Array.from({ length: ordem.length - 1 }, (_, k) => ordem.length - 2 - k);
    for (const i of seqCamadas) {
      const c = ordem[i];
      if (!c.length) continue;
      const desejado = c.map((id) => {
        const viz = (descendo ? anteriores[id] : seguintes[id]) || [];
        const alt = viz.map((v) => y[v]).filter((v) => v != null);
        return alt.length ? mediana(alt) : y[id];
      });
      const novo: number[] = [];
      c.forEach((id, k) => {
        const piso = k ? novo[k - 1] + vao(c[k - 1], id) : -Infinity;
        novo[k] = Math.max(desejado[k], piso);
      });
      // Todo mundo ficou EM ou ABAIXO do desejado (o piso só empurra para baixo). Subir a camada
      // inteira pela folga comum aproxima todos do alvo sem mexer na ordem nem no vão.
      let folga = Infinity;
      novo.forEach((v, k) => (folga = Math.min(folga, v - desejado[k])));
      if (folga > 0 && folga < Infinity) novo.forEach((_, k) => (novo[k] -= folga));
      c.forEach((id, k) => (y[id] = novo[k]));
    }
  }
  return y;
}

// Último passo, e o único que enxerga o que a pessoa vê: troca vizinhos de altura dentro da camada
// enquanto isso reduzir o custo VISÍVEL — linhas que se cruzam MAIS linhas que passam por baixo de
// um card. As duas juntas, sempre: contando só cruzamento, o refino "arrumava" o desenho enfiando a
// linha longa embaixo dos cards do meio, que é pior do que o cruzamento que ele removeu.
// A troca só mexe nas arestas que tocam os dois nós (e nos cards que são os dois nós) — por isso a
// conta é local, e não uma varredura de tudo a cada tentativa.
function refinarGeometrico(
  ordem: string[][],
  y: Record<string, number>,
  x: Record<string, number>,
  arestas: ArestaLayout[],
  nos: { id: string }[],
  alturaCard: number,
) {
  const seg = arestas.filter((a) => x[a.de] != null && x[a.para] != null);
  const total = seg.length;
  // Acima disto o refino custaria mais do que devolve: é um botão, tem de responder no clique.
  if (!total || total > MAX_ARESTAS_REFINO) return;
  const inc: Record<string, number[]> = {};
  seg.forEach((a, i) => {
    (inc[a.de] ||= []).push(i);
    (inc[a.para] ||= []).push(i);
  });
  const canto = (id: string): PosLayout => ({ x: x[id], y: y[id] });
  const de = (i: number) => saida(canto(seg[i].de), alturaCard);
  const para = (i: number) => entrada(canto(seg[i].para), alturaCard);
  const cruzam = (i: number, j: number) => {
    const a = seg[i];
    const b = seg[j];
    if (a.de === b.de || a.para === b.para || a.de === b.para || a.para === b.de) return false;
    return segmentosCruzam(de(i), para(i), de(j), para(j));
  };
  const passaPorBaixo = (i: number, id: string) =>
    id !== seg[i].de &&
    id !== seg[i].para &&
    x[id] != null &&
    segmentoCruzaCaixa(de(i), para(i), canto(id), LARGURA_CARD, alturaCard);

  const marcado = new Uint8Array(total);
  const custoLocal = (idxs: number[], movidos: string[]) => {
    idxs.forEach((i) => (marcado[i] = 1));
    let c = 0;
    for (const i of idxs)
      for (let j = 0; j < total; j++) {
        if (j === i || (marcado[j] && j > i)) continue; // par marcado-marcado conta uma vez só
        if (cruzam(i, j)) c++;
      }
    // Linha sob card muda em dois casos: a aresta se moveu (ponta em um dos dois nós) ou o card se
    // moveu. Os dois entram na conta, sem repetir o par (aresta movida × card movido).
    let sob = 0;
    for (const i of idxs) for (const n of nos) if (passaPorBaixo(i, n.id)) sob++;
    for (const id of movidos)
      for (let i = 0; i < total; i++) if (!marcado[i] && passaPorBaixo(i, id)) sob++;
    idxs.forEach((i) => (marcado[i] = 0));
    return c + PESO_SOB_CARD * sob;
  };

  for (let volta = 0; volta < PASSADAS_REFINO; volta++) {
    let mudou = false;
    for (const camada of ordem) {
      for (let k = 0; k + 1 < camada.length; k++) {
        const a = camada[k];
        const b = camada[k + 1];
        const idxs = [...new Set([...(inc[a] || []), ...(inc[b] || [])])];
        const movidos = [a, b].filter((id) => x[id] != null);
        if (!idxs.length && !movidos.length) continue;
        const antes = custoLocal(idxs, movidos);
        const ya = y[a];
        const yb = y[b];
        camada[k] = b;
        camada[k + 1] = a;
        y[a] = yb;
        y[b] = ya;
        if (custoLocal(idxs, movidos) < antes) mudou = true;
        else {
          camada[k] = a;
          camada[k + 1] = b;
          y[a] = ya;
          y[b] = yb;
        }
      }
    }
    if (!mudou) break;
  }
}

// Layout automático em camadas (fonte → saída). Usado tanto no botão "Reorganizar" quanto para os
// nós sem `posicao` salva (ex.: pipeline criado via API) — sem isto todos caíam em (60,0) e ficavam
// empilhados, e só um nó aparecia.
//
// `camadas` (opcional) fixa a COLUNA de cada nó e deixa o algoritmo decidir só a ordem dentro dela.
// Sem ele, nada muda: é o que o Studio usa. O mapa da corrida passa as colunas agrupadas por família.
export function autoLayout(
  nos: { id: string }[],
  arestas: ArestaLayout[],
  modo: ModoLayout = "camadas",
  camadas?: Record<string, number>,
): Record<string, PosLayout> {
  if (!nos.length) return {};
  const compacto = nos.length > LIMIAR_COMPACTO;
  const passoX = compacto ? PASSO_X_COMPACTO : PASSO_X;
  const passoY = compacto ? PASSO_Y_COMPACTO : PASSO_Y;
  const prof = camadas ?? profundidades(nos, arestas);
  return modo === "sem-cruzamentos"
    ? layoutSemCruzamentos(nos, arestas, prof, passoX, passoY, camadas != null)
    : layoutCamadas(nos, prof, passoX, passoY);
}
