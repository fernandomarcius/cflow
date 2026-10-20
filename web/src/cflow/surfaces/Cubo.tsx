import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import * as api from "../../api/client";
import { SeletorVersao } from "../SeletorVersao";
import { campoSistema, ConfirmarAcao, Icon, ListaCadastro, type ItemCadastro } from "../ui";
import { diaSP, fmtHora, rotuloDia } from "../execucao/formato";
import { execucoesDaPagina } from "./cadeias";
import { dependentesDosCubos, type EstadoCubo, estadoCuboDoResumo, estadosDoResumo, type Fase } from "./listasResumo";

// ============================================================================
// Cubo — lista + construtor (fonte → dimensões → medidas nomeadas → prévia).
//
// O cubo agrega o SNAPSHOT do nó de um pipeline executado. Duas consequências que a tela precisa
// dizer em voz alta, sob pena de mostrar número que não corresponde ao que está na tela:
//   • as colunas disponíveis são as do snapshot da ÚLTIMA execução concluída (não as do rascunho
//     do pipeline) — e podem não existir mais;
//   • a prévia consulta a versão PUBLICADA do cubo (a API só consulta publicado), então enquanto
//     houver mudança não publicada a prévia é de OUTRA definição.
// ============================================================================

const AGG: [string, string][] = [
  ["unica", "Contagem única"],
  ["contagem", "Contagem"],
  ["soma", "Soma"],
  ["media", "Média"],
  ["min", "Mínimo"],
  ["max", "Máximo"],
];
// Valores preservados (round-trip do JSON versionado das definições já salvas).
const FORMATOS: [string, string][] = [
  ["Número", "Número"],
  ["R$", "R$ (moeda)"],
  ["%", "% (percentual)"],
];

// Gera a expressão SQL exibível da medida. Coluna sempre quotada: nome com espaço/acento
// quebraria o parser do DuckDB.
function qid(col: string): string {
  return `"${col.replace(/"/g, '""')}"`;
}
function unqid(s: string): string {
  const m = s.match(/^"(.*)"$/);
  return m ? m[1].replace(/""/g, '"') : s;
}
function cexpr(agg: string, col: string): string {
  if (agg === "contagem") return "count(*)";
  if (agg === "unica") return `count(distinct ${qid(col)})`;
  const fn: Record<string, string> = { soma: "sum", media: "avg", min: "min", max: "max" };
  return `${fn[agg] ?? "sum"}(${qid(col)})`;
}
// Só para EXIBIR: sem coluna escolhida, `cexpr` produz `sum("")` — uma expressão que parece
// pronta e não é. A definição salva continua saindo de `cexpr` (a pendência bloqueia antes).
function cexprExib(agg: string, col: string): string {
  if (agg !== "contagem" && !col) return "— falta escolher a coluna";
  return cexpr(agg, col);
}

interface Medida {
  nome: string;
  agg: string;
  col: string;
  fmt: string;
}

interface Coluna {
  nome: string;
  tipo: string; // "?" = veio da definição do cubo, não do snapshot lido
}

interface OrigemSnap {
  execucaoId: string;
  quando: string | null;
  linhas: number | null;
}

// Estado da leitura das colunas — os quatro casos são distinguíveis na tela (nunca "vazio" para
// dizer "não consegui ler").
type FaseCols =
  | { fase: "vazio" }
  | { fase: "carregando" }
  | { fase: "ok"; origem: OrigemSnap | null } // origem null = colunas calculadas do rascunho
  | { fase: "sem_execucao" }
  | { fase: "erro"; mensagem: string };

interface PrevResultado {
  colunas: string[];
  linhas: Record<string, unknown>[];
  execucaoId: string | null; // execução cujo snapshot a consulta agregou (vem em `origem`)
  // Medidas (nome → formato) da definição que PRODUZIU esta prévia. A formatação da célula sai
  // daqui, não do estado atual da tela: renomear uma medida depois da consulta fazia a coluna de
  // número virar coluna de texto (perdia separador de milhar e "R$") sobre os MESMOS valores.
  medidas: Record<string, string>;
  selo: string; // assinatura da definição no momento da consulta (detecta prévia envelhecida)
}

// Tinta escura FIXA para texto sobre o lime (`--lime` não muda entre os temas, mas `--navy` sim:
// no tema escuro vira #5b8bd0 e o número do passo caía a 2,03:1 — medido). Mesma armadilha que o
// bloco escuro do cflow.css já documentou no `.plabel`.
const TINTA_NO_LIME = "#14294d";

// ---- Exibição (pt-BR / America/Sao_Paulo) --------------------------------------------------
const NF = new Intl.NumberFormat("pt-BR");

function fmtMomento(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    const t = new Date(iso).toLocaleString("pt-BR", {
      timeZone: "America/Sao_Paulo",
      dateStyle: "short",
      timeStyle: "short",
    });
    return `${t} (GMT-3)`;
  } catch {
    return iso;
  }
}

// Separador de milhar aplicado sobre o TEXTO: DECIMAL chega da API como string e passar por float
// perderia dígito exato.
function milhar(inteiro: string): string {
  return inteiro.replace(/\B(?=(\d{3})+(?!\d))/g, ".");
}
function enfeitar(txt: string, formato?: string): string {
  if (formato === "R$") return `R$ ${txt}`;
  if (formato === "%") return `${txt}%`;
  return txt;
}

interface Celula {
  texto: string;
  titulo?: string;
  vazia?: boolean;
}

// Célula de MEDIDA (número). "—" (NULL) e "(vazio)" (texto vazio) são achados diferentes numa
// conferência: não podem cair no mesmo símbolo.
function celulaMedida(v: unknown, formato?: string): Celula {
  if (v === null || v === undefined) return { texto: "—", titulo: "sem valor (NULL)", vazia: true };
  if (typeof v === "number") {
    const txt = NF.format(Number.isInteger(v) ? v : Number(v.toFixed(2)));
    return { texto: enfeitar(txt, formato), titulo: String(v) };
  }
  if (typeof v === "string") {
    if (v === "") return { texto: "(vazio)", titulo: "texto vazio", vazia: true };
    const m = v.match(/^(-?)(\d+)(?:[.,](\d+))?$/);
    if (!m) return { texto: v };
    return { texto: enfeitar(m[1] + milhar(m[2]) + (m[3] ? `,${m[3]}` : ""), formato), titulo: v };
  }
  return { texto: String(v) };
}

// Célula de DIMENSÃO: texto puro (identificador não leva separador de milhar).
function celulaDim(v: unknown): Celula {
  if (v === null || v === undefined) return { texto: "—", titulo: "sem valor (NULL)", vazia: true };
  if (v === "") return { texto: "(vazio)", titulo: "texto vazio", vazia: true };
  return { texto: String(v) };
}

function plural(n: number, um: string, muitos: string): string {
  return `${NF.format(n)} ${n === 1 ? um : muitos}`;
}

// Lista longa em etiqueta de cabeçalho: um cubo de 22 dimensões enfileirava os 22 nomes, o muro
// empurrava o título "Prévia do cubo" para três linhas e ninguém lia nem uma coisa nem outra.
// Mostra os primeiros + "+N" e guarda a lista inteira no `title`.
function resumoLista(itens: string[], limite = 4): { texto: string; titulo: string } {
  const titulo = itens.join(", ");
  if (itens.length <= limite) return { texto: titulo, titulo };
  return { texto: `${itens.slice(0, limite).join(", ")} +${itens.length - limite}`, titulo };
}

// ---- Colunas do snapshot ---------------------------------------------------------------------
// Fonte da verdade: a última execução CONCLUÍDA do pipeline — é dela que a consulta do cubo lê.
// Barata (metadados do Parquet + amostra pequena) e honesta: o que está aqui é o que o cubo vai
// agregar. Ler pelo preview do rascunho (`previewNo`) recalcularia o pipeline inteiro sobre as
// fontes reais, sem os parâmetros de escopo — lento e, pior, uma lista de colunas que a consulta
// pode não ter.
async function colunasDoSnapshot(
  pipelineId: string,
  noId: string,
): Promise<{ cols: Coluna[]; origem: OrigemSnap } | null> {
  // `GET /execucoes` devolve CORRIDAS: com `pipelineId`, vêm as corridas que CONTÊM este pipeline,
  // e a raiz da corrida pode ser de OUTRO (no DAG, quem chama é o consumidor). Por isso achata e
  // filtra aqui, em vez de ler `itens[0].raiz` — que traria a execução do pipeline errado.
  // `limite: 2` porque agora a unidade é a corrida, não a execução.
  const r = await api.listarExecucoes({ pipelineId, status: "concluida", limite: 2, offset: 0 });
  const ex = execucoesDaPagina(r.itens).find(
    (e) => e.pipeline_id === pipelineId && e.status === "concluida",
  );
  if (!ex) return null;
  const am = await api.previewNoExecucao(ex.id, noId, 5);
  const cols = am.colunas.map((c) => ({ nome: c, tipo: tipoPorAmostra(c, am.amostra) }));
  return {
    cols,
    origem: { execucaoId: ex.id, quando: ex.concluida_em ?? ex.iniciada_em, linhas: am.contagem },
  };
}

// Tipo inferido do primeiro valor não-nulo da amostra (a amostra do snapshot não traz tipos).
// Snapshot vazio → "?" e a tela diz que não deu para saber.
function tipoPorAmostra(col: string, amostra: Record<string, unknown>[]): string {
  for (const l of amostra) {
    const v = l[col];
    if (v === null || v === undefined || v === "") continue;
    if (typeof v === "number") return "número";
    if (typeof v === "boolean") return "booleano";
    if (typeof v === "string" && /^-?\d+([.,]\d+)?$/.test(v)) return "número";
    return "texto";
  }
  return "?";
}

// Fallback EXPLÍCITO (botão): recalcula o nó sobre uma amostra das fontes. Traz tipos, mas pode
// demorar e, sem parâmetro de escopo, varrer a base — por isso nunca é automático.
async function colunasDoRascunho(
  pipelineId: string,
  noId: string,
  definicao: unknown,
): Promise<Coluna[]> {
  try {
    const pv = await api.previewNo(pipelineId, noId, definicao);
    return pv.colunas.map((c, i) => ({ nome: c, tipo: pv.tipos[i] ?? "texto" }));
  } catch {
    const disp = await api.colunasDisponiveis(pipelineId, noId, definicao);
    const vistas = new Set<string>();
    const out: Coluna[] = [];
    for (const lista of Object.values(disp)) {
      for (const c of lista) {
        if (!vistas.has(c)) {
          vistas.add(c);
          out.push({ nome: c, tipo: "?" });
        }
      }
    }
    if (out.length === 0) throw new Error("O pipeline não devolveu colunas para este nó.");
    return out;
  }
}

// ============================================================================
// LISTA — estado real de cada cubo
//
// `GET /cubos` devolve só `{id, nome, sistema}`. Sem enriquecer, toda linha repetia "abrir para
// editar dimensões e medidas": a mesma frase em todas, que não distingue um cubo do outro nem
// responde às duas perguntas da cadeia `fontes → grafo → CUBO → view → página`:
//   • de onde este cubo sai (pipeline + nó de saída);
//   • o que ele publica (dimensões/medidas), desde quando, e QUEM depende dele (views).
// As respostas vêm no `resumo` da própria listagem (`GET /cubos?resumo=true`, uma consulta no
// servidor; ver `listasResumo.ts`). Antes eram duas chamadas por cubo e uma por view — centenas de
// idas pelo gateway numa navegação. Linha sem resumo diz "não lido", sem derrubar a lista.
// ============================================================================

function Fraco({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <span style={{ color: "var(--ink-3)" }} title={title}>
      {children}
    </span>
  );
}

// Célula que respeita as três fases. "lendo…" ≠ "não lido" (falhou) ≠ o valor.
function celFase<T>(f: Fase<T> | undefined, render: (dado: T) => ReactNode): ReactNode {
  if (!f || f.fase === "carregando") return <Fraco title="lendo a definição">lendo…</Fraco>;
  if (f.fase === "erro") return <Fraco title="não foi possível ler esta informação">não lido</Fraco>;
  return render(f.dado);
}

// Dia + hora em America/Sao_Paulo (o fuso aparece uma vez, no cabeçalho da coluna).
function quando(iso: string | null): string {
  const dia = diaSP(iso);
  return dia ? `${rotuloDia(dia)} ${fmtHora(iso)}` : "sem data registrada";
}

export default function Cubo() {
  const [aberto, setAberto] = useState<string | null>(null);
  const [cubosRaw, setCubosRaw] = useState<api.PipelineResumo[]>([]);
  const [ehAdmin, setEhAdmin] = useState(false);
  const [sistemaBusy, setSistemaBusy] = useState<string | null>(null);
  const [erroLista, setErroLista] = useState("");
  const [carregando, setCarregando] = useState(true);

  // Enriquecimento da lista (ver bloco acima). `serieEnriq` = "última carga vence": recarregar duas
  // vezes rápido não pode deixar na tela o resultado da carga anterior.
  const [estado, setEstado] = useState<Record<string, Fase<EstadoCubo>>>({});
  const [nomePipeline, setNomePipeline] = useState<Record<string, string>>({});
  const [dependentes, setDependentes] = useState<Fase<Record<string, number>>>({ fase: "carregando" });
  const serieEnriq = useRef(0);

  const enriquecer = useCallback((lista: api.CuboComResumo[]) => {
    const meu = ++serieEnriq.current;
    setEstado(estadosDoResumo(lista, estadoCuboDoResumo));
    // Quem depende: views que leem cada cubo — tudo-ou-nada (ver `dependentesDosCubos`).
    setDependentes(dependentesDosCubos(lista));

    // Nomes dos pipelines: UMA chamada resolve a origem de todas as linhas (a definição do cubo
    // guarda só o id). Sem ela, a origem cai para o id curto — a lista não quebra.
    void api
      .listarPipelines()
      .then((ps) => {
        if (meu === serieEnriq.current) {
          setNomePipeline(Object.fromEntries(ps.map((p) => [p.id, p.nome])));
        }
      })
      .catch(() => undefined);
  }, []);

  const recarregarLista = useCallback(async () => {
    setCarregando(true);
    setErroLista("");
    try {
      const lista = await api.listarCubosComResumo();
      setCubosRaw(lista);
      enriquecer(lista);
    } catch (e: unknown) {
      setErroLista(e instanceof Error ? e.message : "Falha ao carregar os cubos.");
    } finally {
      setCarregando(false);
    }
  }, [enriquecer]);

  useEffect(() => {
    if (aberto === null) void recarregarLista();
  }, [aberto, recarregarLista]);

  useEffect(() => {
    void api
      .meAtual()
      .then((u) => setEhAdmin(u.perfil === "admin"))
      .catch(() => setEhAdmin(false));
  }, []);

  async function alternarSistema(id: string, sistema: boolean) {
    setErroLista("");
    setSistemaBusy(id);
    setCubosRaw((l) => l.map((c) => (c.id === id ? { ...c, sistema } : c)));
    try {
      await api.definirSistema("cubos", id, sistema);
    } catch (e: unknown) {
      setCubosRaw((l) => l.map((c) => (c.id === id ? { ...c, sistema: !sistema } : c)));
      setErroLista(e instanceof Error ? e.message : "Falha ao alterar a marca de sistema.");
    } finally {
      setSistemaBusy(null);
    }
  }

  const cubos: ItemCadastro[] = useMemo(
    () =>
      cubosRaw.map((c) => {
        const f = estado[c.id];
        const e = f?.fase === "ok" ? f.dado : null;

        // Sub-linha do nome = a ORIGEM. É a pergunta que a lista não respondia: este cubo agrega o
        // snapshot de qual pipeline, em qual nó de saída?
        let meta = "lendo a origem…";
        if (f?.fase === "erro") meta = "não foi possível ler a definição deste cubo";
        else if (e && !e.pipelineId) meta = "sem fonte — nenhum pipeline/nó escolhido";
        else if (e) {
          const nome = nomePipeline[e.pipelineId];
          meta = nome
            ? `${nome} · nó ${e.noId || "?"}`
            : `pipeline ${e.pipelineId.slice(0, 8)} (fora da sua lista) · nó ${e.noId || "?"}`;
        }

        // Sem medida o cubo não pode ser publicado (nenhum número nasce dele) — é achado, não zero.
        const pill = !e
          ? { label: f?.fase === "erro" ? "não lido" : "lendo…", cls: "off" }
          : !e.publicada
            ? { label: "sem versão publicada", cls: "off" }
            : e.rascunhoAdiante
              ? { label: `rascunho v${e.rascunhoAdiante}`, cls: "skip" }
              : { label: "publicada", cls: "ok" };

        return {
          id: c.id,
          nome: c.nome,
          meta,
          status: pill.label,
          scls: pill.cls,
          campos: [
            {
              // Uma coluna só: com "Dimensões" e "Medidas" separadas, a tabela roubava largura da
              // coluna do NOME e os nomes longos quebravam em três linhas.
              rotulo: "Estrutura",
              valor: celFase(f, (d) => (
                <span style={{ whiteSpace: "nowrap" }}>
                  {d.dims ? (
                    plural(d.dims, "dimensão", "dimensões")
                  ) : (
                    <Fraco title="nenhuma dimensão marcada">sem dimensão</Fraco>
                  )}
                  {" · "}
                  {d.meds ? (
                    plural(d.meds, "medida", "medidas")
                  ) : (
                    <Fraco title="sem medida nomeada o cubo não pode ser publicado">sem medida</Fraco>
                  )}
                </span>
              )),
            },
            {
              rotulo: "Usado por",
              valor: celFase(dependentes, (mapa) => {
                const n = mapa[c.id] ?? 0;
                return n ? (
                  <span title="views que leem as medidas deste cubo">{plural(n, "view", "views")}</span>
                ) : (
                  <Fraco title="nenhuma view lê este cubo">nenhuma view</Fraco>
                );
              }),
            },
            {
              rotulo: "Publicada (GMT-3)",
              valor: celFase(f, (d) =>
                d.publicada ? (
                  // nowrap: carimbo quebrado em duas linhas deixa de se ler como um momento.
                  <span style={{ whiteSpace: "nowrap" }} title="versão que view, página e relatório enxergam">
                    v{d.publicada.numero} · {quando(d.publicada.em)}
                  </span>
                ) : (
                  <Fraco title="só a versão publicada é consultável">nunca</Fraco>
                ),
              ),
            },
            campoSistema(ehAdmin, c.id, c.sistema === true),
          ].filter((x): x is NonNullable<typeof x> => x !== null),
        };
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [cubosRaw, ehAdmin, sistemaBusy, estado, nomePipeline, dependentes],
  );

  async function novoCubo() {
    setErroLista("");
    try {
      const { id } = await api.criarCubo("Novo cubo");
      setAberto(id);
    } catch (e: unknown) {
      setErroLista(e instanceof Error ? e.message : "Falha ao criar cubo.");
    }
  }

  // Sem `window.confirm`: o GridLista já confirma no modal do design system (duas confirmações
  // seguidas eram uma delas fora do tema e sem foco controlado).
  async function excluir(id: string) {
    setErroLista("");
    try {
      await api.excluirCubo(id);
      await recarregarLista();
    } catch (e: unknown) {
      setErroLista(e instanceof Error ? e.message : "Falha ao excluir cubo.");
    }
  }

  async function renomear(id: string, nome: string) {
    setErroLista("");
    try {
      await api.renomearCubo(id, nome);
      await recarregarLista();
    } catch (e: unknown) {
      setErroLista(e instanceof Error ? e.message : "Falha ao renomear cubo.");
    }
  }

  if (aberto !== null) {
    return <Construtor cuboId={aberto} onVoltar={() => setAberto(null)} />;
  }

  // Carregando / falhou / vazio são estados DIFERENTES: com um só, "Nada cadastrado ainda"
  // aparecia enquanto a lista carregava e também quando a chamada falhava.
  if (carregando && cubosRaw.length === 0) {
    return (
      <>
        <div className="page-head">
          <h1 className="display">Cubos</h1>
        </div>
        <div className="pad">
          <div className="card lc-empty" role="status">
            Carregando cubos…
          </div>
        </div>
      </>
    );
  }

  if (erroLista && cubosRaw.length === 0) {
    return (
      <>
        <div className="page-head">
          <h1 className="display">Cubos</h1>
        </div>
        <div className="pad">
          <div className="exec-banner-falha" role="alert">
            <span className="dot" />
            <span>{erroLista}</span>
            <button type="button" className="pag-btn" onClick={() => void recarregarLista()}>
              Tentar de novo
            </button>
          </div>
        </div>
      </>
    );
  }

  return (
    <>
      {erroLista && (
        <div className="pad" style={{ paddingBottom: 0 }}>
          <div className="exec-banner-falha" role="alert">
            <span className="dot" />
            <span>{erroLista}</span>
          </div>
        </div>
      )}
      <ListaCadastro
              erro={!!erroLista}
        titulo="Cubos"
        novoLabel="Novo cubo"
        itens={cubos}
        contagem={plural(cubos.length, "cubo", "cubos")}
        carregando={carregando && cubosRaw.length === 0}
        onNovo={() => void novoCubo()}
        onAbrir={(id) => setAberto(id)}
        onExcluir={(id) => void excluir(id)}
        onRenomear={(id, nomeAtual) => void renomear(id, nomeAtual)}
        onSistema={ehAdmin ? (id, s) => void alternarSistema(id, s) : undefined}
        ehSistema={(it) => cubosRaw.find((c) => c.id === it.id)?.sistema === true}
      />
    </>
  );
}

// ============ CONSTRUTOR (3 passos + prévia) ============
function Construtor({ cuboId, onVoltar }: { cuboId: string; onVoltar: () => void }) {
  const [nome, setNome] = useState("");
  const [nomeSalvo, setNomeSalvo] = useState(""); // último nome confirmado no backend
  const [renomeando, setRenomeando] = useState(false);
  const [baseNumero, setBaseNumero] = useState(0);
  const [carregando, setCarregando] = useState(true);
  const [erro, setErro] = useState("");
  const [aviso, setAviso] = useState("");

  // Passo 1 — fonte
  const [pipelines, setPipelines] = useState<api.PipelineResumo[]>([]);
  const [pipe, setPipe] = useState("");
  const [no, setNo] = useState("");
  const [nosPipe, setNosPipe] = useState<{ id: string; rotulo: string; tipo: string }[]>([]);
  const [cols, setCols] = useState<Coluna[]>([]);
  const [estadoCols, setEstadoCols] = useState<FaseCols>({ fase: "vazio" });
  const [definicaoPipe, setDefinicaoPipe] = useState<unknown>(null);
  const [buscaCol, setBuscaCol] = useState("");

  // Passo 2 — dimensões (coluna → nome amigável) / Passo 3 — medidas
  const [dims, setDims] = useState<Record<string, string>>({});
  const [meds, setMeds] = useState<Medida[]>([]);

  // Prévia / publicação
  const [previa, setPrevia] = useState<PrevResultado | null>(null);
  const [erroPrevia, setErroPrevia] = useState("");
  const [carregandoPrevia, setCarregandoPrevia] = useState(false);
  const [salvando, setSalvando] = useState(false);
  const [publicado, setPublicado] = useState<number | null>(null);
  const [confirmando, setConfirmando] = useState<null | "publicar" | "voltar">(null);

  // Versões. `versaoSel` = versão CARREGADA (null = última). baseNumero = nº máximo (lock) —
  // editar/publicar uma versão anterior gera uma NOVA (a anterior fica intacta).
  const [versoes, setVersoes] = useState<api.VersaoResumo[]>([]);
  const [versaoSel, setVersaoSel] = useState<number | null>(null);
  const ultima = versoes.length ? versoes[0].numero : null;
  const temPublicada = versoes.some((v) => v.status === "publicada");
  const versaoAtiva = versoes.find((v) => v.ativa)?.numero ?? null;

  // Definição carregada do backend — referência para saber se há mudança não salva.
  const [assinaturaSalva, setAssinaturaSalva] = useState("");

  const carregarVersoes = useCallback(() => {
    api.listarVersoesCubo(cuboId).then(setVersoes).catch(() => setVersoes([]));
  }, [cuboId]);

  const aplicarCubo = useCallback((cubo: any) => {
    setNome(cubo.nome);
    setNomeSalvo(cubo.nome);
    setBaseNumero(cubo.ultimo ?? cubo.numero); // lock = máximo
    const d = cubo.definicao || {};
    const fonte = d.fonte || {};
    setPipe(fonte.pipeline_id || "");
    setNo(fonte.no_id || "");
    const dm: Record<string, string> = {};
    if (Array.isArray(d.dimensoes)) for (const x of d.dimensoes) dm[x.coluna] = x.nome;
    setDims(dm);
    const ms: Medida[] = Array.isArray(d.medidas) ? d.medidas.map((m: any) => parseMedida(m)) : [];
    setMeds(ms);
    setAssinaturaSalva(assinar(fonte.pipeline_id || "", fonte.no_id || "", dm, ms));
  }, []);

  // Última chamada vence (mesma régua do Pipeline, da View e da Página): das cargas do detalhe que
  // abrir o construtor dispara (o StrictMode dobra o efeito de montagem no dev), a que chegava por
  // último reescrevia o nome e as medidas que a pessoa já tinha editado. Só a mais recente aplica.
  const serieCarga = useRef(0);
  const carregar = useCallback(
    async (numero?: number) => {
      const minha = ++serieCarga.current;
      setCarregando(true);
      setErro("");
      try {
        const det = await api.obterCubo(cuboId, numero);
        if (minha !== serieCarga.current) return;
        aplicarCubo(det);
      } catch (e: unknown) {
        if (minha !== serieCarga.current) return;
        setErro(e instanceof Error ? e.message : "Falha ao abrir o cubo.");
      } finally {
        if (minha === serieCarga.current) setCarregando(false);
      }
    },
    [cuboId, aplicarCubo],
  );

  useEffect(() => {
    void carregar();
    carregarVersoes();
    api.listarPipelines().then(setPipelines).catch(() => setPipelines([]));
  }, [carregar, carregarVersoes]);

  const selecionarVersao = async (numero: number | null) => {
    setVersaoSel(numero);
    setErro("");
    setAviso("");
    setPublicado(null);
    // A prévia era de OUTRA versão: mantê-la na tela sob o cabeçalho da versão recém-carregada é
    // exibir número de uma definição como se fosse de outra.
    setPrevia(null);
    setErroPrevia("");
    await carregar(numero ?? undefined);
  };

  // Ao escolher pipeline, busca a definição (nós + fallback de colunas).
  useEffect(() => {
    if (!pipe) {
      setNosPipe([]);
      setDefinicaoPipe(null);
      return;
    }
    let vivo = true;
    (async () => {
      try {
        const det = await api.obterPipeline(pipe);
        if (!vivo) return;
        setDefinicaoPipe(det.definicao);
        const nos = (det.definicao?.nos ?? []) as any[];
        const lista = nos.map((n) => ({
          id: String(n.id),
          rotulo: String(n.nome ?? n.id),
          tipo: String(n.tipo ?? ""),
        }));
        setNosPipe(lista);
        // Só escolhe por conta própria quando NADA está escolhido (cubo novo). Trocar o nó de um
        // cubo salvo em silêncio mudaria a fonte do relatório sem ninguém pedir.
        setNo((atual) => {
          if (atual) return atual;
          const saidas = lista.filter((n) => n.tipo === "saida");
          const candidatos = saidas.length ? saidas : lista;
          return candidatos[candidatos.length - 1]?.id ?? "";
        });
      } catch (e: unknown) {
        if (vivo) setErro(e instanceof Error ? e.message : "Falha ao abrir o pipeline da fonte.");
      }
    })();
    return () => {
      vivo = false;
    };
  }, [pipe]);

  // Colunas do snapshot da última execução concluída. "Última chamada vence": trocar pipeline/nó
  // rápido não pode deixar na tela as colunas da escolha anterior.
  const serieCols = useRef(0);
  const lerColunas = useCallback(async () => {
    if (!pipe || !no) {
      setCols([]);
      setEstadoCols({ fase: "vazio" });
      return;
    }
    const meu = ++serieCols.current;
    setEstadoCols({ fase: "carregando" });
    try {
      const r = await colunasDoSnapshot(pipe, no);
      if (meu !== serieCols.current) return;
      if (!r) {
        setCols([]);
        setEstadoCols({ fase: "sem_execucao" });
        return;
      }
      setCols(r.cols);
      setEstadoCols({ fase: "ok", origem: r.origem });
    } catch (e: unknown) {
      if (meu !== serieCols.current) return;
      setCols([]);
      setEstadoCols({
        fase: "erro",
        mensagem: e instanceof Error ? e.message : "Falha ao ler as colunas do snapshot.",
      });
    }
  }, [pipe, no]);

  useEffect(() => {
    void lerColunas();
  }, [lerColunas]);

  const [lendoRascunho, setLendoRascunho] = useState(false);
  async function lerColunasDoRascunho() {
    if (!pipe || !no || !definicaoPipe) return;
    setLendoRascunho(true);
    try {
      const c = await colunasDoRascunho(pipe, no, definicaoPipe);
      setCols(c);
      setEstadoCols({ fase: "ok", origem: null });
    } catch (e: unknown) {
      setEstadoCols({
        fase: "erro",
        mensagem: e instanceof Error ? e.message : "Falha ao calcular as colunas do rascunho.",
      });
    } finally {
      setLendoRascunho(false);
    }
  }

  // Universo de colunas = as do snapshot + as que a DEFINIÇÃO já referencia. Sem a segunda parte,
  // um cubo salvo abria com o passo 2 vazio (as dimensões existiam, mas nada as desenhava) e a
  // coluna da medida aparecia em branco no select.
  const universo: Coluna[] = useMemo(() => {
    const mapa = new Map<string, Coluna>();
    for (const c of cols) mapa.set(c.nome, c);
    for (const c of Object.keys(dims)) if (!mapa.has(c)) mapa.set(c, { nome: c, tipo: "?" });
    for (const m of meds) if (m.col && !mapa.has(m.col)) mapa.set(m.col, { nome: m.col, tipo: "?" });
    return [...mapa.values()];
  }, [cols, dims, meds]);

  const noSnapshot = useMemo(() => new Set(cols.map((c) => c.nome)), [cols]);
  const colsOk = estadoCols.fase === "ok";
  // Coluna referenciada que NÃO existe no snapshot lido: a consulta do cubo vai falhar. É achado,
  // não detalhe estético.
  const ausentes = useMemo(() => {
    if (!colsOk) return [] as string[];
    const refs = new Set<string>([...Object.keys(dims), ...meds.map((m) => m.col).filter(Boolean)]);
    return [...refs].filter((c) => !noSnapshot.has(c));
  }, [colsOk, dims, meds, noSnapshot]);

  const noAusente = !!no && nosPipe.length > 0 && !nosPipe.some((n) => n.id === no);

  const colunasVisiveis = useMemo(() => {
    const q = buscaCol.trim().toLowerCase();
    if (!q) return universo;
    return universo.filter((c) => c.nome.toLowerCase().includes(q));
  }, [universo, buscaCol]);

  // ---- Edição ----
  const toggleDim = (c: string) =>
    setDims((d) => {
      const n = { ...d };
      if (n[c] !== undefined) delete n[c];
      else n[c] = c;
      return n;
    });
  const renameDim = (c: string, v: string) => setDims((d) => ({ ...d, [c]: v }));
  const setMed = (i: number, k: keyof Medida, v: string) =>
    setMeds((m) => m.map((x, j) => (j === i ? { ...x, [k]: v } : x)));
  // Só pré-seleciona coluna quando o tipo é CONHECIDO e numérico. Com snapshot vazio todo tipo é
  // "?" e o antigo fallback (`universo[0]`) escolhia a primeira coluna — normalmente a data —
  // deixando a medida nascer como `sum("data")`: plausível na tela e errada na consulta.
  const addMed = () => {
    const num = universo.find((c) => c.tipo === "número")?.nome ?? "";
    setMeds((m) => [...m, { nome: "", agg: "soma", col: num, fmt: "Número" }]);
  };
  const delMed = (i: number) => setMeds((m) => m.filter((_, j) => j !== i));

  // ---- Definição (JSON versionado) ----
  const montarDefinicao = useCallback(
    () => ({
      fonte: { pipeline_id: pipe, no_id: no },
      dimensoes: Object.entries(dims).map(([coluna, nm]) => ({ nome: nm, coluna })),
      medidas: meds.map((m) => ({
        nome: m.nome.trim(),
        expressao: cexpr(m.agg, m.col),
        formato: m.fmt,
      })),
    }),
    [pipe, no, dims, meds],
  );

  const assinaturaAtual = assinar(pipe, no, dims, meds);
  const sujo = assinaturaAtual !== assinaturaSalva;
  // Prévia gerada antes da edição atual: os números continuam sendo os da consulta antiga, então
  // ela é rotulada e esmaecida (mesma regra do preview de nó no editor de pipeline).
  const previaVelha = !!previa && previa.selo !== assinaturaAtual;

  // Pendências = o que a definição precisa para ser aceita (mesmas regras do backend, ditas antes
  // de gastar um round-trip que voltaria 422).
  const pendencias = useMemo(() => {
    const p: string[] = [];
    if (!pipe || !no) p.push("Passo 1: escolha o pipeline e o nó de saída da fonte.");
    if (meds.length === 0)
      p.push("Passo 3: adicione ao menos uma medida — todo número do relatório nasce de uma medida nomeada.");
    const nomesMed = meds.map((m) => m.nome.trim());
    if (nomesMed.some((n) => !n)) p.push("Passo 3: toda medida precisa de nome.");
    const dupMed = nomesMed.filter((n, i) => n && nomesMed.indexOf(n) !== i);
    if (dupMed.length) p.push(`Passo 3: nome de medida repetido (${[...new Set(dupMed)].join(", ")}).`);
    if (meds.some((m) => m.agg !== "contagem" && !m.col))
      p.push("Passo 3: escolha a coluna das medidas que não são Contagem.");
    const nomesDim = Object.values(dims).map((n) => n.trim());
    if (nomesDim.some((n) => !n)) p.push("Passo 2: toda dimensão precisa de um nome amigável.");
    const dupDim = nomesDim.filter((n, i) => n && nomesDim.indexOf(n) !== i);
    if (dupDim.length) p.push(`Passo 2: nome de dimensão repetido (${[...new Set(dupDim)].join(", ")}).`);
    return p;
  }, [pipe, no, dims, meds]);

  const podeGravar = pendencias.length === 0 && !salvando;

  async function renomear() {
    const limpo = nome.trim();
    if (!limpo || limpo === nomeSalvo) return;
    setRenomeando(true);
    setErro("");
    setAviso("");
    try {
      const r = await api.renomearCubo(cuboId, limpo);
      setNome(r.nome);
      setNomeSalvo(r.nome);
      setAviso("Nome atualizado.");
    } catch (e: unknown) {
      setErro(e instanceof Error ? e.message : "Falha ao renomear o cubo.");
    } finally {
      setRenomeando(false);
    }
  }

  // Devolve o NOVO número da versão (não um booleano): publicar precisa dele como `base_numero`.
  // Com o `baseNumero` do estado, o publicar ia com o número de ANTES do salvamento e o backend
  // respondia 409 "Versão-base desatualizada" — publicar cubo não funcionava.
  async function salvar(): Promise<number | null> {
    setSalvando(true);
    setErro("");
    setAviso("");
    try {
      const def = montarDefinicao();
      const { numero } = await api.salvarCubo(cuboId, def, baseNumero);
      setBaseNumero(numero);
      setVersaoSel(null);
      setAssinaturaSalva(assinar(pipe, no, dims, meds));
      carregarVersoes();
      return numero;
    } catch (e: unknown) {
      setErro(e instanceof Error ? e.message : "Falha ao salvar o rascunho.");
      return null;
    } finally {
      setSalvando(false);
    }
  }

  async function salvarRascunho() {
    const n = await salvar();
    if (n != null) setAviso(`Rascunho salvo como versão v${n} (ainda não publicada).`);
  }

  async function publicar() {
    // Sem mudança na tela, o rascunho vigente JÁ é o que se quer publicar: salvar de novo só
    // criaria uma versão idêntica no histórico.
    const base = sujo ? await salvar() : baseNumero;
    if (base == null) return;
    setSalvando(true);
    setErro("");
    try {
      const { numero } = await api.publicarCubo(cuboId, base);
      setBaseNumero(numero);
      setVersaoSel(null);
      carregarVersoes();
      setPublicado(numero);
      setAviso("");
      void prever();
    } catch (e: unknown) {
      setErro(e instanceof Error ? e.message : "Falha ao publicar o cubo.");
    } finally {
      setSalvando(false);
    }
  }

  // A API consulta o cubo PUBLICADO (por nome de dimensão/medida). Enquanto houver mudança não
  // publicada, a prévia é de outra definição — a tela diz isso em vez de deixar acreditar.
  async function prever() {
    setCarregandoPrevia(true);
    setErroPrevia("");
    // Congelado ANTES da chamada: é esta definição que a consulta usa, e é por ela que a grade
    // formata e se declara envelhecida.
    const selo = assinar(pipe, no, dims, meds);
    const medidasDaConsulta: Record<string, string> = {};
    for (const m of meds) if (m.nome.trim()) medidasDaConsulta[m.nome.trim()] = m.fmt;
    try {
      const res = await api.consultarCubo(
        cuboId,
        Object.values(dims).map((n) => n.trim()).filter(Boolean),
        Object.keys(medidasDaConsulta),
      );
      setPrevia({
        colunas: res.colunas ?? [],
        linhas: res.linhas ?? [],
        execucaoId: res.origem?.execucao_id ? String(res.origem.execucao_id) : null,
        medidas: medidasDaConsulta,
        selo,
      });
    } catch (e: unknown) {
      setErroPrevia(e instanceof Error ? e.message : "Falha ao pré-visualizar.");
      setPrevia(null);
    } finally {
      setCarregandoPrevia(false);
    }
  }

  const sair = () => (sujo ? setConfirmando("voltar") : onVoltar());

  if (carregando) {
    return (
      <div className="page-head">
        <button type="button" className="link-back" onClick={sair}>
          ← Cubos
        </button>
        <h1 className="display">Construtor de cubo</h1>
        <p className="sub" role="status">
          Carregando o cubo…
        </p>
      </div>
    );
  }

  const nomesMedidas = meds.map((m) => m.nome.trim()).filter(Boolean);
  const dimsMarcadas = Object.keys(dims).length;
  const porDims = resumoLista(Object.values(dims));
  const porMeds = resumoLista(nomesMedidas);
  // Tipo por coluna (inferido da amostra do snapshot). "?" = não deu para saber.
  const tipoDaColuna = new Map(universo.map((c) => [c.nome, c.tipo]));
  // Colunas cujo tipo não pôde ser inferido mesmo havendo linha no snapshot: a amostra só trouxe
  // vazio ali. Diferente de "snapshot vazio" — e a tela diz qual dos dois é.
  const semTipo = cols.filter((c) => c.tipo === "?").map((c) => c.nome);

  return (
    <>
      {confirmando === "publicar" && (
        <ConfirmarAcao
          titulo="Publicar o cubo"
          corpo={
            <>
              Publicar cria a versão <b>v{baseNumero + (sujo ? 2 : 1)}</b> do cubo, registra quem
              publicou na trilha de auditoria e passa a valer para todas as views e páginas que usam
              este cubo. A versão publicada não pode ser apagada — só substituída por outra.
            </>
          }
          rotuloConfirmar="Publicar cubo"
          onConfirmar={() => {
            setConfirmando(null);
            void publicar();
          }}
          onCancelar={() => setConfirmando(null)}
        />
      )}
      {confirmando === "voltar" && (
        <ConfirmarAcao
          titulo="Sair sem salvar"
          corpo={<>Há mudanças que não foram salvas neste cubo. Se sair agora, elas são perdidas.</>}
          rotuloConfirmar="Sair sem salvar"
          perigo
          onConfirmar={() => {
            setConfirmando(null);
            onVoltar();
          }}
          onCancelar={() => setConfirmando(null)}
        />
      )}

      <div className="page-head">
        <button type="button" className="link-back" onClick={sair}>
          ← Cubos
        </button>
        <div style={{ display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap" }}>
          <h1 className="display" style={{ margin: 0 }}>
            Construtor de cubo
          </h1>
          {versoes.length > 0 && (
            <SeletorVersao
              versoes={versoes}
              ultima={ultima}
              sel={versaoSel}
              ocupado={salvando}
              onSelecionar={selecionarVersao}
            />
          )}
          {sujo && (
            <span className="ver-aviso" data-testid="cubo-sujo">
              mudanças não salvas
            </span>
          )}
        </div>
        <p className="sub">
          O cubo agrega o snapshot de um nó do pipeline e publica <b>medidas nomeadas</b> — é delas
          que view, página e relatório tiram todo número (nenhuma fórmula solta na visualização).
        </p>
      </div>

      <div className="pad">
        {erro && (
          <div className="exec-banner-falha" role="alert" style={{ marginBottom: 14 }}>
            <span className="dot" />
            <span>{erro}</span>
          </div>
        )}

        <div style={{ maxWidth: 560, marginBottom: 16 }}>
          <label className="fld" htmlFor="cubo-nome-in">
            Nome do cubo
          </label>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <input
              id="cubo-nome-in"
              className="nm"
              data-testid="cubo-nome"
              style={{ fontFamily: '"Space Grotesk"', fontWeight: 600, fontSize: 14 }}
              value={nome}
              onChange={(e) => setNome(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void renomear();
              }}
            />
            <button
              type="button"
              className="top-action"
              data-testid="renomear-cubo"
              style={{ flex: "none" }}
              disabled={renomeando || !nome.trim() || nome.trim() === nomeSalvo}
              onClick={() => void renomear()}
            >
              {renomeando ? "Renomeando…" : "Renomear"}
            </button>
          </div>
        </div>

        {/* ---------- Passo 1 — Fonte ---------- */}
        <div className="cstep">
          <div className="ch">
            <span className="n" style={{ color: TINTA_NO_LIME }}>1</span>
            <h3>Fonte</h3>
            <span className="hint">de qual pipeline e nó de saída vêm os dados</span>
          </div>
          <div className="cb">
            <div className="selrow">
              <div className="f">
                <label htmlFor="cubo-pipe">Pipeline</label>
                <select id="cubo-pipe" value={pipe} onChange={(e) => setPipe(e.target.value)}>
                  <option value="">— escolha —</option>
                  {pipelines.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.nome}
                    </option>
                  ))}
                </select>
              </div>
              <div className="f">
                <label htmlFor="cubo-no">Nó de saída</label>
                <select
                  id="cubo-no"
                  value={no}
                  onChange={(e) => setNo(e.target.value)}
                  disabled={!nosPipe.length}
                >
                  {!nosPipe.length && <option value="">—</option>}
                  {noAusente && <option value={no}>{no} — não existe mais no pipeline</option>}
                  {nosPipe.map((n) => (
                    <option key={n.id} value={n.id}>
                      {n.rotulo}
                      {n.tipo ? ` · ${n.tipo}` : ""}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            {noAusente && (
              <div className="exec-aviso" role="alert" style={{ marginTop: 12 }}>
                O nó <b>{no}</b> não existe mais neste pipeline. Escolha outro nó de saída — sem
                isso a consulta do cubo não encontra o snapshot.
              </div>
            )}

            <div className="eyebrow" style={{ marginTop: 14 }}>
              Colunas do snapshot
            </div>
            <div className="cubo-fonte-estado" role="status">
              {estadoCols.fase === "vazio" && (
                <span>Escolha o pipeline e o nó para ver as colunas disponíveis.</span>
              )}
              {estadoCols.fase === "carregando" && <span>Lendo as colunas do snapshot…</span>}
              {estadoCols.fase === "sem_execucao" && (
                <span>
                  Este pipeline ainda não tem execução concluída — o cubo agrega o snapshot de uma
                  execução, então não há colunas para listar. Rode o pipeline em Execuções, ou
                  calcule as colunas a partir do rascunho.
                </span>
              )}
              {estadoCols.fase === "ok" && estadoCols.origem && (
                <span>
                  {plural(cols.length, "coluna", "colunas")} · snapshot da execução{" "}
                  <b>{estadoCols.origem.execucaoId.slice(0, 8)}</b> de{" "}
                  {fmtMomento(estadoCols.origem.quando)}
                  {estadoCols.origem.linhas != null && (
                    <> · {plural(estadoCols.origem.linhas, "linha", "linhas")}</>
                  )}
                  {/* Coluna sem tipo COM linha no snapshot é outro caso (amostra só com vazio) — o
                      snapshot vazio é dito no aviso abaixo, não aqui em cinza. */}
                  {estadoCols.origem.linhas !== 0 && semTipo.length > 0 && (
                    <>
                      {" · "}
                      <span title={semTipo.join(", ")}>
                        {plural(semTipo.length, "coluna sem tipo", "colunas sem tipo")} (só valor
                        vazio na amostra)
                      </span>
                    </>
                  )}
                </span>
              )}
              {/* Snapshot VAZIO é o caso em que o construtor engana mais: dá para montar uma Soma
                  inteira sobre colunas cujo tipo ninguém conferiu, publicar, e o cubo não devolver
                  número. Antes isso vivia em micro-copy cinza no fim da linha de estado. */}
              {estadoCols.fase === "ok" && estadoCols.origem?.linhas === 0 && (
                <div className="exec-aviso" style={{ marginTop: 10, flexBasis: "100%" }}>
                  <b>A execução de origem terminou sem nenhuma linha.</b> Os nomes das{" "}
                  {plural(cols.length, "coluna", "colunas")} vêm dos metadados do snapshot, então dá
                  para montar dimensões e medidas — mas <b>nenhum tipo de coluna pôde ser conferido</b>
                  : uma medida de Soma montada agora só será confrontada com o dado quando o pipeline
                  rodar. Até lá, a prévia e a consulta do cubo vêm vazias.
                </div>
              )}
              {estadoCols.fase === "ok" && !estadoCols.origem && (
                <span>
                  {plural(cols.length, "coluna", "colunas")} calculadas do <b>rascunho</b> do
                  pipeline — não são as de um snapshot; a consulta do cubo só verá estas colunas
                  depois de uma execução.
                </span>
              )}
              {estadoCols.fase === "erro" && (
                <span className="cubo-erro-inline">{estadoCols.mensagem}</span>
              )}
              {(estadoCols.fase === "erro" || estadoCols.fase === "sem_execucao") && (
                <>
                  <button type="button" className="pag-btn" onClick={() => void lerColunas()}>
                    Tentar de novo
                  </button>
                  <button
                    type="button"
                    className="pag-btn"
                    disabled={lendoRascunho || !definicaoPipe}
                    title="Recalcula o nó sobre uma amostra das fontes; pode demorar."
                    onClick={() => void lerColunasDoRascunho()}
                  >
                    {lendoRascunho ? "Calculando…" : "Calcular pelo rascunho"}
                  </button>
                </>
              )}
            </div>

            {ausentes.length > 0 && (
              <div className="exec-aviso" role="alert" style={{ marginTop: 10 }}>
                {plural(ausentes.length, "coluna usada por este cubo não existe", "colunas usadas por este cubo não existem")}{" "}
                no snapshot atual: <b>{ausentes.join(", ")}</b>. A consulta falha enquanto elas
                estiverem em uma dimensão ou medida.
              </div>
            )}
          </div>
        </div>

        {/* ---------- Passo 2 — Dimensões ---------- */}
        <div className="cstep">
          <div className="ch">
            <span className="n" style={{ color: TINTA_NO_LIME }}>2</span>
            <h3>Dimensões</h3>
            <span className="hint">por quais campos dá pra agrupar/filtrar</span>
          </div>
          <div className="cb">
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 12,
                flexWrap: "wrap",
                marginBottom: 9,
              }}
            >
              <span className="cubo-nota" data-testid="cubo-dims-contagem">
                {dimsMarcadas === 0
                  ? "nenhuma dimensão marcada"
                  : `${plural(dimsMarcadas, "dimensão marcada", "dimensões marcadas")} de ${universo.length}`}
              </span>
              {universo.length > 8 && (
                <input
                  className="nm cubo-busca"
                  type="search"
                  value={buscaCol}
                  aria-label="Filtrar colunas por nome"
                  placeholder="filtrar colunas…"
                  onChange={(e) => setBuscaCol(e.target.value)}
                />
              )}
            </div>

            {universo.length === 0 ? (
              <p className="cubo-vazio">
                Sem colunas para marcar. Resolva a fonte no passo 1 — as dimensões são colunas do
                snapshot.
              </p>
            ) : colunasVisiveis.length === 0 ? (
              <p className="cubo-vazio">Nenhuma coluna com «{buscaCol}».</p>
            ) : (
              <div className="colpick">
                {colunasVisiveis.map((c) => {
                  const on = dims[c.nome] !== undefined;
                  const fora = colsOk && !noSnapshot.has(c.nome);
                  return (
                    <span
                      key={c.nome}
                      className={"colchip" + (on ? " on" : "") + (fora ? " cubo-fora" : "")}
                    >
                      {/* Botão de verdade: como <span onClick> a marcação era inalcançável por
                          teclado e o leitor de tela não anunciava o estado. */}
                      <button
                        type="button"
                        className="cubo-mk"
                        aria-pressed={on}
                        title={fora ? "coluna não encontrada no snapshot atual" : undefined}
                        onClick={() => toggleDim(c.nome)}
                      >
                        <span className="mk" aria-hidden="true" />
                        {c.nome}
                      </button>
                      {(fora || c.tipo !== "?") && (
                        <span className="ty">{fora ? "fora do snapshot" : c.tipo}</span>
                      )}
                      {on && (
                        <>
                          <span aria-hidden="true">→</span>
                          {/* `--ink` em vez do `--navy` de `.colchip .rn`: no tema escuro o navy
                              vira #5b8bd0 e o nome amigável — que é o rótulo que o operador vai
                              ver — ficava em 4,24:1 (medido, mínimo 4,5). O sublinhado tracejado
                              continua marcando que o campo é editável. */}
                          <input
                            className="rn"
                            style={{ color: "var(--ink)" }}
                            value={dims[c.nome]}
                            aria-label={`Nome amigável da dimensão ${c.nome}`}
                            onChange={(e) => renameDim(c.nome, e.target.value)}
                          />
                        </>
                      )}
                    </span>
                  );
                })}
              </div>
            )}
            <p className="cubo-nota" style={{ margin: "10px 0 0" }}>
              Marque a coluna para virar dimensão. O nome amigável (→) é o que o operador vê no
              relatório — e é por ele que view e página referenciam a dimensão.
            </p>
          </div>
        </div>

        {/* ---------- Passo 3 — Medidas ---------- */}
        <div className="cstep">
          <div className="ch">
            <span className="n" style={{ color: TINTA_NO_LIME }}>3</span>
            <h3>Medidas</h3>
            <span className="hint">os números — escolha a conta, sem SQL</span>
          </div>
          <div className="cb">
            {meds.length === 0 ? (
              <p className="cubo-vazio">
                Nenhuma medida ainda. Todo número exibido no produto nasce de uma medida nomeada
                daqui — sem medida, o cubo não pode ser publicado.
              </p>
            ) : (
              <>
                <div className="medhead" aria-hidden="true">
                  <span>Nome</span>
                  <span>Operação</span>
                  <span>Coluna</span>
                  <span>Formato</span>
                  <span />
                </div>
                <div className="medlist">
                  {meds.map((m, i) => {
                    const nomeVazio = !m.nome.trim();
                    const repetido =
                      !!m.nome.trim() &&
                      nomesMedidas.filter((n) => n === m.nome.trim()).length > 1;
                    const semCol = m.agg !== "contagem" && !m.col;
                    const foraDoSnap = colsOk && !!m.col && !noSnapshot.has(m.col);
                    // Soma/Média sobre coluna que a amostra mostrou como texto/booleano: o DuckDB
                    // não tem sum(VARCHAR) — a consulta do cubo falha. Aviso, não bloqueio: o tipo
                    // vem de inferência sobre 5 linhas, e quem edita pode saber mais que a amostra.
                    const tipoIncompativel =
                      (m.agg === "soma" || m.agg === "media") &&
                      !!m.col &&
                      (tipoDaColuna.get(m.col) === "texto" ||
                        tipoDaColuna.get(m.col) === "booleano");
                    const rot = m.nome.trim() || `medida ${i + 1}`;
                    return (
                      <div className="medrow" key={i}>
                        <input
                          value={m.nome}
                          aria-label={`Nome da medida ${i + 1}`}
                          aria-invalid={nomeVazio || repetido}
                          placeholder="ex.: Total recebido"
                          onChange={(e) => setMed(i, "nome", e.target.value)}
                        />
                        <select
                          value={m.agg}
                          aria-label={`Operação da medida ${rot}`}
                          onChange={(e) => setMed(i, "agg", e.target.value)}
                        >
                          {AGG.map(([k, l]) => (
                            <option key={k} value={k}>
                              {l}
                            </option>
                          ))}
                        </select>
                        <select
                          value={m.col}
                          aria-label={`Coluna da medida ${rot}`}
                          aria-invalid={semCol}
                          onChange={(e) => setMed(i, "col", e.target.value)}
                          disabled={m.agg === "contagem"}
                        >
                          {m.agg === "contagem" ? (
                            <option value={m.col}>todas as linhas</option>
                          ) : (
                            <>
                              <option value="">— escolha a coluna —</option>
                              {universo.map((c) => (
                                <option key={c.nome} value={c.nome}>
                                  {c.nome}
                                  {colsOk && !noSnapshot.has(c.nome) ? " (fora do snapshot)" : ""}
                                </option>
                              ))}
                            </>
                          )}
                        </select>
                        <select
                          value={m.fmt}
                          aria-label={`Formato da medida ${rot}`}
                          onChange={(e) => setMed(i, "fmt", e.target.value)}
                        >
                          {FORMATOS.map(([v, l]) => (
                            <option key={v} value={v}>
                              {l}
                            </option>
                          ))}
                        </select>
                        {/* 32×32: o alvo de 26px do × ficava rente ao mínimo de 24 do WCAG 2.5.8
                            para uma remoção sem desfazer, encostada no select de Formato. */}
                        <button
                          type="button"
                          className="del"
                          style={{ width: 32, height: 32, fontSize: 16 }}
                          aria-label={`Remover a medida ${rot}`}
                          title={`Remover a medida ${rot}`}
                          onClick={() => delMed(i)}
                        >
                          ×
                        </button>
                        <span className="expr" style={{ gridColumn: "1 / -1" }}>
                          gera: <b style={{ color: "var(--ink-2)" }}>{cexprExib(m.agg, m.col)}</b>
                        </span>
                        {(nomeVazio || repetido || semCol || foraDoSnap || tipoIncompativel) && (
                          <span className="cubo-erro-inline" style={{ gridColumn: "1 / -1" }}>
                            {nomeVazio
                              ? "Dê um nome à medida — é por ele que view e página a referenciam."
                              : repetido
                                ? "Nome repetido: cada medida do cubo precisa de um nome único."
                                : semCol
                                  ? "Escolha a coluna desta medida."
                                  : foraDoSnap
                                    ? `A coluna «${m.col}» não existe no snapshot atual.`
                                    : `A amostra do snapshot mostrou «${m.col}» como ${tipoDaColuna.get(m.col)} — ${
                                        m.agg === "soma" ? "Soma" : "Média"
                                      } sobre ela faz a consulta do cubo falhar. Use Contagem, ou uma coluna numérica.`}
                          </span>
                        )}
                      </div>
                    );
                  })}
                </div>
              </>
            )}
            <button type="button" className="addmed" onClick={addMed}>
              + Nova medida
            </button>
          </div>
        </div>

        {/* ---------- Prévia ---------- */}
        <div className="card pivot-wrap">
          <div className="card-h">
            {/* nowrap: com a etiqueta longa ao lado, "Prévia do cubo" quebrava em três linhas. */}
            <h3 style={{ whiteSpace: "nowrap" }}>Prévia do cubo</h3>
            {previaVelha && <span className="ver-aviso">prévia desatualizada</span>}
            <span className="tag junto" title={`${porDims.titulo} · ${porMeds.titulo}`}>
              {dimsMarcadas === 0 ? "sem dimensão" : `por ${porDims.texto}`} ·{" "}
              {nomesMedidas.length ? porMeds.texto : "sem medida"}
            </span>
            <span className="cubo-nota" style={{ marginLeft: "auto" }}>
              {temPublicada
                ? `agrega o snapshot pela definição publicada${versaoAtiva ? ` (v${versaoAtiva})` : ""}`
                : "o cubo ainda não foi publicado"}
            </span>
          </div>

          {/* Um aviso só. As duas ressalvas quase sempre valem juntas (editar deixa a tela suja E
              envelhece a prévia) e empilhar dois retângulos âmbar dizendo coisas parecidas fazia o
              leitor pular os dois. */}
          {(previaVelha || (sujo && temPublicada)) && (
            <div className="exec-aviso" role="status" style={{ margin: "12px 16px 0" }}>
              {previaVelha && (
                <>
                  Estes números vêm de uma consulta <b>anterior</b> às últimas mudanças da tela.{" "}
                </>
              )}
              {sujo && temPublicada && (
                <>
                  A consulta usa a definição <b>publicada</b>
                  {versaoAtiva ? ` (v${versaoAtiva})` : ""}, não o que está na tela.{" "}
                </>
              )}
              {previaVelha && (
                <>
                  Clique em <b>Pré-visualizar</b> para consultar de novo.
                </>
              )}
            </div>
          )}

          {erroPrevia ? (
            <div className="cubo-previa-estado cubo-erro-inline" role="alert">
              {erroPrevia}
            </div>
          ) : carregandoPrevia ? (
            <div className="cubo-previa-estado" role="status">
              Consultando o snapshot…
            </div>
          ) : !previa ? (
            <div className="cubo-previa-estado">
              {temPublicada
                ? "Clique em Pré-visualizar para agregar o snapshot da última execução."
                : "Publique o cubo para pré-visualizar — a consulta usa a definição publicada."}
            </div>
          ) : previa.linhas.length === 0 ? (
            <div className="cubo-previa-estado">
              A consulta funcionou e não devolveu nenhuma linha — o snapshot da execução usada está
              vazio (ou o filtro do pipeline não trouxe dado). Não é erro de definição.
            </div>
          ) : (
            <>
              {/* Scrollport PRÓPRIO da grade (o card inteiro não pode ser o scroller: o rodapé
                  "N linhas agregadas" sairia de vista junto). É ele que faz o `position:sticky` do
                  `thead th` valer — sem altura máxima, o cabeçalho da grade sumia com a rolagem da
                  página e um cubo de 20+ dimensões ficava impossível de conferir. */}
              <div style={{ overflow: "auto", maxHeight: "min(62vh, 620px)" }}>
                {/* Prévia envelhecida é marcada por ROTULO (pílula no cabeçalho do card + aviso +
                    rodapé), não por opacidade: esmaecer a grade levava o cabeçalho de coluna a
                    2,3:1 (medido) — a marca de "não é atual" não pode custar a legibilidade do
                    dado que ainda é informação. E o scrollport próprio da grade mantém o aviso
                    visível enquanto se rola o dado. */}
                <table className="pivot" data-testid="cubo-previa">
                  <thead>
                    <tr>
                      {previa.colunas.map((c) => {
                        const ehMed = previa.medidas[c] !== undefined;
                        return (
                          <th
                            scope="col"
                            key={c}
                            className={ehMed ? "num" : undefined}
                            // Dimensão é rótulo, não número: `table.pivot` alinha tudo à direita e
                            // só a 1ª coluna escapava (o `table.bmini` do Portal já foi corrigido
                            // assim). Nome de cliente encostado na direita não se lê em coluna.
                            style={{ textAlign: ehMed ? "right" : "left" }}
                          >
                            {c}
                          </th>
                        );
                      })}
                    </tr>
                  </thead>
                  <tbody>
                    {previa.linhas.map((r, i) => (
                      <tr key={i}>
                        {previa.colunas.map((c) => {
                          // Medida/formato da definição CONSULTADA (ver PrevResultado.medidas).
                          const fmt = previa.medidas[c];
                          const cel =
                            fmt !== undefined ? celulaMedida(r[c], fmt) : celulaDim(r[c]);
                          return (
                            <td
                              key={c}
                              className={
                                (fmt !== undefined ? "num" : "") + (cel.vazia ? " cel-vazia" : "")
                              }
                              style={{ textAlign: fmt !== undefined ? "right" : "left" }}
                              title={cel.titulo}
                            >
                              {cel.texto}
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="cubo-previa-estado cubo-nota">
                {plural(previa.linhas.length, "linha", "linhas")} agregadas
                {previa.execucaoId && (
                  <> · snapshot da execução {previa.execucaoId.slice(0, 8)}</>
                )}
                {previaVelha && <> · consulta anterior às mudanças da tela</>}
              </div>
            </>
          )}
        </div>

        {/* ---------- Ações ---------- */}
        <div
          style={{
            marginTop: 13,
            display: "flex",
            gap: 10,
            alignItems: "center",
            flexWrap: "wrap",
          }}
        >
          <button
            type="button"
            className="btn-stamp"
            data-testid="publicar-cubo"
            disabled={!podeGravar}
            title={pendencias.length ? "Resolva as pendências abaixo" : undefined}
            onClick={() => setConfirmando("publicar")}
          >
            <Icon name="stamp" cls="ic" /> Publicar cubo
          </button>
          <button
            type="button"
            className="addmed"
            style={{ marginTop: 0 }}
            disabled={!podeGravar}
            onClick={() => void salvarRascunho()}
          >
            {salvando ? "Salvando…" : "Salvar rascunho"}
          </button>
          <button
            type="button"
            className="addmed"
            style={{ marginTop: 0 }}
            disabled={carregandoPrevia || !temPublicada}
            title={temPublicada ? undefined : "Disponível depois da primeira publicação"}
            onClick={() => void prever()}
          >
            {carregandoPrevia ? "Consultando…" : "Pré-visualizar"}
          </button>
          <span className="cubo-nota">
            {plural(dimsMarcadas, "dimensão", "dimensões")} ·{" "}
            {plural(meds.length, "medida", "medidas")} · vira insumo das views
          </span>
        </div>

        {pendencias.length > 0 && (
          <div className="exec-aviso" style={{ marginTop: 12 }} data-testid="cubo-pendencias">
            <b>Falta resolver antes de salvar ou publicar:</b>
            <ul className="cubo-pend-lista">
              {pendencias.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          </div>
        )}

        {/* Resultado das ações num só lugar, anunciado ao leitor de tela. */}
        <div role="status" aria-live="polite">
          {aviso && (
            <div className="cubo-nota" style={{ marginTop: 9 }}>
              {aviso}
            </div>
          )}
          {publicado !== null && (
            <div className="run-banner" data-testid="cubo-publicado">
              <span className="dot" />
              <span>
                <b>Cubo publicado — versão v{publicado}</b> · as medidas nomeadas já estão
                disponíveis no Construtor de view. A publicação ficou registrada na trilha de
                auditoria.
              </span>
            </div>
          )}
        </div>
      </div>
    </>
  );
}

// Assinatura da definição editável — usada só para saber se há mudança não salva.
function assinar(
  pipe: string,
  no: string,
  dims: Record<string, string>,
  meds: Medida[],
): string {
  return JSON.stringify([
    pipe,
    no,
    Object.entries(dims).sort(([a], [b]) => a.localeCompare(b)),
    meds.map((m) => [m.nome.trim(), m.agg, m.col, m.fmt]),
  ]);
}

// Recupera agg/col da expressão salva (ida-e-volta do JSON versionado).
function parseMedida(m: any): Medida {
  const expr: string = String(m.expressao ?? "");
  const fmt = m.formato ?? "Número";
  let agg = "soma";
  let col = "";
  if (expr === "count(*)") {
    agg = "contagem";
  } else {
    const md = expr.match(/^count\(distinct\s+(.+)\)$/);
    if (md) {
      agg = "unica";
      col = unqid(md[1]);
    } else {
      const fm = expr.match(/^(sum|avg|min|max)\((.+)\)$/);
      if (fm) {
        agg = { sum: "soma", avg: "media", min: "min", max: "max" }[fm[1]] ?? "soma";
        col = unqid(fm[2]);
      }
    }
  }
  return { nome: m.nome ?? "medida", agg, col, fmt };
}
