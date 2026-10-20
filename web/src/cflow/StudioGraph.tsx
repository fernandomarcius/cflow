import { autocompletion, type CompletionContext } from "@codemirror/autocomplete";
import { sql } from "@codemirror/lang-sql";
import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
  type Dispatch,
  type SetStateAction,
} from "react";
import { useDispatch, useSelector } from "react-redux";
import ReactFlow, {
  addEdge,
  Background,
  Controls,
  type Connection,
  type DefaultEdgeOptions,
  type Node,
  type NodeTypes,
  type ReactFlowInstance,
  useEdgesState,
  useNodesState,
} from "reactflow";
import "reactflow/dist/style.css";

import * as api from "../api/client";
import { lerDefinicao, montarDefinicao, type Objeto } from "./definicao";
import { autoLayout, LIMIAR_COMPACTO, type ModoLayout } from "./layout";
import { bibsDetectadas, parametrosAutomaticos } from "./nos/deteccao";
import { validacaoSql } from "./nos/lintsql";
import NoPipeline from "./nos/NoPipeline";
import { realceParametros } from "./nos/realce";
import { Icon } from "./ui";
import { useTemaAtual } from "./tema";
import { EditorNoContext } from "./nos/contexto";
import { setBaseNumero, setErro, setPipeline, type RootState } from "../store/store";

let seq = 0;
const novoId = () => `n${++seq}`;

// Todas as categorias usam o MESMO componente custom (switch interno por data). A chave casa com
// node.type; guiada/filtrar/agrupar compartilham "guiada" (distinção fina por config.op.tipo).
// Constante de módulo (estável) → não dispara o warning de "nodeTypes recreated" do React Flow.
const nodeTypes: NodeTypes = {
  fonte: NoPipeline,
  sql: NoPipeline,
  guiada: NoPipeline,
  python: NoPipeline,
  comparacao: NoPipeline,
  saida: NoPipeline,
  escrita_sql: NoPipeline,
  http: NoPipeline,
  email: NoPipeline,
  ingestao: NoPipeline,
};
// Aresta bezier (default = bezier no v11); cor fica no CSS p/ herdar claro/escuro.
const defaultEdgeOptions: DefaultEdgeOptions = { type: "default", style: { strokeWidth: 2 } };

// Chave da paleta = node.type ("guiada" cobre juntar/filtrar/agrupar). Um alias só: a lista estava
// escrita à mão em quatro lugares e cada tipo novo de nó exigia lembrar de todos os quatro.
type ChavePaleta = keyof typeof nodeTypes;
type TipoNo = Exclude<ChavePaleta, "guiada">;

// Itens da paleta lateral (arrastáveis E clicáveis). chave = node.type; op = sub-tipo de guiada.
const PALETA: { testid: string; chave: ChavePaleta; op?: "juntar" | "filtrar" | "agrupar"; rotulo: string; ic: Parameters<typeof Icon>[0]["name"] }[] = [
  { testid: "add-fonte", chave: "fonte", rotulo: "Fonte", ic: "portal" },
  // `ingestao` vem logo depois de Fonte porque é o outro jeito de o dado ENTRAR no grafo — e o
  // único que o traz de fora da casa. Fica no topo, não no fim junto dos efeitos, porque quem
  // monta um pipeline bronze começa por ele.
  { testid: "add-ingestao", chave: "ingestao", rotulo: "Ingestão", ic: "download" },
  { testid: "add-sql", chave: "sql", rotulo: "SQL", ic: "studio" },
  { testid: "add-juntar", chave: "guiada", op: "juntar", rotulo: "Juntar", ic: "modelo" },
  { testid: "add-filtrar", chave: "guiada", op: "filtrar", rotulo: "Filtrar", ic: "search" },
  { testid: "add-agrupar", chave: "guiada", op: "agrupar", rotulo: "Agrupar", ic: "grid" },
  { testid: "add-comparacao", chave: "comparacao", rotulo: "Comparação", ic: "layout" },
  { testid: "add-python", chave: "python", rotulo: "Python", ic: "runs" },
  { testid: "add-escrita-sql", chave: "escrita_sql", rotulo: "Escrita SQL", ic: "download" },
  { testid: "add-http", chave: "http", rotulo: "HTTP", ic: "arrow" },
  { testid: "add-email", chave: "email", rotulo: "E-mail", ic: "carta" },
  { testid: "add-saida", chave: "saida", rotulo: "Saída", ic: "download" },
];
const DND_TIPO = "application/cflow-no"; // mime do dataTransfer no drag da paleta

// Layout automático (camadas fonte → saída) mora em `./layout` — dois modos, ver o cabeçalho de lá.
// Piso do zoom no enquadramento = o mesmo limiar em que o card deixa de ser legível. Abaixo disso
// não vale a pena "caber": é melhor abrir legível e rolar (mesma regra que a tela de execução já
// cumpre).
const ZOOM_LEGIVEL = 0.45;
const OPCOES_FIT = { padding: 0.12, maxZoom: 1, minZoom: ZOOM_LEGIVEL };

// Tipos de nó em que `amostra` tem efeito no preview: a FONTE lê N linhas do banco e o nó SQL
// aplica LIMIT N (ver `preview.py`). Os demais herdam o tamanho de quem vem antes — mexer na
// amostra deles não mudaria nada, e oferecer isso seria um controle que não faz o que diz.
const TIPOS_COM_AMOSTRA = new Set(["fonte", "sql"]);
const AMOSTRA_PADRAO = 10;

// API imperativa exposta ao detalhe (Pipeline.tsx): publicar usa a definição ATUAL do canvas.
export interface StudioGraphHandle {
  // Persiste o rascunho atual (canvas em memória) → devolve o nº do rascunho.
  salvar: () => Promise<number>;
  // Persiste o rascunho atual e publica (a versão publicada reflete o canvas). `nota` é a nota de
  // mudança da versão — o que essa publicação alterou.
  publicar: (nota?: string) => Promise<number>;
  // Carrega uma VERSÃO específica no canvas (undefined = a última) — p/ ver/rodar/publicar anterior.
  abrirVersao: (numero?: number) => void;
  // Avisa que houve edição FORA do canvas (aba de Parâmetros): uma carga em voo sobrescrevia o
  // parâmetro recém-criado com a lista do servidor, e o trabalho sumia sozinho.
  registrarEdicaoExterna: () => void;
  // Amostra da prévia: quantos nós a usam e quais valores estão em uso (para o detalhe montar o
  // diálogo sem espelhar o estado do canvas).
  resumoAmostra: () => { nos: number; valores: number[] };
  // Aplica a amostra em TODOS os nós que a usam de uma vez → devolve quantos mudaram.
  aplicarAmostra: (valor: number) => number;
}

interface StudioGraphProps {
  // Sinaliza ao detalhe que o pipeline foi alterado (dirty) — dirige o botão Executar/Publicar.
  onSujo?: () => void;
  // Reporta contagens (nós/fontes) ao cabeçalho do detalhe sempre que o grafo muda.
  onMeta?: (m: { nos: number; fontes: number }) => void;
  // Erros da última execução, por nó (no_id → mensagem) — pinta o card que falhou.
  errosExec?: Record<string, string>;
  // Notifica que `salvar()` autodeclarou `:ref` sem parâmetro correspondente — o detalhe (Pipeline.tsx)
  // usa isto p/ manter em dia a SUA própria cópia de `parametros` (aba Parâmetros) e mostrar o aviso.
  onParametrosCriados?: (nomes: string[], todos: api.ParametroPipeline[]) => void;
  // Dispara em TODO save bem-sucedido (toolbar ou "Salvar" do code area) — o detalhe usa isto p/
  // saber quando refazer o fetch dos nós que alimenta o lint de parâmetros (aba Parâmetros).
  onSalvo?: () => void;
  // Parâmetros do pipeline: estado ELEVADO ao detalhe (Pipeline.tsx). Canvas e aba Parâmetros
  // editam a MESMA lista — com duas cópias, salvar o grafo regravava a cópia carregada na abertura
  // e apagava o que a aba tinha acabado de salvar.
  parametros: api.ParametroPipeline[];
  onParametros: Dispatch<SetStateAction<api.ParametroPipeline[]>>;
}

// Editor de grafo do Studio (React Flow). Comportamento + data-testids preservados do App original.
// Auto-carrega o pipeline aberto (Redux `studio.pipelineId`); sem seletor/criação internos.
export const StudioGraph = forwardRef<StudioGraphHandle, StudioGraphProps>(function StudioGraph(
  { onSujo, onMeta, errosExec, onParametrosCriados, onSalvo, parametros, onParametros },
  ref,
) {
  const d = useDispatch();
  const tema = useTemaAtual(); // tema do editor SQL (CodeMirror) acompanha claro/escuro
  const { pipelineId, baseNumero } = useSelector((s: RootState) => s.studio);
  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const [selId, setSelId] = useState<string | null>(null);
  const [conexoes, setConexoes] = useState<api.Conexao[]>([]);
  const [pipelines, setPipelines] = useState<api.PipelineResumo[]>([]); // fontes tipo pipeline
  const [fontesLake, setFontesLake] = useState<api.FonteLake[]>([]); // datasets do bronze
  const [bibsDisponiveis, setBibsDisponiveis] = useState<api.PipelineResumo[]>([]);
  const [macroParaBib, setMacroParaBib] = useState<Record<string, string>>({});
  // A paleta FLUTUA sobre o canvas: aberta por padrão, ela cobria a primeira coluna de nós de um
  // pipeline já montado. A escolha da pessoa persiste; sem escolha, ela abre só no canvas vazio
  // (onde a paleta é o próximo passo óbvio) e fica recolhida em pipeline que já tem nó.
  const [naoCoube, setNaoCoube] = useState(false);
  const [paletaAberta, setPaletaAberta] = useState(
    () => localStorage.getItem("cflow-paleta") !== "0",
  );
  const paletaEscolhida = useRef(localStorage.getItem("cflow-paleta") !== null);
  const alternarPaleta = useCallback((aberta: boolean) => {
    paletaEscolhida.current = true;
    localStorage.setItem("cflow-paleta", aberta ? "1" : "0");
    setPaletaAberta(aberta);
  }, []);
  const [versao, setVersao] = useState(0);
  // Preview POR NÓ (mostrado dentro do card, não numa seção genérica). Keyed por id do nó.
  const [previews, setPreviews] = useState<Record<string, api.PreviewResultado>>({});
  const [errosPreview, setErrosPreview] = useState<Record<string, string>>({});
  const [carregandoPreview, setCarregandoPreview] = useState<Record<string, boolean>>({});
  const [previewSelo, setPreviewSelo] = useState<Record<string, { assinatura: string; em: number }>>({});
  const [buscaPaleta, setBuscaPaleta] = useState(""); // filtro da barra lateral de componentes
  const [excluirId, setExcluirId] = useState<string | null>(null); // nó pendente de confirmação de exclusão
  // Parâmetros de runtime do pipeline — carregados na abertura e PRESERVADOS no preview/salvar
  // (sem eles o preview de nós-fonte com `:param` falha; salvar sem eles apagaria os params).
  // O estado vive no detalhe (props `parametros`/`onParametros`), compartilhado com a aba Parâmetros.
  const setParametros = onParametros;
  // Evita disparar onSujo durante a própria carga (abrir) — só edições do usuário marcam dirty.
  const carregando = useRef(false);
  // O que o topo da definição carregada tem além de nós/arestas/parâmetros (ex.: `montante_publica`).
  // Volta intacto em todo salvar — ver `definicao.ts`. Recarregado a cada `abrir` (cada versão tem o
  // seu). Estado, e não ref, porque o controle do `montante_publica` no canvas o exibe.
  const [extrasTopo, setExtrasTopo] = useState<Objeto>({});
  // Geração de edição do usuário (NÃO gated por `carregando`): o auto-load captura este valor antes
  // do fetch e descarta o resultado se o usuário editou durante a carga — senão o setNodes do load
  // apagaria um nó recém-adicionado (corrida ao abrir um pipeline novo).
  const edicaoSeq = useRef(0);
  // Instância do React Flow (capturada via onInit) — usada p/ screenToFlowPosition no drop da paleta.
  const rfRef = useRef<ReactFlowInstance | null>(null);

  useEffect(() => {
    api.listarConexoes().then(setConexoes);
    api.listarPipelines().then(setPipelines);
    api.listarFontesLake().then(setFontesLake);
    // Bibliotecas disponíveis + mapa nome-de-macro → id da biblioteca (auto-ativação por uso real).
    api
      .listarFuncoes()
      .then(async (libs) => {
        setBibsDisponiveis(libs);
        const mapa: Record<string, string> = {};
        await Promise.all(
          libs.map(async (b) => {
            try {
              const det = await api.obterFuncao(b.id);
              for (const f of det?.definicao?.funcoes ?? []) {
                const nm = String(f?.nome ?? "").toLowerCase();
                if (nm) mapa[nm] = b.id;
              }
            } catch {
              /* biblioteca sem versão publicada/acessível → ignora */
            }
          }),
        );
        setMacroParaBib(mapa);
      })
      .catch(() => {
        setBibsDisponiveis([]);
        setMacroParaBib({});
      });
  }, []);

  // Marca o pipeline como alterado (dirty) e propaga ao detalhe.
  //
  // O guard de "carregando" vale só para as mudanças que o PRÓPRIO React Flow emite ao montar o
  // grafo (`onNodesChangeSujo`/`onEdgesChangeSujo`). Aplicá-lo também às ações do usuário abria uma
  // janela silenciosa: adicionar um nó enquanto o pipeline ainda carregava deixava o rascunho
  // "limpo", e como `executar()` só persiste o canvas quando está sujo, o Executar seguinte rodava
  // a versão ANTIGA — sem o nó que estava na tela.
  const marcarSujo = useCallback(() => {
    onSujo?.();
  }, [onSujo]);

  const marcarSujoSeNaoCarregando = useCallback(() => {
    if (carregando.current) return;
    onSujo?.();
  }, [onSujo]);

  const onConnect = useCallback(
    (c: Connection) => {
      marcarSujo();
      setEdges((e) => addEdge(c, e));
    },
    [marcarSujo, setEdges],
  );

  // Exclui uma aresta (corda) — duplo-clique nela ou Delete com ela selecionada. Sem isto não dava
  // p/ desfazer uma conexão feita à mão.
  const excluirAresta = useCallback(
    (idAresta: string) => {
      marcarSujo();
      setEdges((es) => es.filter((e) => e.id !== idAresta));
    },
    [marcarSujo, setEdges],
  );

  // Wrappers de onNodesChange/onEdgesChange. Só 'remove' marca dirty aqui: toda edição do usuário
  // (adicionar nó, ligar corda, editar SQL/config, excluir) já chama `marcarSujo` no seu próprio
  // handler. Os outros tipos ('add', 'replace', 'reset', 'dimensions') são emitidos pelo PRÓPRIO
  // React Flow ao montar o grafo — e chegavam DEPOIS do `setTimeout(0)` que liberava o flag de
  // carga, deixando um pipeline recém-salvo "sujo" outra vez sem ninguém ter tocado nele.
  // 'position' também não conta: mover card é layout, persistido in-place sem criar versão.
  const onNodesChangeSujo = useCallback(
    (changes: any[]) => {
      if (changes.some((c) => c.type === "remove")) marcarSujoSeNaoCarregando();
      onNodesChange(changes);
    },
    [marcarSujoSeNaoCarregando, onNodesChange],
  );
  const onEdgesChangeSujo = useCallback(
    (changes: any[]) => {
      if (changes.some((c) => c.type === "remove")) marcarSujoSeNaoCarregando();
      onEdgesChange(changes);
    },
    [marcarSujoSeNaoCarregando, onEdgesChange],
  );

  // autocomplete: colunas/tabelas das etapas anteriores (preenchido ao selecionar um nó SQL)
  const compRef = useRef<{ tabela: string; colunas: string[] }[]>([]);
  const fonteAutocomplete = (ctx: CompletionContext) => {
    const palavra = ctx.matchBefore(/\w*/);
    if (!palavra || (palavra.from === palavra.to && !ctx.explicit)) return null;
    const opcoes: { label: string; type: string }[] = [];
    for (const { tabela, colunas } of compRef.current) {
      opcoes.push({ label: tabela, type: "class" });
      for (const c of colunas) opcoes.push({ label: c, type: "property" });
    }
    return opcoes.length ? { from: palavra.from, options: opcoes } : null;
  };
  const extensoesSql = [sql(), realceParametros, validacaoSql, autocompletion({ override: [fonteAutocomplete] })];

  const atualizarConfig = (id: string, patch: Record<string, unknown>) => {
    marcarSujo();
    setNodes((ns) => ns.map((n) => (n.id === id ? { ...n, data: { ...n.data, config: { ...n.data.config, ...patch } } } : n)));
  };

  // `amostra` NUNCA é dado confiável — nem do backend (versão antiga pode ter persistido um valor,
  // de antes desta regra existir) nem entre sessões. Sempre 10 ao abrir, sem exceção/fallback pro
  // valor salvo; o especialista aumenta na hora se precisar, só nesta sessão.
  const semAmostra = (cfg: any) => {
    if (!cfg || cfg.amostra === undefined) return cfg;
    const { amostra: _amostra, ...resto } = cfg;
    return resto;
  };

  // O que o canvas edita POR CIMA do que a definição carregada tinha: topo, nó e aresta levam de
  // volta o que nenhum formulário daqui conhece (`definicao.ts`). Esta função montava
  // `{nos, arestas, parametros}` do zero, e o salvar apagava `montante_publica` em silêncio.
  const definicao = () =>
    montarDefinicao({
      extras: extrasTopo,
      // config.bibliotecas é a seleção REAL do nó (pré-marcada por uso de MACRO, mas editável — sem
      // travar/forçar). Não recomputamos aqui p/ que desmarcar uma função persista.
      nos: nodes.map((n) => ({
        extras: n.data._extras,
        id: n.id,
        tipo: n.data.tipo,
        rotulo: n.data.rotulo,
        descricao: n.data.descricao,
        sql: n.data.sql,
        config: n.data.config,
        posicao: n.position,
      })),
      arestas: edges.map((e) => ({ extras: e.data?.extras, de: e.source, para: e.target })),
      parametros,
    });

  // Pré-seleção (não hardcoded, não travada): quando o SQL de um nó passa a CHAMAR uma MACRO de uma
  // biblioteca, marca essa biblioteca no nó automaticamente. Roda ao abrir e quando o TEXTO do SQL
  // muda — NÃO quando o usuário (des)marca um checkbox (a assinatura abaixo só olha o texto), então
  // desmarcar uma função continua valendo.
  const assinaturaSql = nodes
    .map((n) => `${n.id}:${n.data?.sql ?? ""}:${n.data?.config?.sql ?? ""}:${n.data?.config?.op?.condicao ?? ""}`)
    .join("|");
  useEffect(() => {
    if (!Object.keys(macroParaBib).length || !nodes.length) return;
    setNodes((ns) => {
      let mudou = false;
      const novo = ns.map((n) => {
        const det = bibsDetectadas(n.data, macroParaBib);
        if (!det.length) return n;
        const atuais: string[] = (n.data.config?.bibliotecas ?? []).map(String);
        const faltam = det.filter((d) => !atuais.includes(d));
        if (!faltam.length) return n;
        mudou = true;
        return { ...n, data: { ...n.data, config: { ...n.data.config, bibliotecas: [...atuais, ...faltam] } } };
      });
      return mudou ? novo : ns;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [assinaturaSql, macroParaBib]);

  // Reporta as contagens (nós/fontes) ao cabeçalho do detalhe a cada mudança do grafo.
  useEffect(() => {
    const fontes = nodes.filter((n) => n.data.tipo === "fonte").length;
    onMeta?.({ nos: nodes.length, fontes });
  }, [nodes, onMeta]);

  // busca colunas das etapas anteriores quando um nó SQL é selecionado (alimenta o autocomplete)
  useEffect(() => {
    const s = nodes.find((n) => n.id === selId);
    if (!pipelineId || !selId || s?.data.tipo !== "sql") {
      compRef.current = [];
      return;
    }
    api
      .colunasDisponiveis(pipelineId, selId, definicao())
      .then((map) => {
        compRef.current = Object.entries(map).map(([tabela, colunas]) => ({ tabela, colunas }));
      })
      .catch(() => {
        compRef.current = [];
      });
  }, [selId, pipelineId]); // eslint-disable-line react-hooks/exhaustive-deps

  // Carrega o grafo de um pipeline (auto-load do detalhe). `numero` opcional → carrega uma VERSÃO
  // específica no canvas (ver/rodar/publicar uma anterior); sem ele → a última. O lock otimista
  // (baseNumero) usa SEMPRE o nº máximo (det.ultimo) para que salvar/publicar a versão vista
  // gere uma NOVA versão (não tente sobrescrever a antiga).
  // `fitView` programático NÃO emite evento de zoom: sem ler `getZoom()` de volta, qualquer coisa
  // que dependa do zoom decide por um estado que nunca atualiza. Quando o grafo não cabe no piso
  // de legibilidade, ancora no início do fluxo em vez de centralizar o meio de um grafo cortado.
  const enquadrar = useCallback(() => {
    const inst = rfRef.current;
    if (!inst) return;
    inst.fitView(OPCOES_FIT);
    const z = inst.getZoom();
    const noPiso = z <= ZOOM_LEGIVEL + 1e-3;
    if (noPiso) {
      // Não cabe no piso: ancora o CANTO do grafo, nos DOIS eixos. Manter o `y` centralizado do
      // fitView (o que se fazia aqui) empurra o primeiro nó para fora por cima — medido: cabeçalho
      // em y=104 com o canvas começando em y=213, ou seja, o nó existe, é clipado e fica
      // inalcançável. E o fluxo é vertical: o eixo que precisa de âncora é justamente o y.
      const ns = inst.getNodes();
      const minX = ns.length ? Math.min(...ns.map((n) => n.position.x)) : 0;
      const minY = ns.length ? Math.min(...ns.map((n) => n.position.y)) : 0;
      // A paleta FLUTUA sobre o canvas: ancorar em x=24 põe o grafo inteiro debaixo dela (com a
      // paleta aberta, nenhum cabeçalho de nó recebia clique — e sobravam ~760px de canvas vazio
      // à direita). Ler a largura real evita duplicar a medida do CSS aqui.
      const paleta = document.querySelector<HTMLElement>(".sd-palette");
      const folgaEsq = 24 + (paleta ? paleta.offsetWidth + 14 : 0);
      inst.setViewport({ x: folgaEsq - minX * z, y: 24 - minY * z, zoom: z });
    }
    // Não cabe legível: em vez de abrir num zoom ilegível em silêncio, a tela DIZ isso e oferece
    // reorganizar (que é ação do usuário — reescrever a posição salva por conta própria seria
    // mexer no trabalho dele sem pedir).
    setNaoCoube(noPiso);
  }, []);

  // Abrir/recolher a paleta muda a área útil do canvas. Só reenquadra quando o grafo NÃO cabia (aí
  // o viewport já é programático); com o grafo cabendo, reenquadrar aqui roubaria o pan de quem
  // está navegando.
  const paletaInicial = useRef(true);
  useEffect(() => {
    if (paletaInicial.current) {
      paletaInicial.current = false;
      return;
    }
    if (!naoCoube) return;
    const t = setTimeout(() => enquadrar(), 60);
    return () => clearTimeout(t);
  }, [paletaAberta]); // eslint-disable-line react-hooks/exhaustive-deps

  // Layout BASE: as posições que valem e que são persistidas. O empurrão que abre espaço para um
  // card expandido é TEMPORÁRIO e não entra aqui — antes ele virava posição salva, então fechar o
  // nó deixava um buraco permanente e o grafo escorregava um pouco a cada abre-e-fecha.
  const layoutBase = useRef<Record<string, { x: number; y: number }>>({});
  const fixarBase = useCallback((ns: { id: string; position: { x: number; y: number } }[]) => {
    const m: Record<string, { x: number; y: number }> = {};
    for (const n of ns) m[n.id] = { x: n.position.x, y: n.position.y };
    layoutBase.current = m;
  }, []);

  const abrir = useCallback(
    async (id: string, numero?: number) => {
      carregando.current = true;
      const seqIni = edicaoSeq.current;
      try {
        const det = await api.obterPipeline(id, numero);
        // Usuário editou durante o fetch (ex.: adicionou um nó logo após abrir um pipeline novo)?
        // Então NÃO sobrescreve o canvas — preserva a edição em vez de apagá-la.
        if (edicaoSeq.current !== seqIni) return;
        // Campos editáveis separados do que só passa pelo canvas (`_extras` no nó, `extras` na
        // aresta, `extrasTopo` no topo) — `definicao()` os devolve no salvar.
        const lida = lerDefinicao(det.definicao);
        setExtrasTopo(lida.extras);
        const nos = lida.nos;
        const layout = autoLayout(nos, lida.arestas);
        setNodes(
          nos.map((n) => ({
            id: n.id,
            type: n.tipo, // casa com nodeTypes (fonte/sql/guiada) → renderiza o card custom
            // posição SALVA vence (mover o card persiste); auto-layout só p/ nós sem posição (criados via API)
            position: n.posicao ?? layout[n.id] ?? { x: 60, y: 0 },
            data: {
              tipo: n.tipo,
              rotulo: n.rotulo,
              descricao: n.descricao,
              sql: n.sql,
              config: semAmostra(n.config),
              label: `${n.id} · ${n.tipo}`,
              _extras: n.extras,
            },
          })) as Node[],
        );
        fixarBase(
          nos.map((n) => ({
            id: n.id,
            position: n.posicao ?? layout[n.id] ?? { x: 60, y: 0 },
          })),
        );
        setEdges(
          lida.arestas.map((a, i) => ({ id: `e${i}`, source: a.de, target: a.para, data: { extras: a.extras } })),
        );
        setParametros(lida.parametros);
        seq = Math.max(0, ...nos.map((n) => parseInt(String(n.id).replace(/\D/g, "")) || 0));
        setSelId(null);
        if (!paletaEscolhida.current) setPaletaAberta(nos.length === 0);
        setVersao(det.numero); // rótulo da versão CARREGADA (pode ser anterior)
        d(setPipeline({ id, numero: det.ultimo ?? det.numero })); // baseNumero = máximo (lock)
        // Reenquadra após carregar (o `fitView` do React Flow só roda no 1º render; ao reabrir um
        // pipeline os nós ficariam fora da viewport / clipados).
        setTimeout(() => enquadrar(), 120);
      } finally {
        // libera o flag após o ciclo de render (os setNodes/setEdges não devem marcar dirty)
        setTimeout(() => {
          carregando.current = false;
        }, 0);
      }
    },
    [d, setNodes, setEdges, setParametros],
  );

  // AUTO-LOAD: ao montar e sempre que o pipeline aberto mudar, carrega o grafo correspondente.
  useEffect(() => {
    if (pipelineId) void abrir(pipelineId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pipelineId]);

  // Autodeclara `:ref` sem parâmetro correspondente ANTES de persistir — cobre o caso de o usuário
  // ter escrito `:novo_param` no SQL sem passar pela aba Parâmetros. Retorna também os nomes criados
  // (p/ quem chamou avisar o especialista).
  const salvar = async (): Promise<{ numero: number; criados: string[] }> => {
    const nosAtuais = nodes.map((n) => ({ sql: n.data.sql, config: n.data.config }));
    const { criados, todos } = parametrosAutomaticos(nosAtuais, parametros);
    if (criados.length) {
      setParametros(todos);
      onParametrosCriados?.(criados.map((p) => p.nome), todos);
    }
    // `amostra` é só do preview desta sessão — nunca persiste (reabrir a pipe sempre volta ao
    // padrão de 10; o especialista aumenta na hora se precisar, sem isso virar config salva).
    const def = definicao();
    const nosSemAmostra = def.nos.map((n) => ({ ...n, config: semAmostra(n.config) }));
    const r = await api.salvarRascunho(
      pipelineId!,
      { ...def, nos: nosSemAmostra, parametros: todos },
      baseNumero,
    );
    d(setBaseNumero(r.numero));
    setVersao(r.numero);
    onSalvo?.();
    return { numero: r.numero, criados: criados.map((p) => p.nome) };
  };

  // Autosave (debounced) só da POSIÇÃO (layout) ao MOVER/reorganizar nós — IN-PLACE, sem criar
  // versão nova (mover é cosmético, não muda lógica). Lê as posições atuais no disparo (nodesRef).
  // Não persiste se estiver vendo uma versão ANTERIOR (versao != baseNumero) p/ não escrever layout
  // numa versão que não é a corrente.
  const nodesRef = useRef(nodes);
  nodesRef.current = nodes;
  const layoutTimer = useRef<number | null>(null);
  const agendarLayout = useCallback(() => {
    if (!pipelineId || carregando.current || versao !== baseNumero) return;
    if (layoutTimer.current) window.clearTimeout(layoutTimer.current);
    layoutTimer.current = window.setTimeout(() => {
      const posicoes: Record<string, { x: number; y: number }> = {};
      for (const n of nodesRef.current) {
        const base = layoutBase.current[n.id];
        posicoes[n.id] = base ?? { x: n.position.x, y: n.position.y };
      }
      void api.atualizarLayout(pipelineId, posicoes).catch(() => {});
    }, 700);
  }, [pipelineId, versao, baseNumero]);

  // Expõe publicar ao detalhe: persiste o rascunho atual ANTES (para a versão publicada refletir o
  // canvas) e então publica. Reusa salvarRascunho conforme a story.
  useImperativeHandle(
    ref,
    (): StudioGraphHandle => ({
      salvar: async () => {
        if (!pipelineId) throw new Error("Abra um pipeline antes de salvar.");
        const r = await salvar();
        return r.numero;
      },
      // `nota` é a NOTA DE MUDANÇA da versão. Sem ela toda versão do sistema ficava com a mesma
      // frase ("publicado pelo Studio") — a trilha existia e não carregava informação, o que é pior
      // que não ter, porque parece registro.
      publicar: async (nota?: string) => {
        if (!pipelineId) throw new Error("Abra um pipeline antes de publicar.");
        const { numero: numRascunho } = await salvar();
        const p = await api.publicar(pipelineId, numRascunho, (nota ?? "").trim() || "publicado pelo Studio");
        d(setBaseNumero(p.numero));
        setVersao(p.numero);
        d(setErro(null));
        return p.numero;
      },
      abrirVersao: (numero?: number) => {
        if (pipelineId) void abrir(pipelineId, numero);
      },
      registrarEdicaoExterna: () => {
        edicaoSeq.current += 1;
      },
      // Amostra da prévia de TODOS os nós que a usam, de uma vez. Abrir nó por nó para trocar
      // 10 → 1000 é o trabalho que este par de funções elimina.
      resumoAmostra: () => {
        const alvos = nodes.filter((n) => TIPOS_COM_AMOSTRA.has(String((n.data as any)?.tipo)));
        return {
          nos: alvos.length,
          valores: [
            ...new Set(alvos.map((n) => Number((n.data as any)?.config?.amostra ?? AMOSTRA_PADRAO))),
          ].sort((a, b) => a - b),
        };
      },
      // NÃO marca sujo de propósito: `amostra` não entra na definição salva (ver `semAmostra`), e
      // acender "Salvar rascunho" para algo que o salvar descarta é prometer o que não acontece.
      // A prévia já exibida fica marcada como desatualizada sozinha — a assinatura do nó inclui a
      // config, então o selo do preview acusa a mudança.
      aplicarAmostra: (valor: number) => {
        const alvo = Math.max(1, Math.floor(valor));
        let alterados = 0;
        setNodes((ns) =>
          ns.map((n) => {
            if (!TIPOS_COM_AMOSTRA.has(String((n.data as any)?.tipo))) return n;
            if (Number((n.data as any)?.config?.amostra ?? AMOSTRA_PADRAO) === alvo) return n;
            alterados += 1;
            return { ...n, data: { ...n.data, config: { ...(n.data as any).config, amostra: alvo } } };
          }),
        );
        edicaoSeq.current += 1;
        return alterados;
      },
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [pipelineId, baseNumero, nodes, edges, parametros, extrasTopo, abrir, onParametrosCriados, onSalvo],
  );

  // `montante_publica`: ao disparar ESTE pipeline, os que ele lê por fonte tipo pipeline
  // rodam junto; `false` faz a montante calcular sem publicar. O controle aparece onde a marca faz
  // sentido (há montante) ou quando ela já está desligada — escondê-la com `false` gravado seria
  // esconder o que decide se a corrida move ponteiro. Marcado = chave AUSENTE (o default).
  const temMontante = nodes.some(
    (n) => n.data?.tipo === "fonte" && n.data?.config?.origem === "pipeline",
  );
  const montantePublica = extrasTopo.montante_publica !== false;
  const alternarMontante = (publica: boolean) => {
    marcarSujo();
    setExtrasTopo((x) => {
      const { montante_publica: _m, ...resto } = x;
      return publica ? resto : { ...resto, montante_publica: false };
    });
  };

  // pos opcional: clique da paleta usa o default (comportamento atual, E2E intacto); drag passa o ponto solto.
  const addNo = (tipo: TipoNo, pos?: { x: number; y: number }) => {
    marcarSujo();
    edicaoSeq.current += 1;
    const id = novoId();
    const y = nodes.length * 380; // passo > altura do card sempre-aberto p/ não empilhar ao clicar
    // config inicial por tipo (semente mínima válida; campos obrigatórios o usuário preenche)
    const configInicial: Record<string, any> = {
      fonte: { conexao_id: "", sql: "SELECT * FROM ...", amostra: 10 },
      python: { codigo: '# Globais: con (DuckDB), entradas (dict), parametros (dict).\n# Predecessores entram como VIEW pelo id do nó. Defina `resultado`.\nresultado = con.sql("SELECT * FROM entrada")', linguagem: "python" },
      comparacao: { chave: [] },
      saida: { nome: "", formato: "parquet" },
      // Nós de efeito (008): sementes mínimas; a conexão (permitir_escrita) o usuário escolhe.
      escrita_sql: { modo: "tabela", estrategia: "append" },
      http: { metodo: "POST", modo: "unico", corpo_tipo: "colunas", tolerancia_falha: "abortar" },
      // `email`: semente com um template MÍNIMO já válido (não um campo em branco). O especialista
      // troca o HTML; a tabela do nó anterior já aparece por `{{tabela}}`.
      email: {
        modo: "unico",
        corpo_tipo: "html",
        para: "",
        assunto: "",
        corpo:
          '<div style="font-family:Arial,sans-serif">\n  <p>Prezados,</p>\n  <p>Segue o resultado ({{contagem}} linha(s)), gerado em {{data_hora}}.</p>\n  {{tabela}}\n</div>',
        anexos: [],
        tolerancia_falha: "abortar",
      },
    };
    const data: any = {
      tipo,
      label: `${id} · ${tipo}`,
      rotulo: `${tipo} ${id}`,
      sql: tipo === "sql" ? "SELECT 1 AS um" : undefined,
      config: configInicial[tipo],
    };
    setNodes((ns) => [...ns, { id, type: tipo, position: pos ?? { x: 40, y }, data } as Node]);
    setSelId(id);
  };

  const atualizarSql = (id: string, valor: string) => {
    marcarSujo();
    edicaoSeq.current += 1;
    setNodes((ns) => ns.map((n) => (n.id === id ? { ...n, data: { ...n.data, sql: valor } } : n)));
  };

  const addGuiada = (opTipo: "juntar" | "filtrar" | "agrupar", pos?: { x: number; y: number }) => {
    marcarSujo();
    edicaoSeq.current += 1;
    const id = novoId();
    const y = nodes.length * 380; // passo > altura do card sempre-aberto p/ não empilhar ao clicar
    const op: any =
      opTipo === "agrupar" ? { tipo: "agrupar", chaves: [], agregacoes: ["count(*) AS qtd"] } : { tipo: opTipo, condicao: "" };
    const data: any = { tipo: "guiada", rotulo: `${opTipo} ${id}`, label: `${id} · ${opTipo}`, config: { op } };
    setNodes((ns) => [...ns, { id, type: "guiada", position: pos ?? { x: 360, y }, data } as Node]);
    setSelId(id);
  };

  // Drag-and-drop da paleta: cria o nó no ponto solto (coordenadas do flow via screenToFlowPosition).
  const onDragOverCanvas = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
  }, []);
  const onDropCanvas = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      const carga = e.dataTransfer.getData(DND_TIPO);
      if (!carga || !rfRef.current) return;
      const [chave, op] = carga.split(":");
      const pos = rfRef.current.screenToFlowPosition({ x: e.clientX, y: e.clientY });
      if (chave === "guiada") addGuiada(op as "juntar" | "filtrar" | "agrupar", pos);
      else addNo(chave as TipoNo, pos);
    },
    // addNo/addGuiada são closures estáveis o bastante p/ este uso (dependem de nodes só p/ y default, não usado no drop)
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  // Reaplica o layout automático em camadas (fonte → saída) aos nós atuais — desempilha o grafo
  // independentemente de posições salvas antigas. Reenquadra a viewport ao final.
  // `modo` escolhe entre a ordem de criação (camadas) e a ordem que evita cruzar as linhas.
  const reorganizar = (modo: ModoLayout = "camadas") => {
    if (!nodes.length) return;
    marcarSujo();
    const layout = autoLayout(
      nodes.map((n) => ({ id: n.id })),
      edges.map((e) => ({ de: e.source, para: e.target })),
      modo,
    );
    setNodes((ns) => {
      const novos = ns.map((n) => ({ ...n, position: layout[n.id] ?? n.position }));
      fixarBase(novos); // reorganizar DEFINE o layout — vira a nova base
      return novos;
    });
    setTimeout(enquadrar, 0);
    agendarLayout(); // persiste o novo layout in-place (sem versão nova)
  };

  // Reposiciona os vizinhos quando um card cresce (ex.: abrir preview): empurra p/ baixo os nós que
  // passariam a colidir, mantendo uma margem mínima. Usa os tamanhos MEDIDOS pelo React Flow.
  // NOTA: não existe mais passe de colisão por expansão. O card aberto sobrepõe (zIndex), porque
  // empurrar vizinho abria um vão na coluna, deixava buraco ao fechar e derivava o grafo a cada
  // abre-e-fecha — três sintomas do mesmo erro: tratar estado de UI como posição do dado.

  const atualizarOp = (id: string, patch: Record<string, unknown>) => {
    marcarSujo();
    setNodes((ns) =>
      ns.map((n) =>
        n.id === id ? { ...n, data: { ...n.data, config: { ...n.data.config, op: { ...n.data.config.op, ...patch } } } } : n,
      ),
    );
  };

  const atualizarRotulo = (id: string, valor: string) => {
    marcarSujo();
    setNodes((ns) => ns.map((n) => (n.id === id ? { ...n, data: { ...n.data, rotulo: valor } } : n)));
  };

  const atualizarDescricao = (id: string, valor: string) => {
    marcarSujo();
    setNodes((ns) => ns.map((n) => (n.id === id ? { ...n, data: { ...n.data, descricao: valor } } : n)));
  };

  // Edita o "valor de preview" de um parâmetro declarado (mostrado no nó que o usa).
  const setValorPreview = (nome: string, valor: string) => {
    marcarSujo();
    setParametros((ps) => ps.map((p: any) => (p.nome === nome ? { ...p, valor_preview: valor } : p)));
  };

  // Liga/desliga uma biblioteca de funções NO NÓ (no.config.bibliotecas) — não no pipeline inteiro.
  const toggleBiblioteca = (id: string, bibId: string) => {
    marcarSujo();
    setNodes((ns) =>
      ns.map((n) => {
        if (n.id !== id) return n;
        const atuais: string[] = (n.data.config?.bibliotecas ?? []).map(String);
        const novas = atuais.includes(bibId) ? atuais.filter((b) => b !== bibId) : [...atuais, bibId];
        return { ...n, data: { ...n.data, config: { ...n.data.config, bibliotecas: novas } } };
      }),
    );
  };

  // Exclusão de nó com confirmação: o card pede (abre o modal); confirmar remove o nó + arestas.
  const excluirNo = (id: string) => {
    marcarSujo();
    setNodes((ns) => ns.filter((n) => n.id !== id));
    setEdges((es) => es.filter((e) => e.source !== id && e.target !== id));
    setPreviews((m) => {
      const { [id]: _p, ...resto } = m;
      return resto;
    });
    setPreviewSelo((m) => {
      const { [id]: _s, ...resto } = m;
      return resto;
    });
    setErrosPreview((m) => {
      const { [id]: _e, ...resto } = m;
      return resto;
    });
    if (selId === id) setSelId(null);
    setExcluirId(null);
  };

  // Preview do nó `id` — resultado/erro guardados POR NÓ e mostrados dentro do card.
  // Assinatura do que define o resultado do nó: SQL/config/op. Comparada na hora de exibir para
  // saber se a prévia na tela ainda corresponde ao que está escrito.
  const assinaturaNo = (id: string): string => {
    const n = nodes.find((x) => x.id === id);
    if (!n) return "";
    const d: any = n.data ?? {};
    return JSON.stringify({ sql: d.sql ?? "", config: d.config ?? null, op: d.op ?? null });
  };

  const preverNo = async (id: string) => {
    if (!pipelineId) return;
    const assinatura = assinaturaNo(id);
    setCarregandoPreview((m) => ({ ...m, [id]: true }));
    try {
      const res = await api.previewNo(pipelineId, id, definicao());
      setPreviews((m) => ({ ...m, [id]: res }));
      setPreviewSelo((m) => ({ ...m, [id]: { assinatura, em: Date.now() } }));
      setErrosPreview((m) => {
        const { [id]: _, ...resto } = m;
        return resto;
      });
    } catch (e: any) {
      setErrosPreview((m) => ({ ...m, [id]: e.message ?? "Falha no preview" }));
    } finally {
      setCarregandoPreview((m) => ({ ...m, [id]: false }));
    }
  };

  // feedback estrutural: marca em vermelho os nós sem nenhuma conexão (quando há mais de um nó)
  const conectados = new Set<string>();
  edges.forEach((e) => {
    conectados.add(e.source);
    conectados.add(e.target);
  });
  const nodesView = nodes.map((n) => ({
    ...n,
    // O card ABERTO fica por cima dos vizinhos (não empurra ninguém): o layout é o mesmo aberto e
    // fechado, então não há vão para "voltar" nem posição salva para consertar depois.
    zIndex: selId === n.id ? 20 : 1,
    data: {
      ...n.data,
      _desconectado: nodes.length > 1 && !conectados.has(n.id),
      _erroExec: errosExec?.[n.id], // falha da última execução neste nó (pinta o card)
    },
  }));

  const paletaFiltrada = PALETA.filter((it) => it.rotulo.toLowerCase().includes(buscaPaleta.trim().toLowerCase()));

  // Contexto consumido pelos cards (NoPipeline) p/ editar SQL/config e mostrar preview DENTRO do nó.
  const ctxEditor = {
    selId,
    conexoes,
    pipelines: pipelines.filter((p) => p.id !== pipelineId), // sem auto-referência
    fontesLake,
    tema,
    extensoesSql,
    patchConfig: atualizarConfig,
    patchSql: atualizarSql,
    patchOp: atualizarOp,
    patchRotulo: atualizarRotulo,
    patchDescricao: atualizarDescricao,
    pedirExcluir: setExcluirId,
    // A partir de ~8 nós o canvas não comporta todos os editores abertos; abaixo disso, o card
    // aberto é o que dá a experiência de planilha viva que o Studio promete.
    compacto: nodes.length > LIMIAR_COMPACTO,
    recolher: () => setSelId(null),
    // Alterna: acionar o nó já selecionado RECOLHE. Sem isso a única saída do modo expandido era
    // clicar em outro nó ou no vazio — e nada na tela dizia isso.
    selecionar: (id: string) => setSelId((atual) => (atual === id ? null : id)),
    preverNo,
    previews,
    previewSelo,
    assinaturaAtual: assinaturaNo,
    errosPreview,
    carregando: carregandoPreview,
    parametros,
    setValorPreview,
    bibliotecas: bibsDisponiveis,
    toggleBiblioteca,
    macroParaBib,
    salvarGrafo: async () => {
      if (!pipelineId) throw new Error("Abra um pipeline antes de salvar.");
      const r = await salvar();
      return r.criados;
    },
  };

  const noExcluir = nodes.find((n) => n.id === excluirId);

  return (
    <EditorNoContext.Provider value={ctxEditor}>
      {/* `recolhida-paleta` diz ao CSS que a faixa da esquerda está livre: o aviso "não cabe" volta
          para o canto quando a paleta não está lá. Sem a classe, a regra que existia no CSS nunca
          valia e o aviso ficava sempre deslocado. */}
      <div data-testid="studio" className={`sd-shell${paletaAberta ? "" : " recolhida-paleta"}`}>
        {/* Canvas React Flow ocupa tudo; a paleta FLUTUA por cima (dentro do canvas), estilo Langflow. */}
        <div
          className="sd-canvas"
          onDragOver={onDragOverCanvas}
          onDrop={onDropCanvas}
          onKeyDown={(ev) => {
            if (ev.key === "Escape") {
              // Escape recolhe o card aberto (saída universal de "modo").
              if (selId) {
                ev.stopPropagation();
                setSelId(null);
              }
              return;
            }
            if (ev.key !== "Delete" && ev.key !== "Backspace") return;
            // Digitando dentro do card (SQL, nome do nó, formulário) a tecla é edição de texto.
            const alvo = ev.target as HTMLElement | null;
            if (alvo && (alvo.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(alvo.tagName))) return;
            // `selId` é a seleção do EDITOR (clique no card, que é um nó custom); `n.selected` é a
            // seleção interna do React Flow e só existe quando se clica na moldura do nó.
            const noSel = nodes.find((n) => n.id === selId) ?? nodes.find((n) => n.selected);
            const arestasSel = edges.filter((e) => e.selected);
            if (noSel) {
              ev.preventDefault();
              setExcluirId(noSel.id); // mesma confirmação do botão × do card
            } else if (arestasSel.length) {
              ev.preventDefault();
              marcarSujo();
              setEdges((es) => es.filter((e) => !e.selected));
            }
          }}
        >
          <ReactFlow
            nodes={nodesView}
            edges={edges}
            nodeTypes={nodeTypes}
            defaultEdgeOptions={defaultEdgeOptions}
            onInit={(inst) => {
              rfRef.current = inst;
            }}
            onNodesChange={onNodesChangeSujo}
            onEdgesChange={onEdgesChangeSujo}
            onConnect={onConnect}
            onNodeClick={(_, n) => setSelId((atual) => (atual === n.id ? null : n.id))}
            onNodeDragStop={() => {
              // Arrastar é a pessoa DEFININDO a posição: vira base (e é o que se persiste).
              fixarBase(nodesRef.current);
              agendarLayout();
            }}
            onPaneClick={() => setSelId(null)}
            onEdgeDoubleClick={(_, edge) => excluirAresta(edge.id)}
            edgesFocusable
            // Delete/Backspace não apaga nó direto (ver `onKeyDown` do wrapper): a tecla passava
            // por cima da confirmação que o botão × do card já exigia.
            deleteKeyCode={null}
            fitView
            fitViewOptions={{ padding: 0.25, maxZoom: 1 }}
            minZoom={0.2}
          >
            <Background />
            <Controls />
          </ReactFlow>

          {/* Barra lateral de componentes DENTRO do canvas (recolhível): busca + itens arrastáveis. */}
          {paletaAberta ? (
            <aside className="sd-palette" aria-label="Componentes">
              <div className="sd-palette-top">
                <div className="sd-palette-busca">
                  <Icon name="search" />
                  <input
                    data-testid="palette-busca"
                    value={buscaPaleta}
                    onChange={(e) => setBuscaPaleta(e.target.value)}
                    placeholder="Buscar componente"
                  />
                </div>
                <button className="sd-palette-colapsar" data-testid="palette-recolher" aria-label="Recolher a paleta de componentes" title="Recolher" onClick={() => alternarPaleta(false)}>
                  <Icon name="chevron" />
                </button>
              </div>
              <div className="sd-palette-grupo">Componentes</div>
              <div className="sd-palette-itens">
                {paletaFiltrada.map((it) => (
                  <button
                    key={it.testid}
                    data-testid={it.testid}
                    className="palitem"
                    draggable
                    onDragStart={(e) => {
                      e.dataTransfer.setData(DND_TIPO, `${it.chave}:${it.op ?? ""}`);
                      e.dataTransfer.effectAllowed = "move";
                    }}
                    onClick={() => (it.chave === "guiada" ? addGuiada(it.op!) : addNo(it.chave))}
                    title={`Arraste para o canvas ou clique para adicionar — ${it.rotulo}`}
                  >
                    <span className={`palitem-ic palitem-ic--${it.op ?? it.chave}`}>
                      <Icon name={it.ic} />
                    </span>
                    <span className="palitem-rot">{it.rotulo}</span>
                  </button>
                ))}
              </div>
              {/* Ações do canvas, separadas dos componentes. Dois arranjos, porque um serve a uma
                  necessidade que o outro não atende: "Reorganizar" mantém a ordem em que os nós
                  foram criados (previsível, o layout de sempre); "Sem cruzar linhas" reordena cada
                  camada para as arestas não se atravessarem — o que importa quando o grafo tem
                  junções e o emaranhado esconde por onde o dado passa. */}
              <div className="sd-palette-rodape">
                <button
                  data-testid="reorganizar"
                  className="sd-acao"
                  onClick={() => reorganizar("camadas")}
                  title="Reorganiza os nós em camadas (fonte → saída), na ordem em que foram criados"
                >
                  <Icon name="layout" />
                  Reorganizar
                </button>
                <button
                  data-testid="reorganizar-sem-cruzamentos"
                  className="sd-acao"
                  onClick={() => reorganizar("sem-cruzamentos")}
                  title="Reorganiza em camadas reordenando cada uma para as linhas não se cruzarem"
                >
                  <Icon name="grid" />
                  Sem cruzar linhas
                </button>
              </div>
            </aside>
          ) : (
            <button className="sd-palette-abrir" data-testid="palette-abrir" aria-label="Abrir a paleta de componentes" title="Componentes" onClick={() => alternarPaleta(true)}>
              <Icon name="studio" />
            </button>
          )}

          {/* Grafo que nem no piso de zoom cabe: a tela diz isso e oferece a saída, em vez de
              abrir num recorte ilegível e deixar a pessoa procurar o que fazer. */}
          {naoCoube && nodes.length > 0 && (
            <div className="sd-naocoube" data-testid="grafo-nao-cabe" role="status">
              <span>O grafo não cabe legível nesta área.</span>
              <button type="button" className="pag-btn" onClick={() => reorganizar("camadas")}>
                Reorganizar
              </button>
            </div>
          )}

          {/* Versão (caption) — flutua no rodapé direito. data-testid estável usado pelo E2E. */}
          <div data-testid="pipeline-id" className="sd-versao">
            versão atual: v{versao || baseNumero}
          </div>

          {(temMontante || !montantePublica) && (
            <label
              className="sd-montante"
              data-testid="montante-publica"
              title={
                "Ao disparar este pipeline, os pipelines que ele lê por fonte tipo pipeline rodam junto. " +
                "Desmarcado, eles calculam e gravam o snapshot que este lê, mas NÃO publicam no lake — " +
                "para não mover o ponteiro que outro consumidor lê como congelado. Montante com e-mail, " +
                "escrita externa, HTTP ou ingestão é recusada nesse modo."
              }
            >
              <input
                type="checkbox"
                data-testid="montante-publica-check"
                checked={montantePublica}
                onChange={(e) => alternarMontante(e.target.checked)}
              />
              {montantePublica ? "A montante publica no lake" : "A montante NÃO publica no lake"}
            </label>
          )}
        </div>
      </div>

      {/* Modal de confirmação de exclusão de nó. */}
      {noExcluir && (
        <div className="modal-fundo" data-testid="modal-excluir" onClick={() => setExcluirId(null)}>
          <div className="modal-caixa" onClick={(e) => e.stopPropagation()}>
            <h3>Excluir nó</h3>
            <p>
              Remover <b>{noExcluir.data?.rotulo ?? noExcluir.id}</b> e suas conexões? Esta ação não pode ser desfeita.
            </p>
            <div className="modal-acoes">
              <button className="btn-sec" onClick={() => setExcluirId(null)}>
                Cancelar
              </button>
              <button data-testid="confirmar-excluir" className="btn-perigo" onClick={() => excluirNo(noExcluir.id)}>
                Excluir
              </button>
            </div>
          </div>
        </div>
      )}
    </EditorNoContext.Provider>
  );
});

export default StudioGraph;
