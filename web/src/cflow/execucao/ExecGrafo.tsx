import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactFlow, {
  Background,
  type Edge,
  type Node,
  type NodeChange,
  type NodeTypes,
  type ReactFlowInstance,
} from "reactflow";
import "reactflow/dist/style.css";

import type { ExecNo } from "../../api/client";
import NoExecucao, { type DadosNoExec } from "./NoExecucao";
import { decorridoNo, type EstadoNo } from "./formato";

// Grafo da execução: React Flow em modo LEITURA (zoom, pan, fit; nada arrastável).
//
// Por que React Flow e não o board anterior de divs absolutas: o board não tinha zoom nem pan —
// grafo grande só dava scrollbar, e era impossível ver o todo. Aqui `fitView` enquadra na abertura
// e a barra ACIMA do canvas dá zoom in/out/enquadrar (fora do canvas de propósito: como overlay
// esses controles cobriam o primeiro nó em tela estreita).
//
// Por que RE-LAYOUT em vez de usar `posicao` salva no editor: o editor espaça os nós para cards
// de ~240×600 (passo 680×720). Enquadrar esse espalhamento com nós de 200×64 devolve o mesmo
// problema (tudo minúsculo e vazio). O layout aqui é denso e determinístico: coluna por
// profundidade no DAG, linha por ordem dentro da coluna.

const NODE_TYPES: NodeTypes = { exec: NoExecucao };
// 180 do nó + 30 de respiro. Medida escolhida para o caso comum (6 colunas) caber INTEIRO e com
// as métricas legíveis num laptop de 1180px: com 240 o fit caía no piso de zoom e o nó perdia
// linhas/duração exatamente na largura de tela mais frequente.
const PASSO_X = 210;
const PASSO_Y = 96;
// Abaixo deste zoom o nó mostra só nome + cor de status (ver .exec-canvas[data-zoom="baixo"]):
// é o limiar em que o texto de 9,5px deixa de ser legível.
const ZOOM_DETALHE = 0.6;
// Piso do enquadramento automático = o MESMO limiar. Num grafo longo é melhor abrir legível e
// recortado (pan e lista cobrem o resto) do que caber inteiro e ilegível — mas um piso ACIMA
// do limiar recortaria de graça grafos que ainda cabem com o detalhe completo.
const FIT_MIN_ZOOM = ZOOM_DETALHE;
const OPCOES_FIT = { padding: 0.06, maxZoom: 1, minZoom: FIT_MIN_ZOOM };

export interface NoDoGrafo {
  id: string;
  tipo: string;
  nome: string;
  estado: EstadoNo;
  linhas?: number | null;
  duracao_ms?: number | null;
  reaproveitado?: boolean | null;
  snapshot?: string | null;
  erro?: string | null;
  // SQL da versão EXECUTADA (nó `sql`, ou o gerado/guardado na config): sem isto, diagnosticar
  // uma falha obrigava a abrir o pipeline no Studio para ver o que rodou.
  sql?: string | null;
  iniciado_em?: string | null; // quando o nó começou — dá o "há quanto tempo" do que roda agora
  executou: boolean; // veio de `execucao.nos` → tem snapshot/log para mostrar
  orfao: boolean; // rodou mas não está na definição desta versão
  de: string[]; // predecessores (para o layout por profundidade)
  // Opcionais, para o MAPA DA CORRIDA (nó = pipeline). Ausentes, nada muda no grafo de nós.
  metrica?: string | null; // texto no lugar das linhas (ver `DadosNoExec.metrica`)
  deTipo?: Record<string, string>; // tipo da aresta por predecessor; `publicado` sai tracejada
}

export interface DefinicaoGrafo {
  nos: any[];
  arestas: any[];
}

/** Cruza a definição da versão executada com a trilha da execução.
 *
 * A API só devolve os nós que EXECUTARAM; os demais existem apenas na definição. E, quando a
 * definição disponível não é a da versão executada (fallback), pode haver o inverso: nó que
 * rodou e não está mais no grafo — esse entra como `orfao`, para o dado nunca ficar inacessível.
 *
 * `execucaoRodando` muda a leitura do que não rodou: com a execução em andamento é "pendente"
 * (ainda vai rodar), não "não executado" (o pipeline parou antes).
 */
// Estado de um nó a partir do registro. `executando` enquanto a execução corre é o nó rodando
// AGORA (o worker grava a marca antes de começar); numa execução já encerrada, o mesmo registro
// significa que o nó começou (podendo ter disparado um efeito externo) e nunca reportou desfecho.
function estadoDoNo(ex: { status?: string; reaproveitado?: boolean | null }, rodando: boolean): EstadoNo {
  if (ex.status === "falha") return "falha";
  if (ex.status === "executando") return rodando ? "executando" : "indefinido";
  if (ex.reaproveitado) return "reap";
  if (ex.status === "concluida") return "ok";
  return "indefinido";
}

export function cruzarNos(
  definicao: DefinicaoGrafo | null,
  nosExecucao: ExecNo[],
  execucaoRodando: boolean,
): NoDoGrafo[] {
  const execPorId = new Map(nosExecucao.map((n) => [n.no_id, n]));
  const naoRodou: EstadoNo = execucaoRodando ? "pendente" : "skip";
  const lista: NoDoGrafo[] = (definicao?.nos ?? []).map((dn: any) => {
    const ex = execPorId.get(dn.id);
    let estado: EstadoNo = naoRodou;
    // Três estados vindos do registro, não dois. `executando` num nó de execução ENCERRADA é
    // desfecho desconhecido (efeito externo disparado sem confirmação) — não é sucesso.
    if (ex) estado = estadoDoNo(ex, execucaoRodando);
    return {
      id: dn.id,
      tipo: dn.tipo ?? "nó",
      nome: dn.config?.rotulo ?? dn.rotulo ?? dn.id,
      estado,
      linhas: ex?.linhas,
      duracao_ms: ex?.duracao_ms,
      reaproveitado: ex?.reaproveitado,
      snapshot: ex?.snapshot,
      erro: ex?.erro,
      iniciado_em: ex?.iniciado_em,
      sql: dn.sql ?? dn.config?.sql ?? null,
      executou: !!ex,
      orfao: false,
      de: [],
    };
  });
  const ids = new Set(lista.map((n) => n.id));
  nosExecucao
    .filter((n) => !ids.has(n.no_id))
    .forEach((n) =>
      lista.push({
        id: n.no_id,
        tipo: "nó",
        nome: n.no_id,
        estado: estadoDoNo(n, execucaoRodando),
        linhas: n.linhas,
        duracao_ms: n.duracao_ms,
        reaproveitado: n.reaproveitado,
        snapshot: n.snapshot,
        erro: n.erro,
        iniciado_em: n.iniciado_em,
        executou: true,
        orfao: true,
        de: [],
      }),
    );
  const porId = new Map(lista.map((n) => [n.id, n]));
  (definicao?.arestas ?? []).forEach((a: any) => {
    const destino = porId.get(a.para);
    if (destino && porId.has(a.de)) destino.de.push(a.de);
  });
  return lista;
}

/** Profundidade no DAG (0 = sem predecessor) → coluna do layout. Tolera ciclo (corta em 0). */
function profundidades(nos: NoDoGrafo[]): Record<string, number> {
  const porId = new Map(nos.map((n) => [n.id, n]));
  const prof: Record<string, number> = {};
  const calc = (id: string, visto: Set<string>): number => {
    if (prof[id] != null) return prof[id];
    if (visto.has(id)) return 0;
    visto.add(id);
    const n = porId.get(id);
    const d = n && n.de.length ? Math.max(...n.de.map((p) => calc(p, visto) + 1)) : 0;
    prof[id] = d;
    return d;
  };
  nos.forEach((n) => calc(n.id, new Set()));
  return prof;
}

/** Nós na ordem em que o grafo os enfileira (profundidade no DAG). A visão em lista segue a
 *  mesma ordem do desenho — a leitura de cima pra baixo bate com a esquerda pra direita. */
export function ordemDeLeitura(nos: NoDoGrafo[]): NoDoGrafo[] {
  const prof = profundidades(nos);
  return [...nos].sort((a, b) => (prof[a.id] ?? 0) - (prof[b.id] ?? 0));
}

// Faixa de família do mapa da corrida: fundo com rótulo, ATRÁS dos nós. Não é nó de verdade — não
// seleciona, não recebe foco e não intercepta clique (`.react-flow__node-faixa` no CSS).
interface DadosFaixa {
  rotulo: string;
  largura: number;
  altura: number;
}
function FaixaNo({ data }: { data: DadosFaixa }) {
  return (
    <div className="exec-faixa" style={{ width: data.largura, height: data.altura }} aria-hidden="true">
      <span className="exec-faixa-rotulo">{data.rotulo}</span>
    </div>
  );
}
// Objeto estável fora do componente (o React Flow avisa e remonta os nós se ele mudar a cada render).
const NODE_TYPES_COM_FAIXA: NodeTypes = { exec: NoExecucao, faixa: FaixaNo };

export interface FaixaGrafo {
  id: string;
  rotulo: string;
  x: number;
  y: number;
  largura: number;
  altura: number;
}

export default function ExecGrafo({
  nos,
  selecionado,
  onSelecionar,
  focoPedido = 0,
  idGrafo = "exec-grafo",
  instrucaoTeclado = "Use Tab para percorrer os nós e Enter para abrir o dado do nó.",
  posicoes,
  faixas,
  realce,
}: {
  nos: NoDoGrafo[];
  selecionado: string | null;
  onSelecionar: (id: string) => void;
  // Contador incrementado a cada ESCOLHA explícita de nó (clique, teclado, "Ver o nó"). O grafo
  // só recentraliza quando isso muda: na abertura, quem manda é o enquadramento do fit.
  focoPedido?: number;
  // Id do React Flow e do `data-testid`. Dois grafos com o mesmo id colidiriam no texto de
  // acessibilidade que o React Flow injeta por id; o default é o do detalhe de execução.
  idGrafo?: string;
  // O que Enter faz no nó focado — no mapa da corrida o nó é um PIPELINE, não um nó com dado.
  instrucaoTeclado?: string;
  // Opcionais, do mapa da corrida. Sem eles o grafo é exatamente o de sempre: coluna por
  // profundidade, sem fundo e sem destaque.
  posicoes?: Record<string, { x: number; y: number }>; // posição calculada fora vence o layout
  faixas?: FaixaGrafo[]; // fundos rotulados atrás dos nós (agrupamento por família)
  realce?: { nos: Set<string>; arestas: Set<string> } | null; // caminho aceso; o resto esmaece
}) {
  const rf = useRef<ReactFlowInstance | null>(null);
  const envolvente = useRef<HTMLDivElement | null>(null);
  const [zoom, setZoom] = useState(1);

  // `fitView` programático NÃO emite evento de zoom (o d3 aplica o transform direto), então sem ler
  // o zoom de volta o estado ficava no valor inicial e o modo reduzido do nó era decidido por
  // acaso — o mesmo grafo aparecia completo numa largura e só com o nome noutra.
  const aplicarFit = useCallback((opcoes: Parameters<ReactFlowInstance["fitView"]>[0] = OPCOES_FIT) => {
    const inst = rf.current;
    if (!inst) return;
    inst.fitView(opcoes);
    const z = inst.getZoom();
    // Quando o zoom foi CLAMPADO pelo piso, o fitView centraliza as bounds e o grafo passa a
    // abrir cortado nas DUAS pontas. Um pipeline se lê da fonte para a saída, então reancora na
    // borda esquerda; o botão "Enquadrar tudo" (sem piso) continua mostrando o todo.
    if (z <= FIT_MIN_ZOOM + 1e-3) {
      const { y } = inst.getViewport();
      // Com posição calculada fora (mapa da corrida), reancora também no TOPO: é lá que ficam os
      // rótulos das faixas, e o grafo alto abria cortado justamente neles.
      inst.setViewport({ x: 16 - 24 * z, y: posicoes ? 16 - 12 * z : y, zoom: z });
    }
    setZoom(z);
  }, [posicoes]);

  // Medida (largura × altura) de cada nó, como o React Flow a mediu. Sem isto o nó SOME em
  // silêncio: `nodes` é derivado (recalculado quando `nos` ou a seleção mudam) e passado sem
  // `onNodesChange`, e o React Flow v11 descarta `width`/`height` a cada troca do array
  // (`createNodeInternals` não os preserva) — o nó volta a `visibility: hidden` até o
  // ResizeObserver medir de novo, e essa nova medida disputa com o próximo `setNodes`. Na máquina
  // o ciclo fecha em milissegundos; no runner do CI a definição chegava DEPOIS da primeira medida,
  // o array trocava e os dois nós ficavam invisíveis por 30 s (medido no trace: `visibility:
  // visible` antes da troca, `hidden` para sempre depois). Reinjetar a medida no nó derivado
  // mantém `initialized` verdadeiro do primeiro ao último render — sem piscar e sem corrida.
  const medidas = useRef(new Map<string, { width: number; height: number }>());
  const onNodesChange = useCallback((mudancas: NodeChange[]) => {
    for (const m of mudancas) {
      if (m.type === "dimensions" && m.dimensions) medidas.current.set(m.id, m.dimensions);
    }
    // `select` e o resto: ignorados de propósito — a seleção é dirigida por `selecionado`
    // (ver `onSelectionChange` abaixo), não pelo estado interno do React Flow.
  }, []);

  // Nós posicionados + quantas LINHAS o layout ocupou (define a altura inicial do canvas: um
  // grafo de 2 linhas não deve abrir com metade da área vazia).
  const { nodes, linhas, colunas } = useMemo(() => {
    const prof = profundidades(nos);
    const ocupacao: Record<number, number> = {};
    const postos = nos.map((n) => {
      const d = prof[n.id] ?? 0;
      const linha = ocupacao[d] ?? 0;
      ocupacao[d] = linha + 1;
      return {
        id: n.id,
        type: "exec",
        // Posição calculada fora (mapa da corrida) vence; sem ela, coluna por profundidade.
        position: posicoes?.[n.id] ?? { x: 24 + d * PASSO_X, y: 24 + linha * PASSO_Y },
        ...medidas.current.get(n.id),
        data: {
          apagado: realce ? !realce.nos.has(n.id) : false,
          nome: n.nome,
          tipo: n.tipo,
          estado: n.estado,
          linhas: n.linhas,
          duracao_ms: n.duracao_ms,
          reaproveitado: n.reaproveitado,
          // Tempo do nó em andamento, medido aqui e não por um cronômetro próprio: `nos` é
          // refeito a cada poll do detalhe (2s), então o número anda sozinho — e um relógio de
          // 1s no card obrigaria a re-renderizar o grafo inteiro a cada segundo.
          decorrido_ms: decorridoNo(n),
          orfao: n.orfao,
          metrica: n.metrica,
          selecionado: n.id === selecionado,
        },
      };
    });
    // Fundos das faixas (mapa da corrida), atrás dos nós: não selecionam nem recebem foco.
    const fundos = (faixas ?? []).map((f) => ({
      id: `faixa:${f.id}`,
      type: "faixa",
      position: { x: f.x, y: f.y },
      ...medidas.current.get(`faixa:${f.id}`), // mesma razão dos nós: refeita a cada render
      data: { rotulo: f.rotulo, largura: f.largura, altura: f.altura },
      selectable: false,
      draggable: false,
      focusable: false,
      zIndex: -1,
    }));
    const usadas = Object.values(ocupacao);
    let linhasOcupadas = usadas.length ? Math.max(...usadas) : 1;
    let colunasOcupadas = usadas.length;
    if (posicoes && postos.length) {
      // Com posição dada, a conta sai da caixa que os nós ocupam (a da faixa inclusive).
      const xs = postos.map((p) => p.position.x);
      const ys = postos.map((p) => p.position.y);
      linhasOcupadas = Math.round((Math.max(...ys) - Math.min(0, ...ys)) / PASSO_Y) + 1;
      colunasOcupadas = Math.round((Math.max(...xs) - Math.min(...xs)) / PASSO_X) + 1;
    }
    return {
      nodes: [...fundos, ...postos] as Node<DadosNoExec | DadosFaixa>[],
      linhas: linhasOcupadas,
      colunas: colunasOcupadas,
    };
  }, [nos, selecionado, posicoes, faixas, realce]);

  // O grafo transborda o canvas no zoom atual? É o que faz a barra avisar "arraste para navegar".
  // A largura vem de um ResizeObserver: lida durante o render ficava velha, e o aviso não voltava
  // ao redimensionar a janela.
  const [larguraCanvas, setLarguraCanvas] = useState(0);
  useEffect(() => {
    const el = envolvente.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setLarguraCanvas(e.contentRect.width));
    ro.observe(el);
    setLarguraCanvas(el.clientWidth);
    return () => ro.disconnect();
  }, []);
  const larguraGrafo = 24 + Math.max(1, colunas) * PASSO_X;
  const recortado = larguraCanvas > 0 && larguraGrafo * zoom > larguraCanvas + 8;

  // Altura = o que o grafo ocupa NO ZOOM QUE A LARGURA PERMITE, com teto de 42vh.
  //
  // Nem pela altura (dimensionar por ela fazia o fit cair no piso — profecia autorrealizável, o nó
  // perdia linhas/duração), nem por zoom 1 (deixava ~36% do canvas morto e empurrava a tabela do
  // dado para fora da dobra, contra o próprio motivo desta tela existir).
  const zoomDaLargura = Math.min(
    1,
    Math.max(FIT_MIN_ZOOM, (larguraCanvas || 900) / (larguraGrafo * 1.06)),
  );
  const alturaCanvas = `clamp(260px, ${Math.round(24 + linhas * PASSO_Y * zoomDaLargura) + 72}px, 42vh)`;

  const edges: Edge[] = useMemo(() => {
    const estadoPorId = new Map(nos.map((n) => [n.id, n.estado]));
    const out: Edge[] = [];
    nos.forEach((n) =>
      n.de.forEach((origem) => {
        const alcancado = estadoPorId.get(n.id);
        const id = `${origem}->${n.id}`;
        const noCaminho = realce ? realce.arestas.has(id) : false;
        out.push({
          id,
          source: origem,
          target: n.id,
          // Aresta do caminho destacado por cima das outras: senão ela some sob as esmaecidas.
          zIndex: noCaminho ? 1 : 0,
          className:
            [
              alcancado === "skip" || alcancado === "pendente" ? "exec-aresta-fria" : null,
              n.deTipo?.[origem] === "publicado" ? "exec-aresta-publicado" : null,
              realce ? (noCaminho ? "exec-aresta-caminho" : "exec-aresta-apagada") : null,
            ]
              .filter(Boolean)
              .join(" ") || undefined,
        });
      }),
    );
    return out;
  }, [nos, realce]);

  // A instrução de teclado que o React Flow injeta para leitor de tela vem em inglês e promete uma
  // tecla Delete que aqui não existe (a tela é read-only). Reescreve em pt-BR com a verdade.
  useEffect(() => {
    const d = document.getElementById(`react-flow__node-desc-${idGrafo}`);
    if (d) d.textContent = instrucaoTeclado;
  }, [idGrafo, instrucaoTeclado]);

  // Reenquadra quando o CONJUNTO de nós muda (abrir outra execução, ou o polling revelar nós
  // novos). Não a cada atualização: durante o poll o usuário pode ter dado zoom/pan, e refazer
  // fitView roubaria a navegação dele no meio do acompanhamento.
  const assinatura = nodes.map((n) => n.id).join("|");
  useEffect(() => {
    if (!assinatura) return;
    const t = window.setTimeout(() => aplicarFit(), 120);
    return () => window.clearTimeout(t);
  }, [assinatura, aplicarFit]);

  // Num grafo longo o enquadramento inicial recorta as pontas; se o nó selecionado (a auto-seleção
  // escolhe o que falhou ou o final do fluxo) cair fora, o painel mostra dado de um nó invisível.
  // Traz o nó para a vista — sem mexer no zoom, para não desorientar quem já navegou.
  useEffect(() => {
    // `focoPedido === 0` = abertura da tela: respeita o fitView. Sem esta guarda, a auto-seleção
    // (que escolhe o nó FINAL do fluxo) rolava todo pipeline largo para a cauda 260ms depois de
    // enquadrar, e a tela abria mostrando o fim em vez do fluxo.
    if (!selecionado || !focoPedido) return;
    const t = window.setTimeout(() => {
      const inst = rf.current;
      const no = inst?.getNode(selecionado);
      const caixa = envolvente.current?.getBoundingClientRect();
      if (!inst || !no || !caixa) return;
      const { x, y, zoom: z } = inst.getViewport();
      const largura = no.width ?? 180;
      const altura = no.height ?? 64;
      // Só mexe o canvas se o nó estiver FORA da área visível: recentralizar um nó que já se vê
      // desloca o grafo debaixo do cursor (e transforma um clique em pan).
      const esq = no.position.x * z + x;
      const topo = no.position.y * z + y;
      const dentro =
        esq >= 0 && topo >= 0 && esq + largura * z <= caixa.width && topo + altura * z <= caixa.height;
      if (dentro) return;
      const suave = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      // Empurra o MÍNIMO para o nó entrar, em vez de centralizar: centralizar joga o começo do
      // fluxo fora da tela quando só a cauda estava faltando.
      const foraDir = Math.max(0, esq + largura * z - caixa.width + 16);
      const foraEsq = Math.max(0, 16 - esq);
      const foraBaixo = Math.max(0, topo + altura * z - caixa.height + 16);
      const foraTopo = Math.max(0, 16 - topo);
      inst.setViewport(
        { x: x - foraDir + foraEsq, y: y - foraBaixo + foraTopo, zoom: z },
        { duration: suave ? 240 : 0 },
      );
    }, 260);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focoPedido]);

  return (
    <>
      <div className="exec-canvas-barra">
        <span className="exec-canvas-dica">
          {recortado ? "arraste para navegar · " : ""}
          {Math.round(zoom * 100)}%
        </span>
        <button type="button" className="pag-btn" aria-label="Afastar" title="Afastar"
          onClick={() => { rf.current?.zoomOut(); setZoom(rf.current?.getZoom() ?? 1); }}>−</button>
        <button type="button" className="pag-btn" aria-label="Aproximar" title="Aproximar"
          onClick={() => { rf.current?.zoomIn(); setZoom(rf.current?.getZoom() ?? 1); }}>+</button>
        <button type="button" className="pag-btn" aria-label="Enquadrar tudo" title="Enquadrar tudo"
          onClick={() => aplicarFit({ padding: 0.1, maxZoom: 1 })}>Enquadrar</button>
      </div>
    <div
      ref={envolvente}
      className="exec-canvas"
      data-testid={idGrafo}
      style={{ height: alturaCanvas }}
      // Em zoom baixo o CSS troca o conteúdo do nó por nome grande + cor (o resto viraria borrão).
      data-zoom={zoom <= ZOOM_DETALHE + 0.001 ? "baixo" : "alto"}
      // Enter/Espaço no nó focado seleciona, sem depender de o React Flow emitir seleção por
      // teclado — ele emite às vezes, e "às vezes" no único caminho de teclado significa que o
      // dado do nó não é alcançável sem mouse.
      onKeyDown={(ev) => {
        if (ev.key !== "Enter" && ev.key !== " ") return;
        const alvo = (ev.target as HTMLElement | null)?.closest?.(".react-flow__node");
        const id = alvo?.getAttribute("data-id");
        if (!id) return;
        ev.preventDefault();
        onSelecionar(id);
      }}
    >
      <ReactFlow
        id={idGrafo}
        nodes={nodes}
        edges={edges}
        nodeTypes={faixas?.length ? NODE_TYPES_COM_FAIXA : NODE_TYPES}
        onInit={(inst) => {
          rf.current = inst;
          // rAF: o fit inicial do próprio React Flow roda depois do onInit; ler o zoom antes daria
          // o valor errado.
          requestAnimationFrame(() => setZoom(inst.getZoom()));
        }}
        onMove={(_, viewport) => setZoom(viewport.zoom)}
        // DOIS caminhos de propósito: `onNodeClick` cobre o mouse; `onSelectionChange` cobre o
        // teclado (Enter/Espaço mudam a seleção mas não disparam clique). Só a seleção não basta —
        // com `nodes` controlado sem a flag `selected`, o React Flow zera a seleção no render
        // seguinte e o evento de seleção deixa de ser confiável como fonte única.
        onNodeClick={(_, n) => onSelecionar(n.id)}
        onNodesChange={onNodesChange}
        onSelectionChange={({ nodes: sel }) => {
          if (sel[0]) onSelecionar(sel[0].id);
        }}
        nodesDraggable={false}
        nodesConnectable={false}
        edgesFocusable={false}
        elementsSelectable
        // Sem isso, Backspace com um nó selecionado tenta REMOVER o nó — numa tela de leitura.
        deleteKeyCode={null}
        minZoom={0.1}
        maxZoom={1.75}
        fitView
        // `minZoom` no FIT (não no canvas): num grafo longo, enquadrar tudo a qualquer custo
        // encolhia os nós até o texto ficar ilegível. Abre num zoom que se lê e o resto se
        // alcança por pan — o botão "enquadrar" dos Controls continua mostrando o todo.
        fitViewOptions={OPCOES_FIT}
        proOptions={{ hideAttribution: false }}
      >
        <Background />
        {/* Sem <Controls> flutuante: o stack no canto inferior esquerdo cobria o primeiro nó em
            telas estreitas (e a área coberta deixava de ser clicável). Os controles vivem na barra
            acima do canvas, com rótulo em pt-BR. */}
        {/* Sem MiniMap: era o último overlay DENTRO do canvas e, num fan-out, deitava sobre os nós
            da última coluna (rótulo ilegível). Orientação de "há grafo fora da vista" fica na barra
            de cima, junto do zoom em % — texto não colide com nó. */}
      </ReactFlow>
    </div>
    </>
  );
}
