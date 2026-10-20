import { useEffect, useId, useRef, useState } from "react";

import * as api from "../../api/client";
import { diaSP, fmtHora, rotuloDia } from "../execucao/formato";
import {
  acoesDoPlano,
  escopoComoTexto,
  lerEscopo,
  resumoDescoberta,
  rotuloEstado,
  saidasLake,
  type SaidaLake,
} from "./carimbo";

// Carimbo de OBSOLETO no ponteiro de escopo de um dataset publicado. Só admin.
//
// Dois passos, como a reexecução: CONSULTAR mostra o ponteiro e o que o servidor deixa
// fazer; só então aparece o botão da escrita, com motivo obrigatório (vai para a trilha e para o
// próprio ponteiro). A tela não decide nada: repete o plano que o servidor devolveu. A regra de
// "layout anterior" mora em `cflow_core.ingestao.carimbo`; escrevê-la aqui de novo criaria a segunda
// regra, e as duas divergiriam no primeiro ajuste.

function momento(iso?: string): string {
  if (!iso) return "";
  const dia = diaSP(iso);
  return dia ? `${rotuloDia(dia)} ${fmtHora(iso)} (GMT-3)` : iso;
}

export function CarimboEscopo({
  pipelineId,
  pipelineNome,
  onFechar,
}: {
  pipelineId: string;
  pipelineNome: string;
  onFechar: () => void;
}) {
  const tituloId = useId();
  const caixaRef = useRef<HTMLDivElement | null>(null);
  const [saidas, setSaidas] = useState<SaidaLake[] | null>(null);
  const [erroCarga, setErroCarga] = useState<string | null>(null);
  const [alvo, setAlvo] = useState(0);
  const [escopo, setEscopo] = useState("");
  // Ambiente da gold (`dev`|`prd`). Começa VAZIO de propósito: o servidor não tem default — em
  // branco não pode cair em produção — e a tela também não escolhe por quem carimba.
  const [ambiente, setAmbiente] = useState("");
  const [motivo, setMotivo] = useState("");
  const [plano, setPlano] = useState<api.PlanoEscopo | null>(null);
  const [erro, setErro] = useState<{ mensagem: string; requestId?: string } | null>(null);
  // O motivo em branco só é acusado DEPOIS da tentativa — abrir com o campo em vermelho culpa quem
  // ainda não digitou (regra de modal do produto).
  const [tentou, setTentou] = useState(false);
  const [ocupado, setOcupado] = useState(false);
  const [feito, setFeito] = useState<string | null>(null);
  // Escopos de layout anterior que o servidor achou nas execuções (vale depois da 1ª corrida do
  // layout novo, quando o índice do dataset já não os lista) e o progresso do "carimbar todos".
  const [descoberta, setDescoberta] = useState<api.DescobertaEscopos | null>(null);
  const [lote, setLote] = useState<{ feitos: number; total: number } | null>(null);

  useEffect(() => {
    let vivo = true;
    api
      .obterPipeline(pipelineId)
      .then((det) => {
        if (vivo) setSaidas(saidasLake(det.definicao));
      })
      .catch((e: any) => {
        if (vivo) setErroCarga(e?.message ?? "Falha ao ler a definição do pipeline.");
      });
    return () => {
      vivo = false;
    };
  }, [pipelineId]);

  // Devolve o foco ao gatilho ao fechar; Escape no DOCUMENTO e Tab preso na caixa — o mesmo
  // comportamento do `ConfirmarAcao`.
  useEffect(() => {
    const anterior = document.activeElement as HTMLElement | null;
    return () => anterior?.focus?.();
  }, []);
  useEffect(() => {
    const tecla = (ev: KeyboardEvent) => {
      if (ev.key === "Escape") {
        ev.stopPropagation();
        onFechar();
        return;
      }
      if (ev.key !== "Tab" || !caixaRef.current) return;
      const focaveis = caixaRef.current.querySelectorAll<HTMLElement>(
        'button:not(:disabled), input, select, textarea, a[href], [tabindex]:not([tabindex="-1"])',
      );
      if (!focaveis.length) return;
      const primeiro = focaveis[0];
      const ultimo = focaveis[focaveis.length - 1];
      const ativo = document.activeElement;
      if (ev.shiftKey && (ativo === primeiro || !caixaRef.current.contains(ativo))) {
        ev.preventDefault();
        ultimo.focus();
      } else if (!ev.shiftKey && ativo === ultimo) {
        ev.preventDefault();
        primeiro.focus();
      }
    };
    document.addEventListener("keydown", tecla, true);
    return () => document.removeEventListener("keydown", tecla, true);
  }, [onFechar]);

  const saida = saidas?.[alvo] ?? null;
  const amb = saida?.camada === "gold" ? ambiente : null;
  const resumo = descoberta ? resumoDescoberta(descoberta) : null;
  const falhou = (e: any, padrao: string) =>
    setErro({ mensagem: e?.message ?? padrao, requestId: e?.requestId });
  const esquecerPlano = () => {
    setPlano(null);
    setFeito(null);
    setErro(null);
  };

  // `itensEscolhidos` vem da linha da descoberta; sem ele, vale o que está digitado no campo.
  const consultar = async (itensEscolhidos?: string[]) => {
    if (!saida) return;
    setFeito(null);
    setErro(null);
    const lido = itensEscolhidos ? { itens: itensEscolhidos, erro: null } : lerEscopo(escopo);
    if (lido.erro) {
      setPlano(null);
      setErro({ mensagem: lido.erro });
      return;
    }
    setOcupado(true);
    try {
      setPlano(await api.consultarEscopo(saida.camada, saida.dataset, lido.itens, amb));
    } catch (e) {
      setPlano(null);
      falhou(e, "Falha ao consultar o escopo.");
    } finally {
      setOcupado(false);
    }
  };

  const mudar = async (acao: "carimbar" | "descarimbar") => {
    if (!saida || !plano) return;
    setTentou(true);
    if (!motivo.trim()) return;
    setOcupado(true);
    setErro(null);
    setFeito(null);
    try {
      const r = await api.mudarCarimboEscopo(
        acao,
        saida.camada,
        saida.dataset,
        { particao: plano.particao, motivo: motivo.trim() },
        amb,
      );
      setPlano(r);
      setTentou(false);
      setMotivo("");
      setFeito(
        r.mudou === false
          ? "Nada mudou: o escopo já estava nesse estado."
          : acao === "carimbar"
            ? "Carimbado. A leitura deste escopo passa a ser recusada (lake_escopo_obsoleto), e a trilha registrou quem e por quê."
            : "Carimbo desfeito. O ponteiro volta a ser lido como estava, e a trilha registrou quem e por quê.",
      );
    } catch (e) {
      falhou(e, acao === "carimbar" ? "Falha ao carimbar." : "Falha ao desfazer o carimbo.");
    } finally {
      setOcupado(false);
    }
  };

  const descobrir = async () => {
    if (!saida) return;
    setFeito(null);
    setErro(null);
    setOcupado(true);
    try {
      setDescoberta(await api.descobrirEscopos(saida.camada, saida.dataset, amb));
    } catch (e) {
      setDescoberta(null);
      falhou(e, "Falha ao descobrir os escopos do layout anterior.");
    } finally {
      setOcupado(false);
    }
  };

  // "Consultar" numa linha da descoberta: o escopo vai para o campo e é consultado pelo MESMO
  // caminho de quem digita — o plano mostrado é sempre o que o servidor devolveu.
  const usar = (particao: string[]) => {
    setEscopo(escopoComoTexto(particao));
    void consultar(particao);
  };

  // Carimba UM POR VEZ os escopos que o servidor liberou, cada um pela rota de sempre e para a
  // trilha com o mesmo motivo. O que falha não interrompe os outros e é dito no fim.
  const carimbarTodos = async () => {
    if (!saida || !resumo?.carimbaveis.length) return;
    setTentou(true);
    if (!motivo.trim()) return;
    const alvos = resumo.carimbaveis;
    setOcupado(true);
    setErro(null);
    setFeito(null);
    setPlano(null);
    let feitos = 0;
    const falhas: string[] = [];
    for (const particao of alvos) {
      setLote({ feitos: feitos + falhas.length, total: alvos.length });
      try {
        await api.mudarCarimboEscopo(
          "carimbar",
          saida.camada,
          saida.dataset,
          { particao, motivo: motivo.trim() },
          amb,
        );
        feitos++;
      } catch (e: any) {
        falhas.push(`${particao.join("/")}: ${e?.message ?? "falha"}`);
      }
    }
    setLote(null);
    setTentou(false);
    setMotivo("");
    setFeito(`${feitos} de ${alvos.length} escopo(s) carimbado(s); a trilha registrou cada um.`);
    if (falhas.length) setErro({ mensagem: `Não carimbados — ${falhas.join("; ")}` });
    try {
      setDescoberta(await api.descobrirEscopos(saida.camada, saida.dataset, amb));
    } catch {
      // A lista anterior fica na tela; o que não foi carimbado já está dito acima.
    }
    setOcupado(false);
  };

  const acoes = plano ? acoesDoPlano(plano) : null;
  const pedeMotivo = Boolean(acoes?.carimbar || acoes?.descarimbar || resumo?.carimbaveis.length);
  const motivoFaltando = tentou && !motivo.trim();
  return (
    <div
      className="modal-fundo"
      role="dialog"
      aria-modal="true"
      aria-labelledby={tituloId}
      data-testid="modal-carimbo"
      onClick={(e) => e.target === e.currentTarget && onFechar()}
    >
      <div
        className="modal-caixa"
        ref={caixaRef}
        style={{ width: "min(640px, 94vw)", maxHeight: "88vh", overflowY: "auto" }}
      >
        <h3 id={tituloId}>Carimbar escopo obsoleto · {pipelineNome}</h3>
        <p>
          Quando um dataset muda de partição, o ponteiro do layout <b>anterior</b> para de ser movido
          e continua legível: a fonte que ainda pede aquele escopo lê a foto do dia da migração, sem
          erro. O carimbo faz a leitura recusá-lo. Nada é apagado — <b>desfazer</b> tira só a marca.
        </p>
        {erroCarga && (
          <p className="exec-erro" role="alert">
            {erroCarga}
          </p>
        )}
        {!saidas && !erroCarga && <p role="status">Lendo a definição do pipeline…</p>}
        {saidas && saidas.length === 0 && (
          <p data-testid="carimbo-sem-saida">
            Este pipeline não publica no lake: nenhum nó <b>Saída</b> com destino lake na última
            versão. Não há ponteiro de escopo a carimbar.
          </p>
        )}
        {saidas && saida && (
          <form
            noValidate
            onSubmit={(e) => {
              e.preventDefault();
              void consultar();
            }}
            style={{ display: "grid", gap: 10, marginBottom: 12 }}
          >
            <label className="fld">
              Dataset
              <select
                className="nm"
                data-testid="carimbo-dataset"
                value={alvo}
                onChange={(e) => {
                  setAlvo(Number(e.target.value));
                  setDescoberta(null);
                  esquecerPlano();
                }}
              >
                {saidas.map((s, i) => (
                  <option key={`${s.camada}/${s.dataset}`} value={i}>
                    {s.camada}/{s.dataset}
                  </option>
                ))}
              </select>
            </label>
            {saida.camada === "gold" && (
              <label className="fld">
                Ambiente — a gold separa homologação e produção
                <select
                  className="nm"
                  data-testid="carimbo-ambiente"
                  value={ambiente}
                  onChange={(e) => {
                    setAmbiente(e.target.value);
                    setDescoberta(null);
                    esquecerPlano();
                  }}
                >
                  <option value="">— escolha —</option>
                  <option value="dev">dev (homologação)</option>
                  <option value="prd">prd (produção)</option>
                </select>
              </label>
            )}
            <small>
              Layout na última versão deste pipeline:{" "}
              <code>{saida.chaves.length ? saida.chaves.join("/") : "sem partição"}</code>. O escopo
              a carimbar é de um layout <b>anterior</b> — sem saber quais são, <b>Descobrir</b> os
              lista a partir das execuções que gravaram em outro layout.
            </small>
            <label className="fld">
              Escopo — <code>chave=valor</code>, um por linha, na ordem do caminho
              <textarea
                className="nm"
                data-testid="carimbo-escopo"
                rows={2}
                autoFocus
                value={escopo}
                placeholder="entidade_id=11"
                onChange={(e) => {
                  setEscopo(e.target.value);
                  esquecerPlano();
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                    e.preventDefault();
                    void consultar();
                  }
                }}
              />
            </label>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <button
                type="submit"
                className="btn-sec"
                data-testid="carimbo-consultar"
                disabled={ocupado}
              >
                Consultar o ponteiro
              </button>
              <button
                type="button"
                className="btn-sec"
                data-testid="carimbo-descobrir"
                disabled={ocupado}
                onClick={() => void descobrir()}
              >
                Descobrir escopos do layout anterior
              </button>
            </div>
          </form>
        )}
        {descoberta && resumo && (
          <div
            data-testid="carimbo-descobertos"
            style={{
              border: "1px solid var(--hair)",
              borderRadius: 8,
              padding: "10px 12px",
              marginBottom: 12,
              fontSize: 13,
            }}
          >
            <p style={{ margin: "0 0 6px" }} data-testid="carimbo-descobertos-resumo">
              {resumo.texto}
            </p>
            {resumo.avisos.map((aviso) => (
              <p key={aviso} className="exec-erro" style={{ margin: "0 0 6px" }}>
                {aviso}
              </p>
            ))}
            {descoberta.escopos.length > 0 && (
              <div className="dgrid-wrap" style={{ maxHeight: 220, overflowY: "auto" }}>
                <table className="dgrid">
                  <thead>
                    <tr>
                      <th>Escopo</th>
                      <th>Estado</th>
                      <th>Aponta para</th>
                      <th aria-label="Ações" />
                    </tr>
                  </thead>
                  <tbody>
                    {descoberta.escopos.map((e) => (
                      <tr key={e.caminho} data-testid="carimbo-descoberto">
                        <td>
                          <code>{e.particao.join("/")}</code>
                        </td>
                        <td>
                          <span className="statuspill off">
                            <span className="d" />
                            {e.estado}
                          </span>
                        </td>
                        <td>
                          {e.aponta?.execucao_id ? (
                            <code title={e.aponta.execucao_id}>{e.aponta.execucao_id.slice(0, 8)}</code>
                          ) : (
                            "—"
                          )}
                          {e.aponta?.momento ? ` · ${momento(e.aponta.momento)}` : ""}
                        </td>
                        <td>
                          <button
                            type="button"
                            className="btn-sec"
                            disabled={ocupado}
                            onClick={() => usar(e.particao)}
                          >
                            Consultar
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
        {plano && (
          <div
            data-testid="carimbo-plano"
            style={{
              border: "1px solid var(--hair)",
              borderRadius: 8,
              padding: "10px 12px",
              marginBottom: 12,
              fontSize: 13,
            }}
          >
            <div style={{ display: "grid", gridTemplateColumns: "max-content 1fr", gap: "4px 12px" }}>
              <span>Estado</span>
              <span data-testid="carimbo-estado">
                <span className="statuspill off">
                  <span className="d" />
                  {plano.estado}
                </span>{" "}
                {rotuloEstado(plano.estado)}
              </span>
              <span>Ponteiro</span>
              <code>
                {plano.camada}/{plano.caminho}
              </code>
              {plano.aponta?.execucao_id && (
                <>
                  <span>Aponta para</span>
                  <span>
                    execução <code title={plano.aponta.execucao_id}>{plano.aponta.execucao_id.slice(0, 8)}</code>
                    {plano.aponta.linhas != null ? ` · ${plano.aponta.linhas} linha(s)` : ""}
                    {plano.aponta.momento ? ` · publicada ${momento(plano.aponta.momento)}` : ""}
                  </span>
                </>
              )}
              <span>Layout vigente</span>
              <span>{plano.layout_vigente ? <code>{plano.layout_vigente.join("/")}</code> : "—"}</span>
              {plano.carimbo && (
                <>
                  <span>Carimbo</span>
                  <span>
                    {momento(plano.carimbo.desde)}
                    {plano.carimbo.motivo ? ` — ${plano.carimbo.motivo}` : ""}
                  </span>
                </>
              )}
            </div>
            {acoes?.motivo && (
              <p data-testid="carimbo-bloqueio" style={{ margin: "8px 0 0" }}>
                {acoes.motivo}
              </p>
            )}
          </div>
        )}
        {pedeMotivo && (
          <label className="fld" style={{ display: "block", marginBottom: 12 }}>
            Motivo — vai para a trilha de auditoria e para o próprio ponteiro
            <input
              className="nm"
              data-testid="carimbo-motivo"
              value={motivo}
              aria-invalid={motivoFaltando}
              placeholder="ex.: a partição do dataset passou a incluir o as-of"
              onChange={(e) => setMotivo(e.target.value)}
            />
            {motivoFaltando && (
              <small className="exec-erro" data-testid="carimbo-motivo-faltando">
                Diga por quê: a trilha é permanente.
              </small>
            )}
          </label>
        )}
        {erro && (
          <p className="exec-erro" role="alert" data-testid="carimbo-erro">
            {erro.mensagem}
            {erro.requestId ? ` · código de suporte ${erro.requestId}` : ""}
          </p>
        )}
        {feito && (
          <p role="status" data-testid="carimbo-feito">
            {feito}
          </p>
        )}
        {lote && (
          <p role="status" data-testid="carimbo-lote">
            Carimbando {lote.feitos + 1} de {lote.total}…
          </p>
        )}
        <div className="modal-acoes">
          <button type="button" className="btn-sec" onClick={onFechar}>
            Fechar
          </button>
          {resumo && resumo.carimbaveis.length > 0 && (
            <button
              type="button"
              className="btn-perigo"
              data-testid="carimbo-carimbar-todos"
              disabled={ocupado}
              onClick={() => void carimbarTodos()}
            >
              {resumo.carimbaveis.length === 1
                ? "Carimbar o escopo liberado"
                : `Carimbar os ${resumo.carimbaveis.length} escopos liberados`}
            </button>
          )}
          {acoes?.descarimbar && (
            <button
              type="button"
              className="btn-stamp"
              data-testid="carimbo-desfazer"
              disabled={ocupado}
              onClick={() => void mudar("descarimbar")}
            >
              Desfazer carimbo
            </button>
          )}
          {acoes?.carimbar && (
            <button
              type="button"
              className="btn-perigo"
              data-testid="carimbo-confirmar"
              disabled={ocupado}
              onClick={() => void mudar("carimbar")}
            >
              Carimbar como obsoleto
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
