import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import * as api from "../../api/client";
import { usePollVisivel } from "../poll";
import type { ExecucaoItem, StatusExecucao } from "../../api/client";
import { rota } from "../../prefixo";
import { SeletorPeriodo } from "../SeletorPeriodo";
import ConfirmarReexecucao from "../execucao/ConfirmarReexecucao";
import { resolverJanela } from "../periodos";
import { AbasFiltro, MenuAcoes } from "../ui";
import {
  ABAS_CAMADA,
  abaDaApi,
  naAba,
  type AbaCamada,
} from "./camadas";
import {
  abertasDaUrl,
  abertasParaUrl,
  alternar,
  composicaoCorrida,
  corridaTemFalha,
  ehCorridaUnica,
  filhasVisiveis,
  janelaCorrida,
  raizEhContexto,
  resumoCorrida,
  rotuloComposicao,
  tempoNaCorrida,
} from "./cadeias";
import type { ComposicaoCorrida, TempoNaCorrida } from "./cadeias";
import {
  diaSP,
  fmtDuracao,
  fmtDuracaoMs,
  fmtHora,
  fmtMomento,
  fmtMomentoCurto,
  num,
  pillExecucao,
  rotuloDia,
  rotuloOrigem,
} from "../execucao/formato";

// ─────────────────────────────────────────────────────────────────────────
// Execuções — histórico de TODOS os pipelines (GET /execucoes), filtrável.
//
// O default é "todos os pipelines": quem abre esta tela normalmente quer saber "o que rodou hoje
// e o que falhou", não navegar pipeline por pipeline. Filtros combináveis (pipeline, status,
// origem, período) ficam na URL, então a visão é linkável e sobrevive ao voltar do detalhe.
//
// O drill mora em `/execucoes/:id` (ExecucaoDetalhe) — rota própria, não estado local.
// ─────────────────────────────────────────────────────────────────────────

const EXEC_POR_PAGINA = 25;
const BUSCA_ESPERA_MS = 350; // debounce da busca livre: uma requisição por pausa, não por tecla

// Régua de abas: "Todas" (sem recorte) + as abas da listagem de pipelines, na MESMA ordem e com o
// MESMO contrato (`camadas.ts`). Aqui "Todas" é o padrão porque a pergunta da tela é "o que rodou",
// e esconder por default o que tem camada esconderia a maior parte das execuções.
const ABAS_EXECUCAO: { chave: AbaCamada | ""; rotulo: string }[] = [
  { chave: "", rotulo: "Todas" },
  ...ABAS_CAMADA,
];
function abaDaUrl(v: string | null): AbaCamada | "" {
  return v && ABAS_CAMADA.some((a) => a.chave === v) ? (v as AbaCamada) : "";
}
const POLL_MS = 5000; // só liga quando há execução em andamento na página corrente

const STATUS_CHIPS: { valor: StatusExecucao | ""; rotulo: string }[] = [
  { valor: "", rotulo: "Todas" },
  { valor: "concluida", rotulo: "Concluídas" },
  { valor: "falha", rotulo: "Falhas" },
  { valor: "executando", rotulo: "Em execução" },
];

// Janela do período → `de`/`ate` ISO com offset de São Paulo (fim EXCLUSIVO, como resolverJanela).
// Offset fixo -03:00: o Brasil não tem mais horário de verão, e o backend normaliza momento sem
// fuso como America/Sao_Paulo de qualquer forma.
function janelaISO(token: string): { de: string; ate: string } | null {
  if (!token) return null;
  try {
    const j = resolverJanela(token);
    return { de: `${j.data_inicio}T00:00:00-03:00`, ate: `${j.data_fim}T00:00:00-03:00` };
  } catch {
    return null;
  }
}

export default function Execucoes() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();

  const [pipelines, setPipelines] = useState<api.PipelineResumo[]>([]);
  // GRUPOS (corridas), não execuções: o servidor pagina por corrida para não partir uma cadeia
  // entre duas páginas. Execução solta vem como grupo de uma só.
  const [grupos, setGrupos] = useState<api.GrupoExecucoes[]>([]);
  // Confirmação de reexecução aberta (id da execução alvo). `null` = fechada.
  const [reexecId, setReexecId] = useState<string | null>(null);
  const [total, setTotal] = useState(0);
  const [carregando, setCarregando] = useState(false);
  const [erro, setErro] = useState<string | null>(null);
  const [atualizadoEm, setAtualizadoEm] = useState<string | null>(null);
  // Contador de cargas EXPLÍCITAS (abrir a tela, trocar filtro, botão Atualizar) — recalcula o
  // resumo de HOJE (janela própria, fora do filtro) sem que o poll de 5s multiplique requisições.
  const [cargas, setCargas] = useState(0);
  const serie = useRef(0);

  // Filtros vivem na URL (fonte única) — voltar do detalhe restaura a visão sem estado extra.
  const pipeId = params.get("pipe") ?? "";
  const status = (params.get("st") ?? "") as StatusExecucao | "";
  const origem = params.get("orig") ?? "";
  const periodo = params.get("per") ?? "";
  const busca = params.get("q") ?? "";
  // Aba desconhecida na URL (link antigo, dedo torto) cai em "Todas", não numa lista vazia.
  const abaUrl = params.get("aba");
  const aba = abaDaUrl(abaUrl);
  const pagina = Math.max(1, Number(params.get("pag") ?? 1) || 1);
  const offset = (pagina - 1) * EXEC_POR_PAGINA;
  const temFiltro = !!(pipeId || status || origem || periodo || busca || aba);

  // O campo de busca tem estado PRÓPRIO (o que está sendo digitado) e só vai para a URL — e daí
  // para a API — depois de uma pausa. Com a URL como fonte direta, cada tecla era uma requisição.
  const [buscaDigitada, setBuscaDigitada] = useState(busca);
  useEffect(() => setBuscaDigitada(busca), [busca]);
  // Acordeão aberto vive na URL como todo filtro desta tela: o link compartilhado abre no mesmo
  // lugar e o voltar do navegador funciona.
  const abertas = useMemo(() => abertasDaUrl(params.get("ab")), [params]);
  const trocarAberta = useCallback(
    (chave: string) => {
      const p = new URLSearchParams(params);
      const v = abertasParaUrl(alternar(abertasDaUrl(p.get("ab")), chave));
      if (v) p.set("ab", v);
      else p.delete("ab");
      setParams(p, { replace: true });
    },
    [params, setParams],
  );

  // Deep-link legado (?exec=ID) — o Histórico de agendamentos antigo e bookmarks apontam aqui.
  useEffect(() => {
    const ex = params.get("exec");
    if (ex) navigate(`/execucoes/${ex}`, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    api
      .listarPipelines()
      .then(setPipelines)
      .catch(() => setErro("Falha ao carregar pipelines."));
  }, []);

  // Muda filtro → volta à 1ª página (senão o offset aponta para além do novo resultado).
  const trocarFiltro = useCallback(
    (chave: string, valor: string) => {
      const p = new URLSearchParams(params);
      if (valor) p.set(chave, valor);
      else p.delete(chave);
      p.delete("pag");
      setParams(p, { replace: true });
    },
    [params, setParams],
  );

  const irParaPagina = useCallback(
    (n: number) => {
      const p = new URLSearchParams(params);
      if (n > 1) p.set("pag", String(n));
      else p.delete("pag");
      setParams(p, { replace: true });
    },
    [params, setParams],
  );

  // "Última chamada vence": cada carga leva um número de série e só a mais recente pode escrever
  // no estado. Sem isso, trocar dois filtros em sequência rápida deixava a tela mostrando o
  // resultado do filtro ANTERIOR (resposta atrasada sobrescrevendo a nova).
  const carregar = useCallback(
    async (silencioso = false) => {
      const meu = ++serie.current;
      if (!silencioso) setCarregando(true);
      const janela = janelaISO(periodo);
      try {
        const r = await api.listarExecucoes({
          pipelineId: pipeId || null,
          status: status || null,
          origem: (origem as api.OrigemExecucao) || null,
          de: janela?.de ?? null,
          ate: janela?.ate ?? null,
          busca: busca || null,
          aba: abaDaApi(aba) as api.AbaExecucoes | null,
          limite: EXEC_POR_PAGINA,
          offset,
        });
        if (meu !== serie.current) return;
        setGrupos(r.itens);
        setTotal(r.total);
        setContagens(r.totais);
        setContagemAbas(r.totais_abas ?? null);
        setErro(null);
        // Só carga EXPLÍCITA reescreve o "atualizado às": o texto vive numa região aria-live, e
        // reescrevê-lo a cada poll de 5s fazia o leitor de tela anunciar um horário sem parar.
        // O horário reflete SEMPRE a última carga (inclusive a do poll) — senão a etiqueta mente
        // dizendo que o dado é de minutos atrás. O que não pode ser anunciado a cada 5s é a região
        // aria-live, que ficou só com "atualizando…" (ver o cabeçalho).
        setAtualizadoEm(new Date().toLocaleTimeString("pt-BR", { timeZone: "America/Sao_Paulo" }));
        if (!silencioso) setCargas((c) => c + 1);
      } catch (e: any) {
        // O erro de uma carga EXPLÍCITA sempre aparece, mesmo que o poll a tenha superado —
        // antes, uma explícita que falhasse enquanto superada falhava em silêncio.
        if (!silencioso) setErro(e?.message ?? "Falha ao listar execuções.");
      } finally {
        // Quem LIGOU o "carregando" tem de desligá-lo, independente do guard de série: com o guard
        // aqui, um tique do poll no meio de uma carga explícita deixava o spinner (e o botão
        // Atualizar, e a paginação) desabilitados para sempre.
        if (!silencioso) setCarregando(false);
      }
    },
    [pipeId, status, origem, periodo, busca, aba, offset],
  );

  useEffect(() => {
    void carregar();
  }, [carregar]);

  // Auto-refresh enquanto há algo rodando na página visível — ou enquanto o usuário está usando a
  // tela como monitor (filtro "Em execução"), caso em que a lista vazia também precisa se atualizar.
  // Todas as execuções visíveis da página, achatadas: é sobre elas que o poll e o separador de dia
  // trabalham. A raiz primeiro, depois as filhas que casam o filtro — a mesma ordem da tela.
  const itens = useMemo(
    () => grupos.flatMap((g) => [g.raiz, ...filhasVisiveis(g)]),
    [grupos],
  );
  const temRodando = useMemo(() => itens.some((i) => i.status === "executando"), [itens]);
  const cicloPoll = useRef(0);
  const tiquePoll = useCallback(() => {
    void carregar(true);
    // Os chips vêm com a página (mesma consulta), então já andam a cada poll. O resumo de HOJE
    // tem janela própria: recalcula a cada 6 ciclos (~30s) para não virar requisição a cada 5s.
    if (++cicloPoll.current % 6 === 0) setCargas((c) => c + 1);
  }, [carregar]);
  // Pausa com a aba oculta; ao voltar, o primeiro tique é imediato (ver `usePollVisivel`).
  usePollVisivel(temRodando || status === "executando", tiquePoll, POLL_MS);

  // Quantas falhas / quantas rodando DENTRO dos filtros atuais (ignorando o filtro de status) —
  // é o que responde "algo falhou?" sem varrer a lista. Vem junto da página (mesma transação).
  const [contagens, setContagens] = useState<{
    todas: number;
    concluida: number;
    falha: number;
    executando: number;
  } | null>(null);
  const [contagemAbas, setContagemAbas] = useState<api.PaginaExecucoes["totais_abas"]>(null);

  // Digitou e parou → a busca vai para a URL (voltando à 1ª página, como todo filtro).
  useEffect(() => {
    const alvo = buscaDigitada.trim();
    if (alvo === busca) return;
    const t = window.setTimeout(() => trocarFiltro("q", alvo), BUSCA_ESPERA_MS);
    return () => window.clearTimeout(t);
  }, [buscaDigitada, busca, trocarFiltro]);

  // Trocar de aba leva junto o pipeline escolhido só se ele pertence à aba nova — senão a tela
  // mostraria "nenhuma execução" sem dizer que o motivo é o pipeline de outra aba.
  const trocarAba = useCallback(
    (chave: string) => {
      const p = new URLSearchParams(params);
      if (chave) p.set("aba", chave);
      else p.delete("aba");
      p.delete("sub");
      const escolhido = pipelines.find((x) => x.id === p.get("pipe"));
      if (chave && escolhido && !naAba(escolhido, chave as AbaCamada)) p.delete("pipe");
      p.delete("pag");
      setParams(p, { replace: true });
    },
    [params, setParams, pipelines],
  );
  // O select de pipeline mostra só os da aba ativa (o escolhido fica, mesmo de fora, para o
  // select não aparecer vazio com um filtro valendo).
  const pipelinesDaAba = useMemo(
    () =>
      aba
        ? pipelines.filter(
            (p) =>
              naAba(p, aba) || p.id === pipeId,
          )
        : pipelines,
    [pipelines, aba, pipeId],
  );

  // Resumo de HOJE, independente do filtro de período: a pergunta nº 1 da tela é "algo falhou
  // hoje?", e o chip de falhas conta a janela filtrada (que por padrão é o histórico inteiro —
  // "484 falhas" de dois anos não responde nada e vira alarme permanente).
  const [hoje, setHoje] = useState<{ total: number; falha: number; executando: number } | null>(null);
  useEffect(() => {
    const j = janelaISO("hoje");
    if (!j) return;
    let vivo = true;
    api
      .listarExecucoes({ pipelineId: pipeId || null, de: j.de, ate: j.ate, limite: 1, offset: 0 })
      .then(
        (r) =>
          vivo &&
          setHoje(
            r.totais
              ? { total: r.totais.todas, falha: r.totais.falha, executando: r.totais.executando }
              : null,
          ),
      )
      .catch(() => vivo && setHoje(null));
    return () => {
      vivo = false;
    };
  }, [pipeId, cargas]);

  const mostrarPipeline = !pipeId; // coluna só faz sentido na visão "todos"
  // Largura do separador de dia: tem de acompanhar as colunas reais, senão a faixa do dia fica
  // curta e a tabela parece quebrada.
  const colunas = (mostrarPipeline ? 6 : 5) + 2; // + Usuário + Ações

  // Abre o detalhe levando os filtros vigentes: o "← Execuções" de lá volta para ESTA visão
  // (com status/período/disparo), não para uma lista recém-filtrada só por pipeline.
  const abrir = useCallback(
    (id: string) => navigate(`/execucoes/${id}`, { state: { volta: params.toString() } }),
    [navigate, params],
  );
  // Mapa da corrida: mesma volta com os filtros vigentes.
  const abrirMapa = useCallback(
    (cadeiaId: string) =>
      navigate(`/corridas/${cadeiaId}`, { state: { volta: params.toString() } }),
    [navigate, params],
  );

  return (
    <>
      <div className="page-head">
        <h1 className="display">Execuções</h1>
        <p className="sub">
          Tudo que rodou — manual ou agendado. Clique numa linha para ver o grafo, o dado de cada
          nó e o log.
        </p>
        {hoje && (
          <p className="exec-resumo-hoje" data-testid="exec-resumo-hoje">
            {pipeId ? "Hoje neste pipeline: " : "Hoje: "}
            <b>
              {hoje.total === 0
                ? "nada rodou ainda"
                : `${num(hoje.total)} execuç${hoje.total === 1 ? "ão" : "ões"}`}
            </b>
            {hoje.total > 0 && (
              <>
                {" · "}
                {hoje.falha === 0 ? (
                  // "nenhuma falha" com execução em andamento é atestado de saúde para desfecho
                  // que ainda não existe.
                  <span>
                    {hoje.executando
                      ? `${num(hoje.executando)} em andamento`
                      : "nenhuma falha"}
                  </span>
                ) : (
                  <button
                    type="button"
                    className="lk exec-resumo-falha"
                    data-testid="exec-resumo-falhas-hoje"
                    onClick={() => {
                      const p = new URLSearchParams(params);
                      p.set("st", "falha");
                      p.set("per", "hoje");
                      p.delete("pag");
                      setParams(p, { replace: true });
                    }}
                  >
                    {num(hoje.falha)} falha{hoje.falha === 1 ? "" : "s"}
                  </button>
                )}
              </>
            )}
          </p>
        )}
      </div>
      <div className="pad">
        <div className="card">
          <div className="card-h">
            <h3>Histórico</h3>
            <span
              className="tag junto"
              data-testid="exec-total"
              title="Uma corrida é um disparo: a execução do pipeline e as dependências que rodaram junto. Os números das abas e dos chips contam execuções."
            >
              {/* O total é de CORRIDAS (é o que a paginação percorre), não de execuções — escrito
                  "execuções" ao lado de chips que contam execuções, "3.854" contra "31.672"
                  parecia erro de conta. */}
              {/* Três estados também no CONTADOR: ele afirmava "nenhuma execução" enquanto a
                  primeira carga corria e depois de a chamada falhar (o corpo da lista já
                  distinguia; o cabeçalho tinha ficado de fora do mesmo passe). */}
              {carregando && itens.length === 0
                ? "carregando…"
                : erro && itens.length === 0
                  ? "não foi possível carregar"
                  : total === 0
                ? temFiltro
                  ? "nenhuma com esses filtros"
                  : "nenhuma execução"
                : total === 1
                  ? temFiltro
                    ? "1 corrida (filtrada)"
                    : "1 corrida"
                  : `${num(total)} corridas${temFiltro ? " (filtradas)" : ""}`}
            </span>
            <div className="exec-h-acoes">
              <span className="exec-atualizado" role="status" aria-live="polite">
                {carregando ? "atualizando…" : ""}
              </span>
              <span className="exec-atualizado">
                {atualizadoEm ? `atualizado às ${atualizadoEm} (GMT-3)` : ""}
                {(temRodando || status === "executando") && (
                  <span className="exec-aovivo" title="atualizando sozinho a cada 5 segundos">
                    ao vivo
                  </span>
                )}
              </span>
              <button
                type="button"
                className="pag-btn"
                data-testid="exec-atualizar"
                disabled={carregando}
                onClick={() => void carregar()}
              >
                Atualizar
              </button>
            </div>
          </div>

          <div className="exec-abas">
          <AbasFiltro
            abas={ABAS_EXECUCAO.map((a) => ({
              chave: a.chave || "todas",
              rotulo: a.rotulo,
              contagem: contagemAbas
                ? a.chave
                  ? contagemAbas[a.chave]
                  : contagemAbas.todas
                : undefined,
            }))}
            ativa={aba || "todas"}
            onAba={(c) => trocarAba(c === "todas" ? "" : c)}
            rotulo="Filtrar execuções pela classificação do pipeline"
            idPainel="exec-painel"
            idPrefixo="exec-aba"
          />
          </div>

          <div className="exec-busca" role="search">
            <input
              type="search"
              data-testid="exec-busca"
              aria-label="Buscar execuções"
              placeholder="Buscar por pipeline, erro, usuário, agendamento, id ou parâmetro (competencia=2024-01)"
              value={buscaDigitada}
              onChange={(e) => setBuscaDigitada(e.target.value)}
              onKeyDown={(e) => {
                // Enter não espera o debounce; Esc limpa.
                if (e.key === "Enter") trocarFiltro("q", buscaDigitada.trim());
                if (e.key === "Escape" && buscaDigitada) {
                  setBuscaDigitada("");
                  trocarFiltro("q", "");
                }
              }}
            />
            <span className="exec-busca-dica">
              Todas as palavras precisam casar · <code>chave=valor</code> busca um parâmetro exato
            </span>
          </div>

          <div className="exec-filtros" data-testid="exec-filtros">
            <label className="exec-filtro">
              <span className="eyebrow">Pipeline</span>
              <select
                data-testid="exec-filtro-pipeline"
                value={pipeId}
                onChange={(e) => trocarFiltro("pipe", e.target.value)}
              >
                <option value="">
                  {aba ? "Todos desta aba" : "Todos os pipelines"}
                </option>
                {pipelinesDaAba.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.nome}
                  </option>
                ))}
              </select>
            </label>

            <div className="exec-filtro">
              <span className="eyebrow" id="rot-status">
                Status
              </span>
              <div className="exec-chips" role="group" aria-labelledby="rot-status">
                {STATUS_CHIPS.map((c) => {
                  // Contagem no próprio chip: "Falhas · 3" responde "algo falhou?" de relance.
                  const n = contagens
                    ? c.valor === ""
                      ? contagens.todas
                      : contagens[c.valor]
                    : undefined;
                  const alerta = c.valor === "falha" && !!contagens?.falha;
                  return (
                    <button
                      key={c.valor || "todas"}
                      type="button"
                      aria-pressed={status === c.valor}
                      className={
                        "chip" + (status === c.valor ? " on" : "") + (alerta ? " alerta" : "")
                      }
                      data-testid={`exec-filtro-status-${c.valor || "todas"}`}
                      onClick={() => trocarFiltro("st", c.valor)}
                    >
                      {c.rotulo}
                      {n != null && (
                        <span className={"cnt" + (n === 0 ? " zero" : "")}> · {num(n)}</span>
                      )}
                    </button>
                  );
                })}
              </div>
            </div>

            <label className="exec-filtro">
              <span className="eyebrow">Disparo</span>
              <select
                data-testid="exec-filtro-origem"
                value={origem}
                onChange={(e) => trocarFiltro("orig", e.target.value)}
              >
                <option value="">Todos</option>
                <option value="manual">Manual</option>
                <option value="agendado">Agendada</option>
                <option value="reexecucao">Reexecução</option>
              </select>
            </label>

            <div className="exec-filtro exec-filtro-periodo">
              <span className="eyebrow">Período</span>
              <SeletorPeriodo
                periodo={periodo}
                permitirTudo
                testid="exec-filtro-periodo"
                onChange={(t) => trocarFiltro("per", t)}
              />
            </div>

            {temFiltro && (
              <button
                type="button"
                className="exec-limpar"
                data-testid="exec-limpar-filtros"
                onClick={() => setParams(new URLSearchParams(), { replace: true })}
              >
                Limpar filtros
              </button>
            )}
          </div>

          {erro && (
            <div className="exec-erro-lista" role="alert">
              {erro}{" "}
              <button type="button" className="pag-btn" onClick={() => void carregar()}>
                Tentar de novo
              </button>
            </div>
          )}

          {/* Três estados, não dois: com o banner de erro em cima, "Nenhuma execução registrada"
              logo abaixo afirmava um fato que a tela não apurou (a chamada falhou). */}
          {carregando && itens.length === 0 ? (
            <div className="exec-lista-vazia">Carregando…</div>
          ) : erro && itens.length === 0 ? null : itens.length === 0 ? (
            <div className="lc-empty">
              <div className="big">
                {temFiltro ? "Nenhuma execução com esses filtros" : "Nenhuma execução registrada"}
              </div>
              {temFiltro ? (
                <>
                  Ajuste a busca, a aba, o período ou o status.{" "}
                  <button
                    type="button"
                    className="lk"
                    data-testid="exec-vazio-limpar"
                    onClick={() => setParams(new URLSearchParams(), { replace: true })}
                  >
                    Limpar filtros
                  </button>
                </>
              ) : (
                "Rode um pipeline no Studio ou aguarde um agendamento."
              )}
            </div>
          ) : (
            <div
              className="exec-tabela-wrap"
              tabIndex={0}
              role="region"
              aria-label="Histórico de execuções"
              aria-busy={carregando}
              data-carregando={carregando}
            >
              <table className="dgrid exec-tabela" data-testid="exec-tabela" id="exec-painel">
                <thead>
                  <tr>
                    {/* Status logo no começo: é a pergunta nº 1 da tela, e como última coluna era
                        a primeira a sair de vista em tela estreita. Versão foi para junto do id. */}
                    {mostrarPipeline && <th scope="col">Pipeline</th>}
                    <th scope="col">Status</th>
                    <th scope="col">Execução</th>
                    <th scope="col">Início (GMT-3)</th>
                    <th scope="col" className="num">Duração</th>
                    <th scope="col">Disparo</th>
                    <th scope="col">Usuário</th>
                    {/* Cabeçalho vazio, como no GridLista: "Ações" repetido em toda tela vira
                        ruído, e o menu já se anuncia pelo aria-label do próprio botão. */}
                    <th scope="col" aria-label="Ações" />
                  </tr>
                </thead>
                <tbody>
                  {grupos.map((g, gi) => {
                    // O separador de dia usa a RAIZ do grupo: a corrida inteira é um evento só no
                    // tempo, e as dependências rodam segundos antes — separá-las por dia partiria
                    // a corrida ao meio numa virada de meia-noite.
                    const dia = diaSP(g.raiz.iniciada_em);
                    const diaAnterior = gi > 0 ? diaSP(grupos[gi - 1].raiz.iniciada_em) : null;
                    const unica = ehCorridaUnica(g);
                    const aberta = abertas.has(g.chave);
                    return (
                      <Fragment key={g.chave}>
                        {/* Separador por dia: "o que rodou hoje" se lê pela estrutura da lista. */}
                        {dia && dia !== diaAnterior && (
                          <tr className="exec-dia">
                            <td colSpan={colunas}>{rotuloDia(dia)}</td>
                          </tr>
                        )}
                        <LinhaExecucao
                          e={g.raiz}
                          mostrarPipeline={mostrarPipeline}
                          abrir={abrir}
                          onReexecutar={setReexecId}
                          // Cabeçalho do acordeão só quando há corrida — em execução solta, seta e
                          // resumo seriam decoração em toda linha da tela.
                          corrida={
                            unica
                              ? null
                              : {
                                  aberta,
                                  resumo: resumoCorrida(g),
                                  composicao: composicaoCorrida(g),
                                  janela: janelaCorrida(g),
                                  temFalha: corridaTemFalha(g),
                                  contexto: raizEhContexto(g),
                                  alternar: () => trocarAberta(g.chave),
                                  mapa: g.raiz.cadeia_id
                                    ? () => abrirMapa(g.raiz.cadeia_id as string)
                                    : null,
                                }
                          }
                        />
                        {!unica &&
                          aberta &&
                          filhasVisiveis(g).map((f) => (
                            <LinhaExecucao
                              key={f.id}
                              e={f}
                              mostrarPipeline={mostrarPipeline}
                              abrir={abrir}
                              onReexecutar={setReexecId}
                              filha
                            />
                          ))}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          {/* Paginação FORA da área rolável: dentro dela, rolar a tabela levava os controles junto. */}
          {itens.length > 0 && total > EXEC_POR_PAGINA && (
            <div className="paginacao" data-testid="exec-paginacao">
                  <button
                    type="button"
                    className="pag-btn"
                    data-testid="exec-pag-anterior"
                    disabled={pagina === 1 || carregando}
                    onClick={() => irParaPagina(pagina - 1)}
                  >
                    ← Anteriores
                  </button>
                  {/* A paginação conta CORRIDAS (é o que ela percorre) e o rodapé diz quantas
                      execuções há nelas — sem isso "1–25 de 300" e as 38 linhas na tela pareceriam
                      contradição. */}
                  <span className="pag-info">
                    {offset + 1}–{Math.min(offset + grupos.length, total)} de {total} corrida
                    {total === 1 ? "" : "s"}
                    {itens.length !== grupos.length && ` · ${itens.length} execuções`}
                  </span>
                  <button
                    type="button"
                    className="pag-btn"
                    data-testid="exec-pag-proxima"
                    disabled={offset + EXEC_POR_PAGINA >= total || carregando}
                    onClick={() => irParaPagina(pagina + 1)}
              >
                Próximas →
              </button>
            </div>
          )}
        </div>
      </div>
      {reexecId && (
        <ConfirmarReexecucao
          execucaoId={reexecId}
          onCancelar={() => setReexecId(null)}
          // Vai para a execução NOVA: é nela que o resultado aparece, e ficar na lista deixaria a
          // pessoa procurando qual das linhas é a que ela acabou de disparar.
          onFeito={(novaId) => {
            setReexecId(null);
            navigate(`/execucoes/${novaId}`);
          }}
        />
      )}
    </>
  );
}

// Uma linha da grade — a mesma para a raiz da corrida e para as dependências dentro do acordeão.
// Extraída porque as duas precisam ser idênticas em formatação e comportamento: escrever a linha
// duas vezes faria a filha divergir da mãe no dia em que uma coluna mudar.
function LinhaExecucao({
  e,
  mostrarPipeline,
  abrir,
  onReexecutar,
  corrida,
  filha,
}: {
  e: ExecucaoItem;
  mostrarPipeline: boolean;
  abrir: (id: string) => void;
  onReexecutar: (id: string) => void;
  // Presente só na linha-mãe de uma corrida com dependências.
  corrida?: {
    aberta: boolean;
    resumo: string;
    composicao: ComposicaoCorrida;
    janela: { inicio: string | null; fim: string | null };
    temFalha: boolean;
    contexto: boolean; // a mãe não casa o filtro: está aqui como contexto, não como resultado
    alternar: () => void;
    mapa?: (() => void) | null; // abre o mapa da corrida; ausente = sem o link
  } | null;
  filha?: boolean;
}) {
  const pill = pillExecucao(e.status);
  // Na corrida, `iniciada_em` é o instante do DISPARO para todas (pré-criação) — ver
  // `tempoNaCorrida`. A filha mostra o início e a duração DELA; a mãe, o disparo e a corrida
  // inteira; execução solta continua como sempre foi.
  const tempo = filha || corrida ? tempoNaCorrida(e) : null;
  const inicioLinha = filha ? (tempo?.tipo === "rodou" ? tempo.inicio : null) : e.iniciada_em;
  const dia = diaSP(inicioLinha);
  const hoje = new Date().toLocaleDateString("en-CA", { timeZone: "America/Sao_Paulo" });
  const falhou = e.status === "falha";
  const disparo = e.pagina_nome
    ? `página · ${e.pagina_nome}`
    : rotuloOrigem(e.origem, e.agendamento_nome);
  return (
    <tr
      className={
        "clic" +
        (falhou ? " linha-falha" : "") +
        (e.status === "executando" ? " linha-exec" : "") +
        (filha ? " exec-filha" : "") +
        (corrida?.aberta ? " exec-corrida-aberta" : "") +
        (corrida?.contexto ? " exec-contexto" : "")
      }
      data-testid={`exec-linha-${e.id.slice(0, 8)}`}
      onClick={() => {
        // Selecionar texto na linha não deve navegar (copiar o nome do pipeline é gesto legítimo).
        if (window.getSelection()?.toString()) return;
        abrir(e.id);
      }}
    >
      {mostrarPipeline && (
        <td>
          {/* Nome PRIMEIRO: é o que se procura na lista. O agrupador da corrida vem embaixo, como
              metadado da linha — em cima, o "+37 pipelines" levava a primeira leitura do olho. */}
          <span className="exec-cel-forte" title={e.pipeline_nome}>
            {e.pipeline_nome}
          </span>
          {corrida && (
            <span className="exec-corrida-linha">
              {/* Chip, não link: a área de clique inteira abre. O marcador fica dentro, mas
                  `aria-hidden` — o nome acessível é o resumo e o estado vem do `aria-expanded`. */}
              <button
                type="button"
                className="exec-corrida-chip"
                aria-expanded={corrida.aberta}
                data-testid={`exec-corrida-${e.id.slice(0, 8)}`}
                title={corrida.aberta ? "Recolher a corrida" : "Ver os pipelines desta corrida"}
                onClick={(ev) => {
                  ev.stopPropagation();
                  corrida.alternar();
                }}
              >
                {/* SVG e não o glifo ▸: nesta fonte o triângulo sai do tamanho de um ponto, e o
                    chip deixava de se anunciar como expansível. */}
                <svg className="exec-corrida-caret" viewBox="0 0 10 10" aria-hidden="true">
                  <path d="M3.5 2 L6.5 5 L3.5 8" />
                </svg>
                {corrida.resumo}
              </button>
              <BarraCorrida c={corrida.composicao} />
              {/* O acordeão diz QUEM rodou; o mapa diz quem esperou quem. <a> de verdade, para
                  cmd-clique abrir em outra aba (mesma regra do link da execução). */}
              {corrida.mapa && e.cadeia_id && (
                <a
                  className="exec-corrida-mapa"
                  href={rota(`/corridas/${e.cadeia_id}`)}
                  data-testid={`exec-corrida-mapa-${e.id.slice(0, 8)}`}
                  title="Ver os pipelines desta corrida num mapa: quem dependeu de quem"
                  onClick={(ev) => {
                    ev.stopPropagation();
                    if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button !== 0) return;
                    ev.preventDefault();
                    corrida.mapa?.();
                  }}
                >
                  Ver mapa
                </a>
              )}
            </span>
          )}
          {falhou && e.erro && <span className="exec-cel-erro">{e.erro}</span>}
          {/* A corrida fechada avisa que há falha lá dentro, mas a pill da MÃE continua sendo o
              desfecho dela: verde é "esta terminou bem", e herdar o vermelho da filha mentiria
              sobre esta execução (mesma regra da tela de execução). */}
          {corrida && !corrida.aberta && corrida.temFalha && !falhou && (
            <span className="exec-cel-erro">falha em um pipeline desta corrida</span>
          )}
          {corrida?.contexto && (
            <span className="exec-cel-versao">fora do filtro — mostrado como contexto</span>
          )}
        </td>
      )}
      <td>
        <span className={"statuspill " + pill.cls} title={e.erro ?? undefined}>
          <span className="d" />
          {pill.label}
        </span>
      </td>
      <td>
        {/* Botão de verdade (não só o clique na linha): a lista precisa ser navegável por teclado,
            como era com o "Abrir" do GridLista. */}
        {/* <a> de verdade: comparar duas execuções lado a lado é gesto básico de quem reconcilia, e
            isso exige cmd-clique/clique do meio. O onClick só intercepta o clique simples, para
            levar os filtros. */}
        <a
          className="exec-cel-link"
          href={rota(`/execucoes/${e.id}`)}
          aria-label={`Abrir execução ${e.id.slice(0, 8)} de ${e.pipeline_nome}, ${fmtMomentoCurto(e.iniciada_em)}`}
          onClick={(ev) => {
            ev.stopPropagation();
            if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button !== 0) return;
            ev.preventDefault();
            abrir(e.id);
          }}
        >
          {e.id.slice(0, 8)}
        </a>
        <span className="exec-cel-versao"> · v{e.versao}</span>
      </td>
      <td title={inicioLinha ? fmtMomento(inicioLinha) : undefined}>
        {!inicioLinha ? "—" : dia === hoje ? fmtHora(inicioLinha) : fmtMomentoCurto(inicioLinha)}
      </td>
      <CelulaDuracao e={e} tempo={tempo} janela={corrida?.janela ?? null} filha={!!filha} />
      {/* Origem completa aqui; "Usuário" só recebe quem é usuário de verdade (antes o nome de uma
          PÁGINA aparecia sob o cabeçalho Usuário). */}
      {/* O disparo agendado escreve uma frase longa ("agendada · Recarga mensal — bronze · …") e
          empurrava Usuário e Ações para fora da área visível. Truncagem OPT-IN nesta célula (nunca
          um seletor posicional, que atingiria toda `.dgrid` do produto), com o texto completo no
          `title`. */}
      <td className="dg-trunc" title={disparo}>
        {disparo}
      </td>
      <td>{e.ator_nome ?? "—"}</td>
      {/* `stopPropagation`: a linha inteira navega, e clicar no menu não pode abrir o detalhe —
          é o mesmo cuidado que `GridLista` já toma na célula de ações. */}
      <td className="dg-acoes" onClick={(ev) => ev.stopPropagation()}>
        <MenuAcoes rotulo={`Ações da execução ${e.id.slice(0, 8)}`}>
          <button type="button" role="menuitem" onClick={() => onReexecutar(e.id)}>
            Executar de novo
          </button>
        </MenuAcoes>
      </td>
    </tr>
  );
}

// Duração da linha. Três casos, porque a corrida pré-cria tudo no instante do disparo:
//  - MÃE de corrida: a corrida inteira (do disparo ao fim da última execução), rotulada na célula —
//    sem o rótulo ela parece o tempo do pipeline da linha, e as filhas não "somam" com ela;
//  - FILHA: só o que ela rodou, do primeiro nó ao fim. A fila das dependências vai no `title`;
//  - execução solta: fim − início, como sempre.
function CelulaDuracao({
  e,
  tempo,
  janela,
  filha,
}: {
  e: ExecucaoItem;
  tempo: TempoNaCorrida | null;
  janela: { inicio: string | null; fim: string | null } | null;
  filha: boolean;
}) {
  if (janela) {
    const proprio =
      tempo?.tipo === "rodou" && tempo.fim ? fmtDuracao(tempo.inicio, tempo.fim) : null;
    return (
      <td
        className="num"
        title={
          "Corrida inteira: do disparo ao fim da última execução." +
          (proprio ? ` Este pipeline rodou ${proprio}.` : "")
        }
      >
        {fmtDuracao(janela.inicio, janela.fim, "em curso")}
        <span className="exec-dur-sub">corrida inteira</span>
      </td>
    );
  }
  if (filha && tempo?.tipo === "aguardando") {
    return (
      <td className="num exec-dur-espera" title="Nenhum nó começou: esperando as dependências.">
        aguardando
      </td>
    );
  }
  if (filha && tempo?.tipo === "sem_nos") {
    const parou = e.status === "falha";
    return (
      <td
        className="num exec-dur-espera"
        title={parou ? "A corrida parou antes de esta execução começar." : "Nenhum nó registrado."}
      >
        {parou ? "não rodou" : "—"}
      </td>
    );
  }
  if (filha && tempo?.tipo === "rodou") {
    const espera =
      tempo.esperaMs != null && tempo.esperaMs >= 1000
        ? ` Esperou as dependências por ${fmtDuracaoMs(tempo.esperaMs)} antes de começar.`
        : "";
    return (
      <td className="num" title={`Tempo que este pipeline rodou, do primeiro nó ao fim.${espera}`}>
        {fmtDuracao(tempo.inicio, tempo.fim, "em curso")}
      </td>
    );
  }
  return <td className="num">{fmtDuracao(e.iniciada_em, e.concluida_em, "em curso")}</td>;
}

// Como as dependências da corrida terminaram, nas MESMAS cores da pill (verde/azul/vermelho) —
// lida de relance sem abrir o acordeão. Proporcional, com piso de largura por parte: 1 falha em 37
// tem de aparecer, porque é justamente a que se vem procurar. A cor não é a única leitura: o mesmo
// texto vai no `aria-label` e no `title`.
function BarraCorrida({ c }: { c: ComposicaoCorrida }) {
  const rotulo = `Dependências: ${rotuloComposicao(c)}`;
  return (
    <span className="exec-corrida-barra" role="img" aria-label={rotulo} title={rotulo}>
      {(["concluida", "executando", "falha"] as const).map((k) =>
        c[k] > 0 ? <span key={k} className={`seg ${k}`} style={{ flexGrow: c[k] }} /> : null,
      )}
    </span>
  );
}
