import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";

import * as api from "../../api/client";
import { usePollVisivel } from "../poll";
import type { ExecucaoDetalhe as Detalhe } from "../../api/client";
import ExecGrafo, {
  cruzarNos,
  ordemDeLeitura,
  type DefinicaoGrafo,
  type NoDoGrafo,
} from "../execucao/ExecGrafo";
import ConfirmarReexecucao from "../execucao/ConfirmarReexecucao";
import PainelNo from "../execucao/PainelNo";
import TrilhaNos from "../execucao/TrilhaNos";
import {
  fmtDuracao,
  fmtDuracaoMs,
  fmtMomento,
  pillExecucao,
  rotuloOrigem,
  type EstadoNo,
} from "../execucao/formato";

// ─────────────────────────────────────────────────────────────────────────────
// Detalhe de UMA execução: resumo no topo, grafo com zoom/pan no meio, dado do nó embaixo.
//
// Tem rota própria (`/execucoes/:id`) — é linkável, o voltar do navegador funciona e o
// Histórico de Agendamentos aponta direto para cá.
//
// A definição buscada é a da VERSÃO EXECUTADA (`obterPipeline(pid, versao)`), não a última:
// abrir uma execução antiga depois de editar o pipeline mostrava o grafo errado (nós que não
// existiam quando ela rodou). Se aquela versão não estiver disponível, cai para a atual COM
// aviso, e o que rodou fora dela aparece marcado como "fora da versão".
// ─────────────────────────────────────────────────────────────────────────────

// 5 s, não 2 s: cada pedido custa uma ida e volta pela rede, e um nó leva
// de segundos a minutos — a cada 2 s a tela pedia mais do que mudava. Mesma cadência da lista de
// Execuções. Com a aba oculta o poll pausa (ver `usePollVisivel`).
const POLL_MS = 5000;
// Acima disto o grafo vira um recorte pouco informativo; a tela abre na lista de nós.

// Vocabulário e cor de cada estado de nó, na ordem em que a legenda os apresenta.
const LEGENDA: { estado: EstadoNo; cls: string; rotulo: string; dica?: string }[] = [
  {
    estado: "indefinido",
    cls: "skip",
    rotulo: "desfecho desconhecido",
    dica: "o nó começou e não reportou o fim — se ele grava fora da plataforma, o efeito pode ter acontecido",
  },
  { estado: "ok", cls: "ok", rotulo: "concluído" },
  {
    estado: "reap",
    cls: "reap",
    rotulo: "reaproveitado",
    dica: "entrada idêntica à da execução anterior — o snapshot foi reusado",
  },
  { estado: "falha", cls: "falha", rotulo: "falha" },
  {
    estado: "executando",
    cls: "exec",
    rotulo: "em execução",
    dica: "o nó está rodando agora — o card mostra há quanto tempo",
  },
  { estado: "pendente", cls: "off", rotulo: "pendente", dica: "ainda não começou" },
  { estado: "skip", cls: "skip", rotulo: "não executado" },
];

export default function ExecucaoDetalhe() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  // A lista manda os filtros vigentes em `state.volta` — voltar devolve a MESMA visão (status,
  // período, disparo), não uma lista filtrada só por pipeline. Sem esse estado (deep-link, F5,
  // link do agendamento) cai no filtro do pipeline desta execução.
  const { state } = useLocation();
  const voltaSalva = (state as { volta?: string } | null)?.volta ?? null;

  const [detalhe, setDetalhe] = useState<Detalhe | null>(null);
  const [definicao, setDefinicao] = useState<DefinicaoGrafo | null>(null);
  const [pipelineNome, setPipelineNome] = useState<string | null>(null);
  const [avisoVersao, setAvisoVersao] = useState<string | null>(null);
  const [selNo, setSelNo] = useState<string | null>(null);
  // Grafo = topologia; lista = varredura rápida ("qual falhou", "quantas linhas em cada etapa").
  // Num pipeline longo o grafo exige zoom/pan para responder isso, então a lista existe ao lado.
  const [visao, setVisao] = useState<"grafo" | "lista">("grafo");
  const visaoAutoFeita = useRef(false);
  const [carregando, setCarregando] = useState(true);
  const [erro, setErro] = useState<string | null>(null);
  // Confirmação de reexecução aberta, e de que ponto ela parte (`null` = a execução inteira).
  // `null` no ESTADO significa fechada — por isso o objeto, e não um `desdeNo: string | null` solto.
  const [reexec, setReexec] = useState<{ desdeNo: string | null } | null>(null);
  // Auto-seleção acontece UMA vez por execução, quando ela chega ao fim (durante o poll o painel
  // não pode ficar pulando de nó a cada atualização).
  const autoSelFeita = useRef(false);

  const rodando = detalhe?.status === "executando";

  const carregar = useCallback(
    async (silencioso = false) => {
      if (!silencioso) setCarregando(true);
      try {
        const d = await api.consultarExecucao(id);
        setDetalhe(d);
        if (!silencioso) {
          try {
            const p = await api.obterPipeline(d.pipeline_id, d.versao);
            setDefinicao(p.definicao);
            setPipelineNome(p.nome ?? null);
            setAvisoVersao(null);
          } catch {
            // Versão indisponível (ou sem permissão de abrir o pipeline): tenta a atual; se nem
            // isso, o grafo é montado só com o que rodou (sem arestas) — o dado nunca fica preso.
            try {
              const p = await api.obterPipeline(d.pipeline_id);
              setDefinicao(p.definicao);
              setPipelineNome(p.nome ?? null);
              setAvisoVersao(
                `A definição da versão ${d.versao} não está disponível — o grafo mostrado é o da versão atual.`,
              );
            } catch {
              setDefinicao(null);
              setAvisoVersao(
                "Não foi possível carregar o grafo do pipeline — mostrando apenas os nós que executaram.",
              );
            }
          }
        }
        setErro(null);
      } catch (e: any) {
        if (!silencioso) setErro(e?.message ?? "Falha ao abrir a execução.");
      } finally {
        if (!silencioso) setCarregando(false);
      }
    },
    [id],
  );

  useEffect(() => {
    autoSelFeita.current = false;
    visaoAutoFeita.current = false; // simétrico ao autoSelFeita: outra execução, outra decisão
    setSelNo(null);
    carregar();
  }, [carregar]);

  // Poll enquanto a execução está em andamento — a tela pinta os nós conforme terminam. Para
  // sozinho quando o status sai de "executando" (`rodando` desliga o hook) e pausa com a aba oculta.
  const atualizarEmSilencio = useCallback(() => void carregar(true), [carregar]);
  usePollVisivel(rodando, atualizarEmSilencio, POLL_MS);

  // Tique de 1s só enquanto roda, para o cronômetro da duração andar na tela.
  const [agora, setAgora] = useState(() => Date.now());
  useEffect(() => {
    if (!rodando) return;
    setAgora(Date.now());
    const t = window.setInterval(() => setAgora(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [rodando]);

  const nos: NoDoGrafo[] = useMemo(
    () => (detalhe ? cruzarNos(definicao, detalhe.nos, detalhe.status === "executando") : []),
    [detalhe, definicao],
  );
  // Ordem de leitura (profundidade no DAG) — usada pela lista e pela auto-seleção: "o último que
  // rodou" é o nó FINAL do fluxo, não o último item do JSON da definição.
  const nosOrdenados = useMemo(() => ordemDeLeitura(nos), [nos]);
  const noFalho = useMemo(() => nos.find((n) => n.estado === "falha"), [nos]);

  // Ao terminar, seleciona o nó que interessa (o que falhou; senão o último que rodou) — a
  // amostra do dado aparece sem exigir clique.
  useEffect(() => {
    if (!detalhe || rodando || autoSelFeita.current || nos.length === 0) return;
    const executados = nosOrdenados.filter((n) => n.executou);
    const alvo = noFalho ?? executados[executados.length - 1] ?? null;
    if (alvo) {
      setSelNo(alvo.id);
      // Falha é a exceção: aí ir até o nó É o que se quer ver. Sucesso mantém o enquadramento.
      if (noFalho) setFocoPedido((n) => n + 1);
      autoSelFeita.current = true;
    }
  }, [detalhe, rodando, nos, nosOrdenados, noFalho]);

  // A visão padrão é sempre o GRAFO: é ele que responde "por onde o dado passou", e era o que se
  // esperava ver ao abrir a execução. A lista continua a um clique (e é melhor para pipeline longo,
  // mas essa é escolha de quem olha, não da tela).

  const selecionado = useMemo(
    () => nos.find((n) => n.id === selNo) ?? null,
    [nos, selNo],
  );

  // Escolher um nó por clique/teclado tem de MOSTRAR o dado dele: em tela de 1180px o painel
  // ficava abaixo da dobra e a seleção parecia não ter efeito. Só rola quando o usuário escolhe
  // (não na auto-seleção da abertura, que já deixa a página no topo).
  const painelRef = useRef<HTMLDivElement | null>(null);
  // Cada escolha EXPLÍCITA de nó incrementa o contador; o grafo usa isso para decidir se
  // recentraliza (na abertura, não).
  const [focoPedido, setFocoPedido] = useState(0);
  const escolherNo = useCallback((id: string) => {
    setSelNo(id);
    setFocoPedido((n) => n + 1);
    const suave = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    window.setTimeout(
      () => painelRef.current?.scrollIntoView({ block: "nearest", behavior: suave ? "smooth" : "auto" }),
      60,
    );
  }, []);

  if (carregando && !detalhe) {
    return (
      <div className="pad">
        <div className="card exec-painel-vazio">Carregando execução…</div>
      </div>
    );
  }

  if (erro || !detalhe) {
    return (
      <>
        <div className="page-head">
          <button className="link-back" onClick={() => navigate("/execucoes")}>
            ← Execuções
          </button>
          <h1 className="display">Execução</h1>
        </div>
        <div className="pad">
          <div className="card exec-erro-lista" role="alert">
            {erro ?? "Execução não encontrada."}
          </div>
        </div>
      </>
    );
  }

  const pill = pillExecucao(detalhe.status);
  const okCount = nos.filter((n) => n.estado === "ok" || n.estado === "reap").length;
  const falhas = nos.filter((n) => n.estado === "falha").length;
  // Nós que NÃO rodaram entram na conta: "1/3 ok · 1 falha" deixava o terceiro nó invisível, o que
  // esconde justamente o que a execução não fez.
  const naoRodaram = nos.filter((n) => n.estado === "skip" || n.estado === "pendente").length;
  // O que está rodando AGORA tem contagem própria: somado a "pendente", o resumo dizia
  // "12 pendente" numa execução em que um nó trabalhava havia 20 minutos.
  const emExecucao = nos.filter((n) => n.estado === "executando");
  // Nó que ficou "executando" numa execução encerrada: efeito externo disparado sem confirmação.
  // Não entra em `ok` (não é sucesso) nem em `falha` (não se sabe) — aparece por si.
  const indefinidos = nos.filter((n) => n.estado === "indefinido").length;
  const reusados = nos.filter((n) => n.estado === "reap").length;
  // Nó que concluiu com ZERO linha conta como "ok" e some no resumo — é o mesmo "verde = deu certo"
  // que a regra já proíbe na lista de execuções. Numa conferência de dado, um passo que não
  // produziu linha é justamente o que se procura.
  const vazios = nos.filter(
    (n) => (n.estado === "ok" || n.estado === "reap") && (n.linhas ?? null) === 0,
  ).length;
  const params = Object.entries(detalhe.parametros ?? {});
  const estadosPresentes = new Set(nos.map((n) => n.estado));

  return (
    <>
      <div className="page-head">
        <button
          className="link-back"
          data-testid="exec-voltar"
          onClick={() =>
            navigate(`/execucoes?${voltaSalva ?? `pipe=${detalhe.pipeline_id}`}`)
          }
        >
          ← Execuções
        </button>
        {/* Fora do grafo, no cabeçalho: refazer a corrida INTEIRA é decisão sobre a execução, não
            sobre um nó dela. O botão por nó vive no painel do nó selecionado. */}
        <button
          type="button"
          className="pag-btn exec-reexec"
          data-testid="exec-reexecutar"
          disabled={rodando}
          title={
            rodando
              ? "A execução ainda está em andamento"
              : "Roda este pipeline de novo, na mesma versão e com os mesmos parâmetros"
          }
          onClick={() => setReexec({ desdeNo: null })}
        >
          ↻ Executar de novo
        </button>
        {/* O título é O QUE rodou; o hash é identificação, não identidade. */}
        <h1 className="display">{pipelineNome ?? "Execução"}</h1>
        <p className="sub">
          execução <span className="exec-cel-mono">{detalhe.id.slice(0, 8)}</span> · versão{" "}
          {detalhe.versao}
          {pipelineNome && (
            <>
              {" · "}
              <button
                type="button"
                className="lk"
                data-testid="exec-abrir-studio"
                onClick={() => navigate(`/pipeline?abrir=${detalhe.pipeline_id}`)}
              >
                abrir no Studio
              </button>
            </>
          )}
          {detalhe.cadeia_id && (
            <>
              {" · "}
              {/* A execução faz parte de uma corrida: o mapa mostra de quem ela dependeu. */}
              <button
                type="button"
                className="lk"
                data-testid="exec-ver-corrida"
                onClick={() => navigate(`/corridas/${detalhe.cadeia_id}?sel=${detalhe.id}`)}
              >
                ver a corrida no mapa
              </button>
            </>
          )}
        </p>
        <div className="run-meta">
          <div className="meta">
            <span className="k">Status</span>
            <span className={"statuspill " + pill.cls} data-testid="exec-status">
              <span className="d" />
              {pill.label}
            </span>
          </div>
          <div className="meta">
            <span className="k">Início</span>
            <span className="v">{fmtMomento(detalhe.iniciada_em)}</span>
          </div>
          <div className="meta">
            <span className="k">Duração</span>
            <span className="v num" data-testid="exec-duracao">
              {rodando && detalhe.iniciada_em
                ? // Cronômetro correndo: sem isto a tela de uma execução longa parecia travada.
                  fmtDuracaoMs(agora - new Date(detalhe.iniciada_em).getTime())
                : fmtDuracao(detalhe.iniciada_em, detalhe.concluida_em)}
            </span>
          </div>
          <div className="meta">
            <span className="k">Disparo</span>
            <span className="v">{rotuloOrigem(detalhe.origem, detalhe.agendamento_nome)}</span>
          </div>
          {detalhe.pagina_nome && (
            <div className="meta">
              <span className="k">Página</span>
              <span className="v">{detalhe.pagina_nome}</span>
            </div>
          )}
          {detalhe.ator_nome && (
            <div className="meta">
              <span className="k">Usuário</span>
              <span className="v">{detalhe.ator_nome}</span>
            </div>
          )}
          <div className="meta">
            <span className="k">Nós</span>
            <span className="v num" data-testid="exec-contagem-nos">
              {[
                `${okCount}/${nos.length} ok`,
                // Com UM nó rodando, o resumo diz o NOME: é a resposta à pergunta que traz o
                // usuário a esta tela ("em que passo está?") sem obrigar a caçar o card aceso.
                emExecucao.length === 1
                  ? `em execução: ${emExecucao[0].nome}`
                  : emExecucao.length > 1
                    ? `${emExecucao.length} em execução`
                    : null,
                // Reuso incremental é feature central: quando metade do grafo foi reaproveitada,
                // a duração pequena tem explicação — e ela precisa estar no resumo.
                reusados ? `${reusados} reaproveitado${reusados === 1 ? "" : "s"}` : null,
                falhas ? `${falhas} falha` : null,
                vazios ? `${vazios} sem linha` : null,
                indefinidos ? `${indefinidos} sem desfecho conhecido` : null,
                naoRodaram ? `${naoRodaram} ${rodando ? "pendente" : "não executado"}` : null,
              ]
                .filter(Boolean)
                .join(" · ")}
            </span>
          </div>
        </div>
        {params.length > 0 && (
          <div className="exec-run-params" data-testid="exec-parametros">
            <span className="eyebrow">Parâmetros</span>
            {params.map(([k, v]) => (
              <span className="chip" key={k}>
                {k}
                <b>{v === null || v === undefined || v === "" ? "—" : String(v)}</b>
              </span>
            ))}
          </div>
        )}
      </div>

      <div className="pad exec-detalhe">
        {avisoVersao && (
          <div className="exec-aviso" data-testid="exec-aviso-versao">
            {avisoVersao}
          </div>
        )}
        {/* Alerta com AÇÃO NOMEADA: como <button> inteiro, o nome acessível do controle era o
            stack de erro completo, e nada indicava que a faixa era clicável. */}
        {detalhe.status === "falha" && (
          <div className="exec-banner-falha" role="alert" data-testid="exec-banner-falha">
            <span className="dot" />
            <span className="exec-banner-txt">
              {noFalho ? (
                <>
                  <b>Falhou no nó “{noFalho.nome}”</b> — {noFalho.erro ?? detalhe.erro ?? "erro na execução"}.
                  Os nós a jusante não rodaram.
                </>
              ) : (
                <>
                  <b>Execução falhou</b> — {detalhe.erro ?? "erro na execução"}.
                </>
              )}
            </span>
            {noFalho && (
              <button
                type="button"
                className="pag-btn"
                data-testid="exec-ver-no-falho"
                onClick={() => escolherNo(noFalho.id)}
              >
                Ver o nó
              </button>
            )}
            {/* Aqui é onde a pessoa está olhando quando decide refazer: a partir do nó que quebrou,
                não do começo — o que já rodou vem pronto desta execução. */}
            <button
              type="button"
              className="pag-btn"
              data-testid="exec-reexecutar-do-falho"
              onClick={() => setReexec({ desdeNo: noFalho ? noFalho.id : null })}
            >
              {noFalho ? "Executar a partir deste nó" : "Executar de novo"}
            </button>
          </div>
        )}

        <div className="exec-visao-barra">
          {/* Alternador com estado programático (`aria-pressed`): sem isso o "ativo" existiria só
              em cor — o mesmo defeito que os chips de filtro já tinham. */}
          <div
            className="cfg-tabs exec-visao"
            data-testid="exec-visao"
            role="group"
            aria-label="Como ver os nós da execução"
          >
            <button
              type="button"
              data-testid="exec-visao-grafo"
              aria-pressed={visao === "grafo"}
              className={visao === "grafo" ? "on" : ""}
              onClick={() => setVisao("grafo")}
            >
              Grafo
            </button>
            <button
              type="button"
              data-testid="exec-visao-lista"
              aria-pressed={visao === "lista"}
              className={visao === "lista" ? "on" : ""}
              onClick={() => setVisao("lista")}
            >
              Lista de nós
            </button>
          </div>
          {/* A legenda cobre TODAS as cores que o grafo pode pintar: sem o "pendente" (azul) e o
              "reaproveitado" (âmbar), a execução em andamento mostrava cor sem explicação. */}
          {/* Legenda derivada dos estados PRESENTES: ensinar uma cor que não está na tela é ruído. */}
          <span className="exec-legenda">
            {LEGENDA.filter((l) => estadosPresentes.has(l.estado)).map((l) => (
              <span key={l.estado} className={"exec-legenda-item " + l.cls} title={l.dica}>
                {l.rotulo}
              </span>
            ))}
          </span>
        </div>

        {visao === "grafo" ? (
          <ExecGrafo nos={nos} selecionado={selNo} onSelecionar={escolherNo} focoPedido={focoPedido} />
        ) : (
          <TrilhaNos
            nos={nosOrdenados}
            selecionado={selNo}
            onSelecionar={escolherNo}
            focoPedido={focoPedido}
          />
        )}
        <div ref={painelRef}>
          <PainelNo
            execucaoId={detalhe.id}
            no={selecionado}
            onLocalizar={() => setFocoPedido((n) => n + 1)}
            onReexecutar={rodando ? undefined : (noId) => setReexec({ desdeNo: noId })}
          />
        </div>
      </div>
      {reexec && (
        <ConfirmarReexecucao
          execucaoId={detalhe.id}
          desdeNo={reexec.desdeNo}
          onCancelar={() => setReexec(null)}
          // A execução nova é outra linha do histórico, e é NELA que se acompanha o resultado —
          // ficar nesta mostraria o desfecho antigo enquanto a nova roda.
          onFeito={(novaId) => {
            setReexec(null);
            navigate(`/execucoes/${novaId}`);
          }}
        />
      )}
    </>
  );
}
