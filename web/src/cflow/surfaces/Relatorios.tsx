import { type ReactNode, useCallback, useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { Bloco, GridLista, Icon, type ItemCadastro } from "../ui";
import * as api from "../../api/client";
import type { RelatorioResumo } from "../../api/client";
import { SeletorPeriodo } from "../SeletorPeriodo";
import { resolverJanela } from "../periodos";
import {
  celula as celulaDado,
  diaSP,
  fmtHora,
  fmtMomento,
  num,
  perfilColuna,
  rotuloDia,
  type PerfilColuna,
} from "../execucao/formato";

// Relatórios (E10): catálogo de relatórios CARIMBADOS (imutáveis — só leitura + export).
//
// O que a tela precisa deixar claro, porque é EVIDÊNCIA e não painel:
//  • o número é a foto de um instante (data/hora com fuso explícito), não dado ao vivo;
//  • de onde cada número veio (view → cubo → execução → nó), com link para a execução;
//  • quando o dado exibido está truncado na tela (o Excel leva o conteúdo inteiro).
// Nada de editar/excluir: o carimbo é append-only (o papel da aplicação só tem SELECT/INSERT).

interface Origem {
  snapshot?: string;
  execucao_id?: string;
  no_id?: string;
  hash?: string;
}

interface Resultado {
  colunas: string[];
  linhas: Record<string, unknown>[];
  layout?: { linhas?: string[]; colunas?: string[]; valores?: string[] };
  dimensoes?: string[];
  medidas?: string[];
  origem?: Origem;
}

interface BlocoConsulta {
  titulo?: string;
  tipo?: string;
  view_id: string;
  // Ligado por bloco na definição da página e RENDERIZADO pelo Portal — o carimbo mostrava menos
  // do que o operador viu quando pediu o relatório.
  total?: boolean;
  resultado: Resultado;
}

interface TraceItem {
  view_id: string;
  origem?: Origem;
}

interface FiltroCarimbado {
  dimensao: string;
  op?: string;
  valor?: unknown;
}

interface RelatorioDetalhe {
  id: string;
  pagina?: string;
  pagina_id?: string;
  versao?: number;
  gerado_em?: string | null;
  criado_por?: string | null;
  filtros?: FiltroCarimbado[] | null;
  blocos: BlocoConsulta[];
  trace?: TraceItem[];
}

// Mesma paleta do preview da Página — blocos idênticos entre telas. A 1ª cor é o token do tema:
// #002050 fixo desaparecia no fundo escuro e o bloco ficava sem faixa de cor.
const LINHA_CORES = ["var(--navy)", "#5f8a2e", "#46566b", "#0a2f63", "#b23b43"] as const;

// Quantas linhas de um bloco a tela desenha antes de pedir confirmação. Um bloco de 50 mil linhas
// travava a página; e truncar em silêncio seria pior que truncar dizendo.
const LINHAS_POR_BLOCO = 200;

// Operador do filtro carimbado. Sem isto a tela mostrava "valor 100" para um filtro
// "valor > 100" — descrevia errado o recorte do relatório.
const ROTULO_OP: Record<string, string> = {
  igual: "=",
  diferente: "≠",
  maior: ">",
  menor: "<",
  maior_igual: "≥",
  menor_igual: "≤",
  contem: "contém",
};

function curto(id?: string): string {
  return id ? id.slice(0, 8) : "—";
}

// Texto de apoio que CARREGA informação: --ink-2 (7,2:1). `.rzhint` é inline-flex com gap — com
// <button> dentro, a frase virava itens separados por vãos.
const NOTA: React.CSSProperties = { fontSize: 11.5, color: "var(--ink-2)", lineHeight: 1.45 };

// Coluna de view tem rótulo HUMANO ("NÚMERO DA VENDA"), não snake_case. `perfilColuna` decide
// "identificador não leva separador de milhar" pelo NOME — sem normalizar, o número da venda
// aparecia como "39.114". A normalização vale só para essa decisão; o rótulo exibido é o original.
function perfisDaView(cols: string[], linhas: Record<string, unknown>[]): Record<string, PerfilColuna> {
  const out: Record<string, PerfilColuna> = {};
  for (const c of cols) {
    const chave = c
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "")
      .replace(/[^A-Za-z0-9]+/g, "_")
      .toLowerCase();
    out[c] = perfilColuna(
      linhas.map((l) => l[c]),
      chave,
    );
  }
  return out;
}

const ehKpi = (t?: string) => ["kpi", "valor"].includes((t ?? "").toLowerCase());
const ehLargo = (t?: string) => ["observacoes", "detalhe", "tabela"].includes((t ?? "").toLowerCase());

// Célula do dado: perfil por COLUNA (tipo + casas decimais uniformes), DECIMAL (que chega como
// texto) sem perder dígito, identificador sem separador de milhar, NULL distinguível de vazio.
// Mesma régua da grade de Execuções (`execucao/formato.ts`).
function renderCelula(v: unknown, perfil?: PerfilColuna): ReactNode {
  if (typeof v === "boolean") {
    // `aria-label` porque `title` não é anunciado de forma confiável; tons dos pills de status (AA).
    return (
      <span aria-label={v ? "Sim" : "Não"} style={{ color: v ? "#2f6b46" : "#a8353d", fontWeight: 800, fontSize: 14 }}>
        {v ? "✓" : "✗"}
      </span>
    );
  }
  const cel = celulaDado(v, perfil);
  const titulo = cel.titulo ?? (cel.texto !== String(v) ? String(v) : undefined);
  if (cel.vazio) return <span className="cel-nula" title={titulo}>{cel.texto}</span>;
  return <span title={titulo}>{cel.texto}</span>;
}

// Linha de Total (rodapé) — soma por COLUNA numérica, igual ao Portal. Aceita texto numérico
// (DECIMAL vem como string; somar só `typeof number` zerava a coluna).
function linhaTotal(
  mostrar: boolean | undefined,
  cols: string[],
  linhas: Record<string, unknown>[],
  perfis: Record<string, PerfilColuna>,
): ReactNode[] | undefined {
  if (!mostrar) return undefined;
  return cols.map((c, ci) => {
    if (perfis[c]?.tipo !== "numero") return <b key={c}>{ci === 0 ? "Total" : ""}</b>;
    const soma = linhas.reduce((s, l) => {
      const v = l[c];
      const n = typeof v === "number" ? v : typeof v === "string" && v !== "" ? Number(v) : NaN;
      return s + (Number.isFinite(n) ? n : 0);
    }, 0);
    return <b key={c}>{renderCelula(soma, perfis[c])}</b>;
  });
}

// Número do cartão-KPI: soma da 1ª medida da view (a MESMA conta do Portal), com o rótulo dizendo
// qual medida é e sobre quantas linhas ela soma.
function kpiDoBloco(res: Resultado, tituloBloco: string): { label: string; val: string } {
  const medidas = res.medidas ?? res.layout?.valores ?? [];
  const linhas = res.linhas ?? [];
  const medida = medidas[0];
  if (!medida) return { label: tituloBloco, val: "—" };
  let soma = 0;
  for (const l of linhas) {
    const v = l[medida];
    const n = typeof v === "number" ? v : typeof v === "string" && v !== "" ? Number(v) : NaN;
    if (Number.isFinite(n)) soma += n;
  }
  const perfil = perfisDaView([medida], linhas)[medida];
  return {
    label: `${medida}${linhas.length > 1 ? ` · soma de ${num(linhas.length)} linhas` : ""}`,
    val: celulaDado(soma, perfil).texto,
  };
}

function fmtValorFiltro(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  return celulaDado(v).texto;
}

// Dispara o download de um Blob no navegador (cria <a download> temporário).
function baixarBlob(blob: Blob, nome: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = nome;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export default function Relatorios() {
  const navigate = useNavigate();
  // Relatório aberto e filtros vivem na URL: o carimbo é evidência que se compartilha por link, e
  // o voltar do navegador precisa funcionar.
  const [params, setParams] = useSearchParams();
  const idAberto = params.get("id");
  const fPagina = params.get("pag") ?? "";
  const fPeriodo = params.get("per") ?? "";

  const [lista, setLista] = useState<RelatorioResumo[]>([]);
  const [aberto, setAberto] = useState<RelatorioDetalhe | null>(null);
  const [carregandoLista, setCarregandoLista] = useState(true);
  const [carregandoDet, setCarregandoDet] = useState(false);
  const [exportando, setExportando] = useState(false);
  const [erro, setErro] = useState("");
  const [erroDet, setErroDet] = useState("");
  const [expandido, setExpandido] = useState<Record<string, boolean>>({});
  // Exportação disparada de uma LINHA do catálogo (sem abrir o carimbo).
  const [exportandoId, setExportandoId] = useState<string | null>(null);

  const carregarLista = useCallback(() => {
    setCarregandoLista(true);
    setErro("");
    api
      .listarRelatorios()
      .then(setLista)
      .catch((e: any) => setErro(e?.message ?? "Falha ao carregar relatórios."))
      .finally(() => setCarregandoLista(false));
  }, []);

  useEffect(() => carregarLista(), [carregarLista]);

  // Abre/fecha pelo id da URL (deep-link, F5 e voltar do navegador incluídos).
  useEffect(() => {
    if (!idAberto) {
      setAberto(null);
      setErroDet("");
      return;
    }
    if (aberto?.id === idAberto) return;
    let vivo = true;
    setCarregandoDet(true);
    setErroDet("");
    setExpandido({});
    api
      .obterRelatorio(idAberto)
      .then((d) => vivo && setAberto(d as RelatorioDetalhe))
      .catch((e: any) => vivo && setErroDet(e?.message ?? "Falha ao abrir relatório."))
      .finally(() => vivo && setCarregandoDet(false));
    return () => {
      vivo = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [idAberto]);

  const trocarFiltro = (chave: string, valor: string) => {
    const p = new URLSearchParams(params);
    if (valor) p.set(chave, valor);
    else p.delete(chave);
    setParams(p, { replace: true });
  };

  const abrir = (id: string, rastro = false) => {
    const p = new URLSearchParams(params);
    p.set("id", id);
    if (rastro) p.set("t", "rastro");
    else p.delete("t");
    setParams(p);
  };

  // Exporta direto da linha: o Excel é o destino mais comum do carimbo, e ele não exige abrir o
  // relatório (o conteúdo é imutável — o que a linha promete é o que o arquivo traz).
  async function exportarLinha(id: string) {
    setExportandoId(id);
    setErro("");
    try {
      baixarBlob(await api.baixarRelatorioXlsx(id), `relatorio-${curto(id)}.xlsx`);
    } catch (e: any) {
      setErro(e?.message ?? "Falha ao exportar Excel.");
    } finally {
      setExportandoId(null);
    }
  }

  const fechar = () => {
    const p = new URLSearchParams(params);
    p.delete("id");
    p.delete("t");
    setParams(p);
  };

  // Deep-link para a cadeia de rastreio (`?id=…&t=rastro`): quem chega pela lista querendo
  // "de onde veio este número" cai direto no bloco, sem rolar o carimbo inteiro.
  useEffect(() => {
    if (!aberto || params.get("t") !== "rastro") return;
    document.getElementById("rastreio")?.scrollIntoView({ block: "start" });
  }, [aberto, params]);

  async function exportar() {
    if (!aberto) return;
    setExportando(true);
    setErroDet("");
    try {
      const blob = await api.baixarRelatorioXlsx(aberto.id);
      baixarBlob(blob, `relatorio-${curto(aberto.id)}.xlsx`);
    } catch (e: any) {
      setErroDet(e?.message ?? "Falha ao exportar Excel.");
    } finally {
      setExportando(false);
    }
  }

  // ── DETALHE ──────────────────────────────────────────────────────────────
  if (idAberto) {
    if (carregandoDet && !aberto) {
      return (
        <>
          <div className="page-head">
            <button className="link-back" onClick={fechar}>
              ← Relatórios
            </button>
            <h1 className="display">Abrindo o carimbo…</h1>
          </div>
          <div className="pad">
            <div className="card exec-lista-vazia">Carregando o relatório carimbado…</div>
          </div>
        </>
      );
    }
    if (!aberto) {
      return (
        <>
          <div className="page-head">
            <button className="link-back" onClick={fechar}>
              ← Relatórios
            </button>
            <h1 className="display">Relatório</h1>
          </div>
          <div className="pad">
            <div className="card exec-erro-lista" role="alert">
              {erroDet || "Relatório não encontrado."}
            </div>
          </div>
        </>
      );
    }

    const blocos = aberto.blocos ?? [];
    const filtros = aberto.filtros ?? [];
    const totalLinhas = blocos.reduce((s, b) => s + (b.resultado?.linhas?.length ?? 0), 0);
    const execucoes = [
      ...new Set(
        blocos
          .map((b, i) => b.resultado?.origem?.execucao_id ?? aberto.trace?.[i]?.origem?.execucao_id)
          .filter((x): x is string => !!x),
      ),
    ];

    // Um painel de bloco (cabeçalho colorido + grade + procedência), reusado pelas três formas.
    const painel = (b: BlocoConsulta, i: number, largo: boolean) => {
      const r = b.resultado ?? { colunas: [], linhas: [] };
      const cols = r.colunas ?? [];
      const todas = r.linhas ?? [];
      const chave = `${b.view_id}:${i}`;
      const mostrarTudo = !!expandido[chave];
      const linhas = mostrarTudo ? todas : todas.slice(0, LINHAS_POR_BLOCO);
      const perfis = perfisDaView(cols, todas);
      const colsNum = new Set<number>();
      cols.forEach((c, j) => perfis[c]?.tipo === "numero" && colsNum.add(j));
      const cor = LINHA_CORES[i % LINHA_CORES.length];
      const origem = r.origem ?? aberto.trace?.[i]?.origem;
      const bv = {
        cols,
        mini: linhas.map((l) => cols.map((c) => renderCelula(l[c], perfis[c]))),
        kpi: kpiDoBloco(r, b.titulo ?? `Bloco ${i + 1}`),
      };
      return (
        <div className={"mpanel" + (largo ? " obs-card" : "")} key={chave}>
          <div className="pbh" style={{ cursor: "default", borderTop: "3px solid " + cor }}>
            <span className="vd" style={{ background: cor }} />
            <span className="pbt">{b.titulo ?? `Bloco ${i + 1}`}</span>
            <span className="rzhint">
              {todas.length === 1 ? "1 linha" : `${num(todas.length)} linhas`}
            </span>
          </div>
          <div className="pbbody" style={largo ? { overflow: "auto" } : undefined}>
            {todas.length === 0 ? (
              // "Zero linha" é um achado legítimo de um check — precisa se distinguir de tela quebrada.
              <p className="pb-vazio">Nenhuma linha foi carimbada neste bloco.</p>
            ) : (
              <>
                <Bloco
                  view={bv}
                  tipo={largo ? "tabela" : "pivot"}
                  total={linhaTotal(b.total, cols, todas, perfis)}
                  colsNum={colsNum}
                />
                {todas.length > linhas.length && (
                  <p style={{ ...NOTA, padding: "8px 2px 0", margin: 0 }}>
                    exibindo {num(linhas.length)} de {num(todas.length)} linhas carimbadas ·{" "}
                    <button type="button" className="lk" style={{ fontSize: 11.5 }} onClick={() => setExpandido((s) => ({ ...s, [chave]: true }))}>
                      mostrar todas
                    </button>{" "}
                    · o Excel leva o conteúdo inteiro
                  </p>
                )}
              </>
            )}
          </div>
          {/* Procedência DO BLOCO: o número desta grade nasceu neste nó, desta execução. */}
          <div className="trace-foot" style={{ padding: "8px 12px", margin: 0 }}>
            <span>
              view <b>{curto(b.view_id)}</b>
            </span>
            {origem?.execucao_id && (
              <span>
                execução{" "}
                <button
                  type="button"
                  className="lk"
                  style={{ fontSize: 11.5 }}
                  title={`Abrir a execução ${origem.execucao_id}`}
                  onClick={() => navigate(`/execucoes/${origem.execucao_id}`)}
                >
                  {curto(origem.execucao_id)}
                </button>
              </span>
            )}
            {origem?.no_id && (
              <span>
                nó <b>{origem.no_id}</b>
              </span>
            )}
            {origem?.hash && (
              <span title={`hash do snapshot: ${origem.hash}`}>
                snapshot <b>{origem.hash.slice(0, 10)}</b>
              </span>
            )}
          </div>
        </div>
      );
    };

    const kpis = blocos.map((b, i) => ({ b, i })).filter(({ b }) => ehKpi(b.tipo));
    const pivots = blocos.map((b, i) => ({ b, i })).filter(({ b }) => !ehKpi(b.tipo) && !ehLargo(b.tipo));
    const largos = blocos.map((b, i) => ({ b, i })).filter(({ b }) => ehLargo(b.tipo));

    return (
      <>
        <div className="page-head">
          <button className="link-back" onClick={fechar}>
            ← Relatórios
          </button>
          <h1 className="display">{aberto.pagina ?? "Relatório carimbado"}</h1>
          <p className="sub">
            Carimbo <span className="exec-cel-mono" title={aberto.id}>{curto(aberto.id)}</span> da
            página <b>v{aberto.versao}</b>. Imutável: reabrir não recalcula nada.
          </p>
          <div className="meta-row">
            <div className="meta">
              <span className="k">Gerado em</span>
              <span className="v">{fmtMomento(aberto.gerado_em)}</span>
            </div>
            <div className="meta">
              <span className="k">Por</span>
              <span className="v">{aberto.criado_por ?? "—"}</span>
            </div>
            <div className="meta">
              <span className="k">Blocos</span>
              <span className="v num">{num(blocos.length)}</span>
            </div>
            <div className="meta">
              <span className="k">Linhas carimbadas</span>
              <span className="v num">{num(totalLinhas)}</span>
            </div>
            {execucoes.length > 0 && (
              <div className="meta">
                <span className="k">{execucoes.length === 1 ? "Execução de origem" : "Execuções de origem"}</span>
                <span className="v">
                  {execucoes.map((ex, i) => (
                    <span key={ex}>
                      {i > 0 && " · "}
                      <button
                        type="button"
                        className="lk"
                        title={`Abrir a execução ${ex}`}
                        onClick={() => navigate(`/execucoes/${ex}`)}
                      >
                        {curto(ex)}
                      </button>
                    </span>
                  ))}
                </span>
              </div>
            )}
          </div>
        </div>
        <div className="pad">
          <div className="ro-banner">
            <span className="immut">imutável</span>
            <span>
              Foto de um instante — os números são os de <b>{fmtMomento(aberto.gerado_em)}</b>, não
              dado ao vivo. Carimbo append-only: só leitura e exportação.
            </span>
          </div>
          {erroDet && (
            <div className="exec-banner-falha" role="alert" style={{ marginBottom: 14 }}>
              <span className="dot" />
              <span className="exec-banner-txt">{erroDet}</span>
            </div>
          )}

          <div className="filterbar">
            <span className="fl">
              <span className="ro">read-only</span>
            </span>
            {filtros.length === 0 ? (
              <span className="fl">Sem filtros — visão completa da página</span>
            ) : (
              filtros.map((f, i) => (
                // Chave com índice: dois filtros podem incidir na MESMA dimensão (≥ e ≤).
                <span className="fl" key={`${f.dimensao}:${i}`}>
                  {f.dimensao} {ROTULO_OP[f.op ?? "igual"] ?? f.op}{" "}
                  <b className="num">{fmtValorFiltro(f.valor)}</b>
                </span>
              ))
            )}
          </div>

          <button className="btn-stamp" style={{ marginBottom: 14 }} onClick={exportar} disabled={exportando}>
            <Icon name="download" cls="ic" />{" "}
            {exportando ? "Exportando…" : "Exportar Excel (uma aba por bloco)"}
          </button>

          {blocos.length === 0 ? (
            <div className="card lc-empty">
              <div className="big">Nenhum bloco neste carimbo</div>
              <div>A página não tinha blocos quando o relatório foi gerado.</div>
            </div>
          ) : (
            <>
              {/* Cartões-KPI primeiro; pivots em grade; detalhe/tabela em largura cheia — a mesma
                  disposição do Portal, para o carimbo parecer com o que o operador viu. */}
              {kpis.length > 0 && (
                <div className="vis-grid" style={{ marginBottom: 14 }}>
                  {kpis.map(({ b, i }) => {
                    const cor = LINHA_CORES[i % LINHA_CORES.length];
                    return (
                      <div className="mpanel" key={`kpi:${b.view_id}:${i}`}>
                        <div className="pbh" style={{ cursor: "default", borderTop: "3px solid " + cor }}>
                          <span className="vd" style={{ background: cor }} />
                          <span className="pbt">{b.titulo ?? `Bloco ${i + 1}`}</span>
                        </div>
                        <div className="pbbody">
                          <Bloco tipo="valor" view={{ cols: [], mini: [], kpi: kpiDoBloco(b.resultado, b.titulo ?? "Total") }} />
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
              {pivots.length > 0 && <div className="vis-grid">{pivots.map(({ b, i }) => painel(b, i, false))}</div>}
              {largos.map(({ b, i }) => painel(b, i, true))}
            </>
          )}

          {/* Cadeia de rastreio congelada: relatório → página → view → cubo → execução → nó.
              Uma linha POR BLOCO: com 5 blocos, a cadeia única virava um parágrafo ilegível. */}
          <div className="trace-card" id="rastreio">
            <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 12 }}>
              <h4 className="display">Cadeia de rastreio</h4>
              <span className="immut">imutável</span>
            </div>
            <div style={{ fontSize: 12, color: "var(--ink-2)" }}>
              Parâmetros e dado congelados. Cada bloco rastreia até o nó do pipeline que produziu o
              snapshot — a execução é clicável.
            </div>
            <div className="trace-chain">
              <span className="step" title={aberto.id}>
                relatório {curto(aberto.id)}
              </span>
              <span className="arr">→</span>
              <span className="step">
                página {aberto.pagina ?? ""} v{aberto.versao}
              </span>
            </div>
            {(blocos.length ? blocos.map((b, i) => ({ b, i })) : (aberto.trace ?? []).map((t, i) => ({ b: { titulo: undefined, view_id: t.view_id, resultado: { colunas: [], linhas: [], origem: t.origem } } as BlocoConsulta, i }))).map(
              ({ b, i }) => {
                const origem = b.resultado?.origem ?? aberto.trace?.[i]?.origem;
                return (
                  <div className="trace-chain" key={`tr:${b.view_id}:${i}`} style={{ margin: "0 0 8px" }}>
                    <span className="step" style={{ fontFamily: "inherit" }}>
                      {b.titulo ?? `bloco ${i + 1}`}
                    </span>
                    <span className="arr">→</span>
                    <span className="step" title={b.view_id}>
                      view {curto(b.view_id)}
                    </span>
                    <span className="arr">→</span>
                    <span className="step">cubo da view</span>
                    <span className="arr">→</span>
                    {origem?.execucao_id ? (
                      <button
                        type="button"
                        className="step lk"
                        style={{ fontFamily: "ui-monospace,Menlo,monospace" }}
                        title={`Abrir a execução ${origem.execucao_id}`}
                        onClick={() => navigate(`/execucoes/${origem.execucao_id}`)}
                      >
                        execução {curto(origem.execucao_id)}
                      </button>
                    ) : (
                      <span className="step">execução —</span>
                    )}
                    <span className="arr">→</span>
                    <span className="step term" title={origem?.hash ? `hash do snapshot: ${origem.hash}` : undefined}>
                      nó {origem?.no_id ?? "—"} · snapshot Parquet
                    </span>
                  </div>
                );
              },
            )}
            <div className="trace-foot">
              <span>
                Gerado por <b>{aberto.criado_por ?? "—"}</b>
              </span>
              <span>
                Blocos <b className="num">{num(blocos.length)}</b>
              </span>
              <span>
                Linhas <b className="num">{num(totalLinhas)}</b>
              </span>
              <span>
                Fuso <b>America/Sao_Paulo</b>
              </span>
            </div>
          </div>
        </div>
      </>
    );
  }

  // ── CATÁLOGO ───────────────────────────────────────────────────────────────
  const paginasDaLista = [...new Set(lista.map((r) => r.pagina).filter(Boolean))].sort((a, b) =>
    String(a).localeCompare(String(b), "pt-BR"),
  );

  const dentroDoPeriodo = (iso: string | null) => {
    if (!fPeriodo) return true;
    const dia = diaSP(iso);
    if (!dia) return false;
    try {
      const j = resolverJanela(fPeriodo);
      return dia >= j.data_inicio && dia < j.data_fim; // fim EXCLUSIVO, como em resolverJanela
    } catch {
      return true;
    }
  };

  const filtrados = lista.filter(
    (r) => (!fPagina || r.pagina === fPagina) && dentroDoPeriodo(r.criado_em),
  );
  const temFiltro = !!(fPagina || fPeriodo);

  const itens: ItemCadastro[] = filtrados.map((r) => ({
    id: r.id,
    testid: "relatorio-row",
    nome: r.pagina ?? `Relatório ${curto(r.id)}`,
    meta: `página v${r.versao}`,
    campos: [
      {
        // Carimbo é artefato datado: sem o ANO, "22/06, 10:41" é o mesmo texto para dois
        // relatórios de anos diferentes. Dia (com ano, ou "hoje"/"ontem") + hora, como na lista
        // de execuções; o momento completo com fuso fica no `title`.
        rotulo: "Gerado em (GMT-3)",
        valor: r.criado_em ? (
          <span title={fmtMomento(r.criado_em)}>
            {rotuloDia(r.criado_em)} · {fmtHora(r.criado_em)}
          </span>
        ) : (
          <span style={{ color: "var(--ink-2)" }}>—</span>
        ),
      },
      { rotulo: "Por", valor: r.criado_por ?? "—" },
      {
        rotulo: "Carimbo",
        valor: (
          <button
            type="button"
            className="lk exec-cel-mono"
            title={`Abrir a cadeia de rastreio do carimbo ${r.id}`}
            onClick={(e) => {
              e.stopPropagation();
              abrir(r.id, true);
            }}
          >
            {curto(r.id)}
          </button>
        ),
      },
    ],
  }));

  return (
    <>
      <div className="page-head">
        <h1 className="display">Relatórios</h1>
        <p className="sub">
          Cada relatório é um <b>carimbo imutável</b>: o dado congelado no instante em que foi
          gerado, com a cadeia de rastreio até o nó de origem. Nada aqui recalcula.
        </p>
      </div>
      <div className="pad">
        <div className="card">
          <div className="card-h">
            <h3>Carimbados</h3>
            <span className="tag junto" data-testid="relatorios-contagem">
              {lista.length === 0
                ? "nenhum"
                : temFiltro
                  ? `${num(filtrados.length)} de ${num(lista.length)}`
                  : `${num(lista.length)} relatório${lista.length === 1 ? "" : "s"}`}
              {" · mais recentes primeiro"}
            </span>
            <div className="exec-h-acoes">
              <button type="button" className="pag-btn" disabled={carregandoLista} onClick={carregarLista}>
                Atualizar
              </button>
            </div>
          </div>

          <div className="exec-filtros" style={{ gridTemplateColumns: "240px auto 1fr" }}>
            <label className="exec-filtro">
              <span className="eyebrow">Página</span>
              <select value={fPagina} onChange={(e) => trocarFiltro("pag", e.target.value)}>
                <option value="">Todas as páginas</option>
                {paginasDaLista.map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </select>
            </label>
            <div className="exec-filtro exec-filtro-periodo">
              <span className="eyebrow">Gerado em</span>
              <SeletorPeriodo
                periodo={fPeriodo}
                permitirTudo
                testid="rel-filtro-periodo"
                onChange={(t) => trocarFiltro("per", t)}
              />
            </div>
            {temFiltro && (
              <button
                type="button"
                className="exec-limpar"
                onClick={() => setParams(new URLSearchParams(), { replace: true })}
              >
                Limpar filtros
              </button>
            )}
          </div>

          {erro && (
            <div className="exec-erro-lista" role="alert">
              {erro}{" "}
              <button type="button" className="pag-btn" onClick={carregarLista}>
                Tentar de novo
              </button>
            </div>
          )}

          {carregandoLista ? (
            <div className="exec-lista-vazia">Carregando…</div>
          ) : (
            <div style={{ padding: "12px 14px" }}>
              <GridLista
              erro={!!erro}
                rotulo="Relatórios carimbados"
                abrirLabel="Abrir"
                onAbrir={(id) => abrir(id)}
                itens={itens}
                acoes={(it) => (
                  <>
                    <button type="button" onClick={() => abrir(it.id)}>
                      Abrir
                    </button>
                    <button type="button" onClick={() => abrir(it.id, true)}>
                      Rastreio
                    </button>
                    <button
                      type="button"
                      disabled={exportandoId === it.id}
                      onClick={() => void exportarLinha(it.id)}
                    >
                      {exportandoId === it.id ? "Exportando…" : "Exportar Excel"}
                    </button>
                  </>
                )}
                vazio={
                  <div className="lc-empty">
                    <div className="big">
                      {temFiltro ? "Nenhum carimbo com esses filtros" : "Nenhum relatório carimbado ainda"}
                    </div>
                    {temFiltro ? (
                      <>
                        Ajuste a página ou o período.{" "}
                        <button
                          type="button"
                          className="lk"
                          onClick={() => setParams(new URLSearchParams(), { replace: true })}
                        >
                          Limpar filtros
                        </button>
                      </>
                    ) : (
                      "No Portal, abra uma página publicada e gere o relatório carimbado."
                    )}
                  </div>
                }
              />
            </div>
          )}
        </div>
      </div>
    </>
  );
}
