// Tela própria "Fontes de Ingestão" — cadastro, edição de contrato, backfill e replay via UI
// (ADR a registrar: reverte a decisão anterior de manter essas quatro ações fora do Studio).
//
// Fonte de ingestão continua NÃO sendo pipeline: sem grafo, sem versão, sem parâmetro. O que mudou
// é que ela ganhou EDITOR — o contrato é formulário, não script. RBAC de borda não muda (as 4 rotas
// seguem admin-only); só a superfície de acesso muda, com guardrail proporcional ao risco:
// cadastro/edição não tocam rede (confirmação simples); backfill/replay tocam (danger zone,
// `ConfirmarBackfillReplay`, nomeando o efeito antes de disparar).
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import * as api from "../../api/client";
import { fmtMomentoCurto, num } from "../execucao/formato";
import { ConfirmarAcao, GridLista, MenuAcoes, type ItemCadastro } from "../ui";
import ConfirmarBackfillReplay from "./ConfirmarBackfillReplay";
import FormaContratoFonte from "./FormaContratoFonte";
import {
  abaValida,
  abasDeClasse,
  contarFalhas,
  contarFatiasSemBronze,
  contarFontesComBuraco,
  daAbaClasse,
  indexarQuarentena,
  metaFonte,
  metaQuarentena,
  ordenarFontes,
  pillQuarentena,
  ultimaExecucaoFonte,
  type ItemQuarentena,
  type ResumoQuarentena,
} from "./ingestao";

// As duas rotas de quarentena ainda não estão em `api/client.ts` — mesmo `apiFetch` autenticado que
// o bloco original da aba Bronze já usava.
async function buscarJson(caminho: string): Promise<any> {
  const r = await api.apiFetch("GET", caminho);
  if (!r.ok) {
    const j = await r.json().catch(() => null);
    throw Object.assign(new Error(j?.erro?.mensagem ?? "Falha ao carregar a quarentena."), {
      codigo: j?.erro?.codigo as string | undefined,
      requestId: j?.erro?.request_id as string | undefined,
    }) as api.ErroApi;
  }
  return r.json();
}

type Acao = { tipo: "backfill" | "replay"; fonte: api.FonteLake };

export default function FontesIngestaoTela() {
  const [params, setParams] = useSearchParams();
  const [criando, setCriando] = useState(false);
  const [editandoId, setEditandoId] = useState<string | null>(null);

  const [fontes, setFontes] = useState<api.FonteLake[]>([]);
  const [carregando, setCarregando] = useState(true);
  const [erro, setErro] = useState<api.ErroApi | null>(null);

  const [pedindoExecutar, setPedindoExecutar] = useState<api.FonteLake | null>(null);
  const [pedindoAtivo, setPedindoAtivo] = useState<api.FonteLake | null>(null);
  const [acao, setAcao] = useState<Acao | null>(null);
  const [disparando, setDisparando] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [erroAcao, setErroAcao] = useState<api.ErroApi | null>(null);

  const [resumos, setResumos] = useState<ResumoQuarentena[] | null>(null);
  const [detalhe, setDetalhe] = useState<api.FonteLake | null>(null);
  const [itensQ, setItensQ] = useState<ItemQuarentena[]>([]);
  const [carregandoQ, setCarregandoQ] = useState(false);
  const [erroQ, setErroQ] = useState<api.ErroApi | null>(null);
  const tituloDetalhe = useRef<HTMLHeadingElement | null>(null);

  const noFormulario = criando || editandoId !== null;

  const claseFiltro = params.get("c") ?? "";
  const trocarFiltro = useCallback(
    (chave: string, valor: string) => {
      const p = new URLSearchParams(params);
      if (valor) p.set(chave, valor);
      else p.delete(chave);
      setParams(p, { replace: true });
    },
    [params, setParams],
  );

  const carregar = useCallback(async () => {
    setCarregando(true);
    setErro(null);
    try {
      setFontes(await api.listarFontesIngestao());
    } catch (e) {
      setErro(e as api.ErroApi);
    } finally {
      setCarregando(false);
    }
    try {
      const corpo = await buscarJson("/ingestao/quarentena/resumo");
      setResumos(Array.isArray(corpo?.fontes) ? corpo.fontes : []);
    } catch {
      setResumos(null);
    }
  }, []);

  useEffect(() => {
    if (!noFormulario) void carregar();
  }, [noFormulario, carregar]);

  const abrirQuarentena = useCallback(async (f: api.FonteLake) => {
    setDetalhe(f);
    setItensQ([]);
    setErroQ(null);
    setCarregandoQ(true);
    try {
      const corpo = await buscarJson(`/ingestao/quarentena?fonte_id=${encodeURIComponent(f.id)}`);
      setItensQ(Array.isArray(corpo?.itens) ? corpo.itens : []);
    } catch (e) {
      setErroQ(e as api.ErroApi);
    } finally {
      setCarregandoQ(false);
    }
  }, []);

  useEffect(() => {
    if (detalhe) tituloDetalhe.current?.focus();
  }, [detalhe]);

  const executar = async (f: api.FonteLake) => {
    setDisparando(f.id);
    setErroAcao(null);
    setMsg(null);
    try {
      const r = await api.executarIngestao(f.id);
      const fatias = Array.isArray(r.fatias) ? r.fatias.length : null;
      const janela = r.janela ? ` na janela de ${r.janela.inicio} a ${r.janela.fim}` : "";
      const fatiasTxt = fatias === null ? "" : ` — ${num(fatias)} ${fatias === 1 ? "fatia" : "fatias"}${janela}`;
      setMsg(
        r.status === "executando"
          ? `Ingestão de ${f.nome} iniciada${fatiasTxt}. Acompanhe o estado nesta lista.`
          : `Ingestão de ${f.nome} concluída${fatiasTxt}.`,
      );
      await carregar();
    } catch (e) {
      setErroAcao(e as api.ErroApi);
    } finally {
      setDisparando(null);
    }
  };

  const alternarAtivo = async (f: api.FonteLake, novo: boolean) => {
    setErroAcao(null);
    try {
      await api.editarFonteIngestao(f.id, { ativo: novo });
      setMsg(`${f.nome} ${novo ? "reativada" : "desativada"}.`);
      await carregar();
    } catch (e) {
      setErroAcao(e as api.ErroApi);
    }
  };

  const ordenadas = useMemo(() => ordenarFontes(fontes), [fontes]);
  const falhas = useMemo(() => contarFalhas(fontes), [fontes]);
  const inativas = useMemo(() => fontes.filter((f) => f.ativo === false).length, [fontes]);
  const porFonte = useMemo(() => indexarQuarentena(resumos ?? []), [resumos]);
  const comBuraco = useMemo(() => contarFontesComBuraco(resumos ?? []), [resumos]);
  const fatiasSemBronze = useMemo(() => contarFatiasSemBronze(resumos ?? []), [resumos]);

  const abas = useMemo(() => abasDeClasse(fontes), [fontes]);
  const aba = useMemo(() => abaValida(claseFiltro, abas.map((a) => a.chave)), [claseFiltro, abas]);
  const visiveis = useMemo(
    () => ordenadas.filter((f) => daAbaClasse(f, aba, abas.map((a) => a.chave))),
    [ordenadas, aba, abas],
  );

  if (criando) {
    return (
      <FormaContratoFonte
        onVoltar={() => setCriando(false)}
        onSalvo={() => {
          /* fica no formulário mostrando "Fonte cadastrada." — mesmo padrão de `NovaConexao`
             (criadaId): sair automaticamente cortaria a confirmação antes de a pessoa vê-la.
             `carregar()` já roda de novo assim que `noFormulario` vira falso, ao voltar. */
        }}
      />
    );
  }
  if (editandoId) {
    return (
      <FormaContratoFonte
        editarId={editandoId}
        onVoltar={() => setEditandoId(null)}
        onSalvo={() => {
          /* segue no formulário — a confirmação já diz "salvo"; sair é escolha da pessoa (mesmo
             comportamento de edição de conexão) */
        }}
      />
    );
  }

  const itens: ItemCadastro[] = visiveis.map((f): ItemCadastro => {
    const ex = ultimaExecucaoFonte(f);
    const q = pillQuarentena(porFonte[f.id]);
    const campos = [
      f.sistema === true
        ? {
            rotulo: "Manutenção",
            valor: (
              <span className="statuspill off" title="Fonte semeada do catálogo da plataforma.">
                <span className="d" />
                plataforma
              </span>
            ),
          }
        : null,
      f.ativo === false
        ? {
            rotulo: "Estado",
            valor: (
              <span className="statuspill off" title="Não entra na recarga automática.">
                <span className="d" />
                desativada
              </span>
            ),
          }
        : null,
      q
        ? {
            rotulo: "Quarentena",
            valor: (
              <span className={`statuspill ${q.cls}`} title={q.titulo}>
                <span className="d" />
                {q.texto}
              </span>
            ),
          }
        : null,
    ].filter((c): c is NonNullable<typeof c> => c !== null);
    return {
      id: f.id,
      nome: f.nome,
      meta: <span title={`${ex.texto} · dataset ${f.dataset}`}>{`${metaFonte(f)} · ${ex.texto}`}</span>,
      status: ex.status,
      scls: ex.scls,
      statusRotulo: "Última execução",
      campos,
      testid: `ing-linha-${f.dataset}`,
    };
  });

  const total = fontes.length;

  return (
    <>
      {pedindoExecutar && (
        <ConfirmarAcao
          titulo="Executar a ingestão"
          corpo={
            <>
              Baixa da origem e grava no lake a janela pendente de <b>{pedindoExecutar.nome}</b>{" "}
              (dataset <b>{pedindoExecutar.dataset}</b>), partindo de{" "}
              {pedindoExecutar.watermark ? (
                <>
                  onde a última execução parou (<b>{pedindoExecutar.watermark}</b>)
                </>
              ) : (
                <>o início declarado no contrato da fonte</>
              )}{" "}
              até hoje. É uma requisição por fatia à origem pública.
              {pedindoExecutar.ativo === false && (
                <>
                  {" "}
                  Esta fonte está <b>desativada</b>: ela não entra na recarga automática, mas este
                  disparo manual roda.
                </>
              )}
            </>
          }
          rotuloConfirmar="Executar"
          onConfirmar={() => {
            void executar(pedindoExecutar);
            setPedindoExecutar(null);
          }}
          onCancelar={() => setPedindoExecutar(null)}
        />
      )}
      {pedindoAtivo && (
        <ConfirmarAcao
          titulo={pedindoAtivo.ativo === false ? "Reativar fonte" : "Desativar fonte"}
          corpo={
            pedindoAtivo.ativo === false ? (
              <>
                <b>{pedindoAtivo.nome}</b> volta a entrar na recarga automática.
              </>
            ) : (
              <>
                <b>{pedindoAtivo.nome}</b> deixa de entrar na recarga automática. O disparo manual
                continua valendo, e pipelines que já leem o dataset continuam lendo.
              </>
            )
          }
          rotuloConfirmar={pedindoAtivo.ativo === false ? "Reativar" : "Desativar"}
          onConfirmar={() => {
            void alternarAtivo(pedindoAtivo, pedindoAtivo.ativo === false);
            setPedindoAtivo(null);
          }}
          onCancelar={() => setPedindoAtivo(null)}
        />
      )}
      {acao && (
        <ConfirmarBackfillReplay
          fonte={acao.fonte}
          tipo={acao.tipo}
          onFeito={() => {
            setAcao(null);
            setMsg(`${acao.tipo === "backfill" ? "Backfill" : "Replay"} de ${acao.fonte.nome} disparado.`);
            void carregar();
          }}
          onCancelar={() => setAcao(null)}
        />
      )}

      <div className="page-head">
        <h1 className="display">Fontes de ingestão</h1>
        <p className="sub">
          Trazem dado público de fora (CVM, BCB, B3, ANBIMA) para o lake, na camada bronze — é delas
          que o nó <b>fonte lake</b> lê. Não têm grafo nem versão: o contrato (URL, formato, janela)
          é este formulário, versionado por edição auditada, não um pipeline.
        </p>
      </div>

      <div className="pad">
        {erro && (
          <div className="ing-aviso erro" role="alert" data-testid="ing-erro">
            {erro.message}
            {erro.requestId && (
              <span style={{ display: "block", fontSize: 11.5 }}>Código para suporte: {erro.requestId}</span>
            )}{" "}
            <button type="button" className="lk" onClick={() => void carregar()}>
              tentar novamente
            </button>
          </div>
        )}
        {erroAcao && (
          <div className="ing-aviso erro" role="alert" data-testid="ing-erro-acao">
            {erroAcao.message}
            {erroAcao.requestId && (
              <span style={{ display: "block", fontSize: 11.5 }}>Código para suporte: {erroAcao.requestId}</span>
            )}
          </div>
        )}
        {msg && (
          <div className="ing-aviso ok" role="status" data-testid="ing-msg">
            {msg}
          </div>
        )}

        <div className="lc-bar">
          <span data-testid="ing-contagem">
            {carregando
              ? "carregando fontes de ingestão…"
              : erro && total === 0
                ? "não foi possível carregar as fontes de ingestão"
                : `${num(total)} ${total === 1 ? "fonte de ingestão" : "fontes de ingestão"}`}
            {falhas > 0 && ` · ${num(falhas)} com falha na última execução`}
            {inativas > 0 && ` · ${num(inativas)} desativada${inativas === 1 ? "" : "s"}`}
            {comBuraco > 0 &&
              ` · ${num(comBuraco)} com ${num(fatiasSemBronze)} ${
                fatiasSemBronze === 1 ? "fatia que nunca pousou" : "fatias que nunca pousaram"
              } no bronze`}
            {!carregando && resumos === null && " · quarentena não pôde ser consultada"}
          </span>
          <button
            className="btn-stamp"
            style={{ marginLeft: "auto" }}
            data-testid="nova-fonte"
            onClick={() => setCriando(true)}
          >
            + Nova fonte
          </button>
        </div>

        {abas.length > 2 && (
          <div className="lc-bar" style={{ marginTop: -6 }}>
            {abas.map((a) => (
              <button
                key={a.chave}
                type="button"
                className={"chip" + (aba === a.chave ? " on" : "")}
                aria-pressed={aba === a.chave}
                onClick={() => trocarFiltro("c", a.chave)}
              >
                {a.rotulo} ({a.contagem})
              </button>
            ))}
          </div>
        )}

        {detalhe && (
          <section
            className="card pad"
            aria-labelledby="ing-q-tit"
            data-testid="ing-quarentena-painel"
            style={{ marginBottom: 14 }}
          >
            <div style={{ display: "flex", alignItems: "baseline", gap: 10, flexWrap: "wrap" }}>
              <h3 id="ing-q-tit" ref={tituloDetalhe} tabIndex={-1} style={{ margin: 0 }}>
                Quarentena de {detalhe.nome}
              </h3>
              <button type="button" className="lk" onClick={() => setDetalhe(null)}>
                fechar
              </button>
            </div>
            <p className="ing-sub" style={{ margin: "8px 0 12px" }}>
              O que a origem entregou e a ingestão recusou: falha de DADO (download, parse, schema),
              que manda a fatia para a quarentena sem parar as demais. O payload cru fica guardado no
              lake — a URI abaixo diz onde. Ele <b>não</b> é baixável por aqui.
            </p>

            {erroQ && (
              <div className="ing-aviso erro" role="alert" data-testid="ing-quarentena-erro">
                {erroQ.message}{" "}
                <button type="button" className="lk" onClick={() => void abrirQuarentena(detalhe)}>
                  tentar novamente
                </button>
              </div>
            )}

            {carregandoQ ? (
              <div role="status">Carregando a quarentena…</div>
            ) : itensQ.length === 0 && !erroQ ? (
              <div role="status">Nenhuma linha na quarentena desta fonte.</div>
            ) : (
              <div className="dgrid-wrap" tabIndex={0} role="region" aria-label={`Quarentena de ${detalhe.nome}`}>
                <table className="dgrid">
                  <thead>
                    <tr>
                      <th>Fatia · etapa · código</th>
                      <th>Motivo</th>
                      <th>Payload cru</th>
                      <th>Quando (GMT-3)</th>
                    </tr>
                  </thead>
                  <tbody>
                    {itensQ.map((it) => (
                      <tr key={it.id}>
                        <td className="dg-nome">
                          {it.chave_lote || "fatia sem chave"}
                          <span className="dg-sub">{metaQuarentena(it)}</span>
                        </td>
                        <td>{it.mensagem || "—"}</td>
                        <td>
                          <code style={{ fontSize: 11.5, wordBreak: "break-all" }}>
                            {it.uri || "não guardado"}
                          </code>
                        </td>
                        <td title={it.criado_em ?? undefined}>{fmtMomentoCurto(it.criado_em)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        )}

        <GridLista
          rotulo="Fontes de ingestão"
          itens={itens}
          carregando={carregando}
          erro={!!erro}
          onAbrir={(id) => setEditandoId(id)}
          acoes={(it) => {
            const f = fontes.find((x) => x.id === it.id);
            if (!f) return null;
            const temQ = (porFonte[f.id]?.total ?? 0) > 0;
            return (
              <>
                <button data-testid={`ing-editar-${f.dataset}`} onClick={() => setEditandoId(f.id)}>
                  Editar contrato
                </button>
                <MenuAcoes rotulo={`Mais ações de ${f.nome}`}>
                  <button
                    data-testid={`ing-executar-${f.dataset}`}
                    disabled={disparando === f.id}
                    onClick={() => setPedindoExecutar(f)}
                  >
                    {disparando === f.id ? "Executando…" : "Executar (incremental)"}
                  </button>
                  <button data-testid={`ing-backfill-${f.dataset}`} onClick={() => setAcao({ tipo: "backfill", fonte: f })}>
                    Backfill…
                  </button>
                  <button data-testid={`ing-replay-${f.dataset}`} onClick={() => setAcao({ tipo: "replay", fonte: f })}>
                    Replay…
                  </button>
                  <button data-testid={`ing-ativo-${f.dataset}`} onClick={() => setPedindoAtivo(f)}>
                    {f.ativo === false ? "Reativar" : "Desativar"}
                  </button>
                  {temQ && (
                    <button data-testid={`ing-quarentena-${f.dataset}`} onClick={() => void abrirQuarentena(f)}>
                      Ver a quarentena
                    </button>
                  )}
                </MenuAcoes>
              </>
            );
          }}
          vazio={
            <div className="card lc-empty">
              <div className="big">Nenhuma fonte de ingestão cadastrada</div>
              <div>
                Clique <b>+ Nova fonte</b> para cadastrar a primeira.
              </div>
            </div>
          }
        />
      </div>
    </>
  );
}
