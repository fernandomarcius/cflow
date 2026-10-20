import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";

import * as api from "../../api/client";
import { usePollVisivel } from "../poll";
import type { CorridaMapa as Corrida, ExecucaoDetalhe, ExecucaoItem } from "../../api/client";
import { rota } from "../../prefixo";
import ExecGrafo, { cruzarNos, ordemDeLeitura, type DefinicaoGrafo } from "../execucao/ExecGrafo";
import TrilhaNos from "../execucao/TrilhaNos";
import {
  fmtDuracao,
  fmtDuracaoMs,
  fmtMomento,
  pillExecucao,
  rotuloOrigem,
  type EstadoNo,
} from "../execucao/formato";
import { janelaCorrida, tempoNaCorrida } from "./cadeias";
import {
  alvoInicial,
  corridaEmAndamento,
  desenhoDaCorrida,
  estadoNaCorrida,
  grupoDaCorrida,
  nosDaCorrida,
  raizDaCorrida,
  realceDoFoco,
  reduzirArestas,
  resumoEstados,
  rotuloEstadoNaCorrida,
  temArestaPublicado,
  vizinhosNaCorrida,
  type VizinhoCorrida,
} from "./mapaCorrida";

// ─────────────────────────────────────────────────────────────────────────────
// Mapa da CORRIDA: cada pipeline de um disparo encadeado é um nó, e as arestas dizem quem esperou
// quem. É a visão macro do que o detalhe de execução mostra por dentro de UM pipeline — o mesmo
// grafo read-only (`ExecGrafo`), com o pipeline selecionado aberto embaixo.
//
// Complementa a lista, não a substitui: o acordeão continua dizendo quem rodou; o mapa diz em que
// ordem e por quê. Rota própria (`/corridas/:cadeiaId`) e seleção na querystring (`?sel=`), para o
// link abrir no mesmo pipeline e sobreviver ao recarregar.
//
// Corrida grande vira novelo (38 pipelines, 116 ligações, dezenas saindo de
// poucos pipelines de carga). Por isso a tela, por padrão, agrupa as colunas por FAMÍLIA, oculta a ligação
// REDUNDANTE e DESTACA o caminho do pipeline selecionado — os três com volta a um clique.
// ─────────────────────────────────────────────────────────────────────────────

// 5 s, não 2 s: cada pedido custa uma ida e volta pela rede, e um pipeline
// da corrida leva minutos — a cada 2 s o mapa pedia mais do que mudava. Mesma cadência da lista de
// Execuções. Com a aba oculta o poll pausa (ver `usePollVisivel`).
const POLL_MS = 5000;
const ms = (iso: string) => new Date(iso).getTime();

const LEGENDA: { estado: EstadoNo; cls: string; rotulo: string; dica?: string }[] = [
  { estado: "ok", cls: "ok", rotulo: "concluída" },
  { estado: "falha", cls: "falha", rotulo: "falha" },
  { estado: "executando", cls: "exec", rotulo: "em execução" },
  { estado: "pendente", cls: "off", rotulo: "aguardando", dica: "esperando as dependências terminarem" },
  {
    estado: "skip",
    cls: "skip",
    rotulo: "não rodou",
    dica: "a corrida parou antes de chegar neste pipeline",
  },
];

export default function CorridaMapa() {
  const { cadeiaId = "" } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  // A lista manda os filtros vigentes em `state.volta`, como faz com o detalhe de execução.
  const voltaSalva = (location.state as { volta?: string } | null)?.volta ?? null;
  const [params, setParams] = useSearchParams();
  const sel = params.get("sel");

  const [corrida, setCorrida] = useState<Corrida | null>(null);
  const [carregando, setCarregando] = useState(true);
  const [erro, setErro] = useState<string | null>(null);
  // Quantas cargas já chegaram: o painel recarrega os nós do pipeline a cada uma enquanto ele roda.
  const [cargas, setCargas] = useState(0);
  // Última chamada vence: um poll lento não pode sobrescrever a resposta de um mais novo.
  const serie = useRef(0);

  // Como desenhar. Escolhas de quem olha, não da corrida — por isso estado local, como o
  // alternador Grafo/Lista do detalhe.
  const [modo, setModo] = useState<"familia" | "dependencia">("familia");
  const [todasLigacoes, setTodasLigacoes] = useState(false);
  const [destacar, setDestacar] = useState(true);

  const carregar = useCallback(
    async (silencioso = false) => {
      const n = ++serie.current;
      if (!silencioso) setCarregando(true);
      try {
        const c = await api.obterCorrida(cadeiaId);
        if (n !== serie.current) return;
        setCorrida(c);
        setCargas((k) => k + 1);
        setErro(null);
      } catch (e: any) {
        if (n !== serie.current) return;
        // Falha num poll não apaga o mapa que está na tela; o próximo tenta de novo.
        if (!silencioso) setErro(e?.message ?? "Falha ao abrir a corrida.");
      } finally {
        if (n === serie.current && !silencioso) setCarregando(false);
      }
    },
    [cadeiaId],
  );

  const autoSelFeita = useRef(false);
  useEffect(() => {
    autoSelFeita.current = false;
    setCorrida(null);
    void carregar();
  }, [carregar]);

  const rodando = corrida ? corridaEmAndamento(corrida) : false;
  const atualizarEmSilencio = useCallback(() => void carregar(true), [carregar]);
  usePollVisivel(rodando, atualizarEmSilencio, POLL_MS);

  // Tique de 1s só enquanto roda: a duração da corrida e o "rodando há" do painel andam sozinhos.
  const [agora, setAgora] = useState(() => Date.now());
  useEffect(() => {
    if (!rodando) return;
    setAgora(Date.now());
    const t = window.setInterval(() => setAgora(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [rodando]);

  // Ligações desenhadas: as essenciais (sem a redundante) ou todas. O painel sempre usa todas.
  const essenciais = useMemo(() => (corrida ? reduzirArestas(corrida.arestas) : []), [corrida]);
  const redundantes = corrida ? corrida.arestas.length - essenciais.length : 0;
  const nos = useMemo(
    () =>
      corrida
        ? nosDaCorrida({ ...corrida, arestas: todasLigacoes ? corrida.arestas : essenciais })
        : [],
    [corrida, essenciais, todasLigacoes],
  );
  // O layout depende só da ESTRUTURA (quem liga em quem) e do modo — o poll troca status e tempos a
  // cada 2 s, e recalcular posição a cada um faria o mapa tremer (e custaria o layout de novo).
  const estrutura = nos.map((n) => `${n.id}<${n.de.join(",")}`).join("|");
  const desenho = useMemo(
    () => desenhoDaCorrida(nos, corrida?.raiz_id ?? null, modo),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [estrutura, modo],
  );
  const realce = useMemo(() => (destacar ? realceDoFoco(nos, sel) : null), [destacar, sel, nos]);

  // Seleção na URL, com `replace`: escolher pipeline não empilha histórico. O `state` vai junto —
  // sem ele o "← Execuções" perderia os filtros que a lista mandou.
  const selecionar = useCallback(
    (id: string) => {
      const q = new URLSearchParams(params);
      q.set("sel", id);
      setParams(q, { replace: true, state: location.state });
    },
    [params, setParams, location.state],
  );

  const painelRef = useRef<HTMLDivElement | null>(null);
  const [focoPedido, setFocoPedido] = useState(0);
  // Escolha EXPLÍCITA (clique, teclado): seleciona e leva o painel à vista — em tela de 1180px ele
  // fica abaixo da dobra, e a seleção pareceria não ter efeito (mesma regra do detalhe).
  const escolher = useCallback(
    (id: string) => {
      selecionar(id);
      setFocoPedido((n) => n + 1);
      const suave = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      window.setTimeout(
        () =>
          painelRef.current?.scrollIntoView({ block: "nearest", behavior: suave ? "smooth" : "auto" }),
        60,
      );
    },
    [selecionar],
  );

  // Ao abrir: mantém o `?sel=` do link se ele é desta corrida; senão o que falhou, o que roda ou a
  // raiz. Uma vez por corrida — durante o poll a seleção não pode ficar pulando.
  useEffect(() => {
    if (!corrida || autoSelFeita.current) return;
    autoSelFeita.current = true;
    if (sel && corrida.execucoes.some((e) => e.id === sel)) return;
    const alvo = alvoInicial(corrida);
    if (!alvo) return;
    selecionar(alvo);
    // Falha é a exceção em que ir até o nó É o que se quer ver (mesma regra do detalhe).
    if (nos.find((n) => n.id === alvo)?.estado === "falha") setFocoPedido((n) => n + 1);
  }, [corrida, sel, selecionar, nos]);

  const voltar = () => navigate(voltaSalva != null ? `/execucoes?${voltaSalva}` : "/execucoes");
  const abrirExecucao = (id: string) => navigate(`/execucoes/${id}`);

  if (carregando && !corrida) {
    return (
      <div className="pad">
        <div className="card exec-painel-vazio">Carregando corrida…</div>
      </div>
    );
  }

  const grupo = corrida ? grupoDaCorrida(corrida) : null;
  if (erro || !corrida || !grupo) {
    return (
      <>
        <div className="page-head">
          <button className="link-back" data-testid="corrida-voltar" onClick={voltar}>
            ← Execuções
          </button>
          <h1 className="display">Corrida</h1>
        </div>
        <div className="pad">
          <div className="card exec-erro-lista" role="alert">
            {erro ?? "Corrida não encontrada."}
          </div>
        </div>
      </>
    );
  }

  const raiz = raizDaCorrida(corrida);
  const janela = janelaCorrida(grupo);
  const estadosPresentes = new Set(nos.map((n) => n.estado));
  const selecionada = corrida.execucoes.find((e) => e.id === sel) ?? null;
  const total = corrida.execucoes.length;
  const disparo = grupo.raiz.pagina_nome
    ? `página · ${grupo.raiz.pagina_nome}`
    : rotuloOrigem(grupo.raiz.origem, grupo.raiz.agendamento_nome);

  return (
    <>
      <div className="page-head">
        <button className="link-back" data-testid="corrida-voltar" onClick={voltar}>
          ← Execuções
        </button>
        <h1 className="display">{raiz?.pipeline_nome ?? "Corrida"}</h1>
        <p className="sub">
          corrida de {total} pipeline{total === 1 ? "" : "s"} · o mapa é montado a partir das versões
          que rodaram
        </p>
        <div className="run-meta">
          {raiz && (
            <div className="meta">
              <span className="k">Status da raiz</span>
              <span className={"statuspill " + pillExecucao(raiz.status).cls} data-testid="corrida-status">
                <span className="d" />
                {pillExecucao(raiz.status).label}
              </span>
            </div>
          )}
          <div className="meta">
            <span className="k">Disparo</span>
            <span className="v">{disparo}</span>
          </div>
          {grupo.raiz.ator_nome && (
            <div className="meta">
              <span className="k">Usuário</span>
              <span className="v">{grupo.raiz.ator_nome}</span>
            </div>
          )}
          <div className="meta">
            <span className="k">Início</span>
            <span className="v">{fmtMomento(janela.inicio)}</span>
          </div>
          <div className="meta">
            <span className="k">Duração</span>
            <span
              className="v num"
              data-testid="corrida-duracao"
              title="Do disparo ao fim do último pipeline da corrida"
            >
              {rodando && janela.inicio
                ? fmtDuracaoMs(agora - ms(janela.inicio))
                : fmtDuracao(janela.inicio, janela.fim)}{" "}
              <span className="exec-cel-versao">corrida inteira</span>
            </span>
          </div>
          <div className="meta">
            <span className="k">Pipelines</span>
            <span className="v num" data-testid="corrida-resumo">
              {resumoEstados(corrida)}
            </span>
          </div>
        </div>
      </div>

      <div className="pad exec-detalhe">
        <div className="exec-visao-barra corrida-controles">
          {/* Alternadores com estado programático (`aria-pressed`), como o Grafo/Lista do detalhe. */}
          <div
            className="cfg-tabs exec-visao"
            role="group"
            aria-label="Como organizar as colunas do mapa"
            data-testid="corrida-modo"
          >
            <button
              type="button"
              data-testid="corrida-modo-familia"
              aria-pressed={modo === "familia"}
              className={modo === "familia" ? "on" : ""}
              title="Uma faixa por família de pipeline, pelo prefixo do nome (carga, fato, ref…)"
              onClick={() => setModo("familia")}
            >
              Por família
            </button>
            <button
              type="button"
              data-testid="corrida-modo-dependencia"
              aria-pressed={modo === "dependencia"}
              className={modo === "dependencia" ? "on" : ""}
              title="Colunas pela ordem de dependência, sem agrupar"
              onClick={() => setModo("dependencia")}
            >
              Por dependência
            </button>
          </div>
          <button
            type="button"
            className="pag-btn"
            data-testid="corrida-destacar"
            aria-pressed={destacar}
            title="Com um pipeline selecionado, acende o que ele lê e o que depende dele; o resto esmaece"
            onClick={() => setDestacar((v) => !v)}
          >
            Destacar caminho
          </button>
          {redundantes > 0 && (
            <button
              type="button"
              className="pag-btn"
              data-testid="corrida-todas-ligacoes"
              aria-pressed={todasLigacoes}
              title={
                "Ligação redundante: A → C quando A → B → C já está desenhado. Ocultá-la não esconde " +
                "dependência — o caminho continua no mapa, e a lista completa fica no painel."
              }
              onClick={() => setTodasLigacoes((v) => !v)}
            >
              {todasLigacoes
                ? `Todas as ligações (${corrida.arestas.length})`
                : `${redundantes} ligações redundantes ocultas`}
            </button>
          )}
          {/* Legenda só do que está na tela: ensinar uma cor ausente é ruído. */}
          <span className="exec-legenda">
            {LEGENDA.filter((l) => estadosPresentes.has(l.estado)).map((l) => (
              <span key={l.estado} className={"exec-legenda-item " + l.cls} title={l.dica}>
                {l.rotulo}
              </span>
            ))}
          </span>
          <span className="corrida-legenda-arestas">
            <span className="corrida-aresta">lê o resultado do pipeline</span>
            {temArestaPublicado(corrida) && (
              <span
                className="corrida-aresta pub"
                title="Lê o que o outro publica no lake; na mesma corrida, espera ele publicar"
              >
                espera a publicação no lake
              </span>
            )}
          </span>
        </div>
        {modo === "familia" && !desenho.porFamilia && (
          <p className="corrida-nota" data-testid="corrida-sem-familias">
            Nesta corrida não há famílias para agrupar (é preciso ao menos duas com dois ou mais
            pipelines) — as colunas seguem a ordem de dependência.
          </p>
        )}

        <div className="corrida-canvas">
          <ExecGrafo
            idGrafo="corrida-grafo"
            instrucaoTeclado="Use Tab para percorrer os pipelines e Enter para ver o detalhe do pipeline."
            nos={nos}
            selecionado={sel}
            onSelecionar={escolher}
            focoPedido={focoPedido}
            posicoes={desenho.posicoes}
            faixas={desenho.faixas}
            realce={realce}
          />
        </div>
        <div ref={painelRef}>
          {selecionada ? (
            <PainelCorrida
              e={selecionada}
              ehRaiz={selecionada.id === corrida.raiz_id}
              agora={agora}
              cargas={cargas}
              vizinhos={vizinhosNaCorrida(corrida, selecionada.id)}
              onAbrir={abrirExecucao}
              onEscolher={escolher}
            />
          ) : (
            <div className="card exec-painel-vazio corrida-painel">
              Selecione um pipeline no mapa para ver o detalhe dele.
            </div>
          )}
        </div>
      </div>
    </>
  );
}

// O pipeline selecionado: como terminou, quanto rodou (sem a fila), o erro, de quem ele depende e
// quem depende dele (pelas ligações COMPLETAS — o mapa pode estar mostrando só as essenciais) e os
// NÓS dele — o mesmo `TrilhaNos` do detalhe, cruzado com a definição da versão que executou. Para
// ver o dado de um nó, "Abrir execução" leva ao detalhe; o painel é resumo, não uma segunda tela.
function PainelCorrida({
  e,
  ehRaiz,
  agora,
  cargas,
  vizinhos,
  onAbrir,
  onEscolher,
}: {
  e: ExecucaoItem;
  ehRaiz: boolean;
  agora: number;
  cargas: number;
  vizinhos: { depende: VizinhoCorrida[]; usadoPor: VizinhoCorrida[] };
  onAbrir: (id: string) => void;
  onEscolher: (id: string) => void;
}) {
  const [detalhe, setDetalhe] = useState<ExecucaoDetalhe | null>(null);
  const [definicao, setDefinicao] = useState<DefinicaoGrafo | null>(null);
  const [erroNos, setErroNos] = useState<string | null>(null);
  const rodando = e.status === "executando";

  // Definição da VERSÃO EXECUTADA, uma vez por execução. Sem ela (versão indisponível ou sem
  // permissão), a lista mostra só os nós que rodaram — o `cruzarNos` já sabe fazer isso.
  useEffect(() => {
    let vivo = true;
    setDetalhe(null);
    setDefinicao(null);
    setErroNos(null);
    api
      .obterPipeline(e.pipeline_id, e.versao)
      .then((p) => {
        if (vivo) setDefinicao(p.definicao);
      })
      .catch(() => {});
    return () => {
      vivo = false;
    };
  }, [e.id, e.pipeline_id, e.versao]);

  // Nós executados: na abertura, a cada poll do mapa enquanto o pipeline roda, e uma última vez
  // quando o status muda (é o fim que pinta o último nó).
  useEffect(() => {
    let vivo = true;
    api
      .consultarExecucao(e.id)
      .then((d) => {
        if (vivo) {
          setDetalhe(d);
          setErroNos(null);
        }
      })
      .catch((err: any) => {
        if (vivo) setErroNos(err?.message ?? "Falha ao carregar os nós do pipeline.");
      });
    return () => {
      vivo = false;
    };
  }, [e.id, e.status, rodando ? cargas : -1]);

  const nosPipeline = useMemo(
    () =>
      detalhe ? ordemDeLeitura(cruzarNos(definicao, detalhe.nos, detalhe.status === "executando")) : [],
    [detalhe, definicao],
  );

  const estado = estadoNaCorrida(e);
  const pill = rotuloEstadoNaCorrida(estado);
  const t = tempoNaCorrida(e);
  const tempo =
    t.tipo === "rodou"
      ? [
          `começou ${fmtMomento(t.inicio)}`,
          t.fim ? `rodou ${fmtDuracao(t.inicio, t.fim)}` : `rodando há ${fmtDuracaoMs(agora - ms(t.inicio))}`,
          t.esperaMs != null ? `esperou ${fmtDuracaoMs(t.esperaMs)} na fila das dependências` : null,
        ]
          .filter(Boolean)
          .join(" · ")
      : t.tipo === "aguardando"
        ? "aguardando as dependências terminarem"
        : "nenhum nó começou — a corrida parou antes de chegar neste pipeline";

  const listaVizinhos = (lista: VizinhoCorrida[], vazio: string, testid: string, rotulo: string) => (
    <div className="corrida-vizinhos" data-testid={testid}>
      <span className="eyebrow">{rotulo}</span>
      {lista.length ? (
        lista.map((v) => (
          <button
            key={v.id}
            type="button"
            className="chip corrida-vizinho"
            title={
              v.tipo === "publicado"
                ? "Ligação pelo lake (leitura publicado). Clique para selecionar."
                : "Ligação direta (fonte pipeline). Clique para selecionar."
            }
            onClick={() => onEscolher(v.id)}
          >
            {v.nome}
            {v.tipo === "publicado" && <span className="corrida-vizinho-pub"> · lake</span>}
          </button>
        ))
      ) : (
        <span className="exec-cel-versao">{vazio}</span>
      )}
    </div>
  );

  return (
    <section
      className="card corrida-painel"
      data-testid="corrida-painel"
      aria-label={`Pipeline selecionado: ${e.pipeline_nome}`}
    >
      <div className="corrida-painel-h">
        <div className="corrida-painel-id">
          <span className="eyebrow">Pipeline selecionado{ehRaiz ? " · raiz da corrida" : ""}</span>
          <h2 className="corrida-painel-nome" data-testid="corrida-painel-nome">
            {e.pipeline_nome}
          </h2>
          <span className="exec-cel-versao">
            execução <span className="exec-cel-mono">{e.id.slice(0, 8)}</span> · versão {e.versao}
          </span>
        </div>
        <span className={"statuspill " + pill.cls} data-testid="corrida-painel-status">
          <span className="d" />
          {pill.label}
        </span>
        {/* <a> de verdade: cmd-clique abre a execução em outra aba sem perder o mapa. */}
        <a
          className="pag-btn corrida-abrir"
          href={rota(`/execucoes/${e.id}`)}
          data-testid="corrida-abrir-execucao"
          onClick={(ev) => {
            if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button !== 0) return;
            ev.preventDefault();
            onAbrir(e.id);
          }}
        >
          Abrir execução →
        </a>
      </div>
      <p className="corrida-painel-tempo" data-testid="corrida-painel-tempo">
        {tempo}
      </p>
      {e.erro && estado !== "ok" && (
        <div className="exec-banner-falha" role="alert">
          <span className="dot" />
          <span className="exec-banner-txt">{e.erro}</span>
        </div>
      )}
      {listaVizinhos(vizinhos.depende, "nada — começa a corrida", "corrida-depende", "Depende de")}
      {listaVizinhos(vizinhos.usadoPor, "ninguém nesta corrida", "corrida-usado-por", "Usado por")}
      <span className="eyebrow">Nós deste pipeline · clique para abrir a execução</span>
      {erroNos ? (
        <div className="exec-aviso">{erroNos}</div>
      ) : !detalhe ? (
        <div className="exec-painel-vazio">Carregando os nós…</div>
      ) : (
        <TrilhaNos nos={nosPipeline} selecionado={null} onSelecionar={() => onAbrir(e.id)} />
      )}
    </section>
  );
}
