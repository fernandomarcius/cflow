import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { campoSistema, ConfirmarAcao, GridLista, Icon, type ItemCadastro } from "../ui";
import { SeletorVersao } from "../SeletorVersao";
import * as api from "../../api/client";
import type { FuncaoDef } from "../../api/client";
import { diaSP, fmtHora, rotuloDia } from "../execucao/formato";

// Biblioteca de funções: cada função vira uma MACRO do DuckDB, versionada e reutilizável
// no SQL dos nós. O editor valida a definição ANTES de chamar a API — o backend recusa a mesma
// coisa com 422 (`cflow_core.biblioteca_funcoes.definicao.validar`), e é melhor dizer o que está
// errado no campo do que devolver um erro de servidor.

// Retornos suportados pela MACRO (espelha cflow_core.biblioteca_funcoes.definicao).
const RETORNOS: [FuncaoDef["retorno"], string][] = [
  ["scalar", "valor único (scalar)"],
  ["struct", "registro (struct)"],
];

// Mesmo identificador aceito pelo backend: letra/_ seguidos de letras, dígitos ou _.
const IDENT = /^[A-Za-z_]\w*$/;

// Texto só para leitor de tela (sem classe utilitária no CSS do produto, e sem poder criar uma).
// `clip` + 1px, nunca `display:none`/`visibility:hidden`, que também escondem da acessibilidade.
const SO_LEITOR: React.CSSProperties = {
  position: "absolute",
  width: 1,
  height: 1,
  overflow: "hidden",
  clip: "rect(0 0 0 0)",
  clipPath: "inset(50%)",
  whiteSpace: "nowrap",
};

// Até esta quantidade a biblioteca abre com TODAS as funções expandidas. Acima, abre recolhida:
// a "Particularidades" tem 39 funções e, expandida, era uma rolagem única de ~40 telas em que não
// dava para ver o que existe nem chegar na última — o índice (assinatura) vale mais que o corpo.
const ABRIR_TUDO_ATE = 6;

interface ErroTela {
  mensagem: string;
  requestId?: string;
}
function erroDe(e: unknown, fallback: string): ErroTela {
  // O cliente da API expõe `requestId` num caminho e `request_id` no outro (`api/client.ts`). Ler
  // só o snake deixava o código de suporte fora da tela justamente nos erros do outro caminho.
  const err = e as { message?: string; requestId?: string; request_id?: string } | null;
  const req = err?.requestId ?? err?.request_id;
  return {
    mensagem: err?.message || fallback,
    requestId: typeof req === "string" ? req : undefined,
  };
}

function Aviso({ erro, onTentar }: { erro: ErroTela; onTentar?: () => void }) {
  return (
    <div className="exec-banner-falha" role="alert" style={{ marginBottom: 14 }}>
      <span className="dot" />
      <span>
        {erro.mensagem}
        {erro.requestId && (
          <span style={{ display: "block", fontSize: 11.5, opacity: 0.9 }}>
            Código para suporte: {erro.requestId}
          </span>
        )}
      </span>
      {onTentar && (
        <button type="button" className="pag-btn" onClick={onTentar}>
          Tentar de novo
        </button>
      )}
    </div>
  );
}

// ============================================================================
// LISTAGEM — estado real de cada biblioteca
//
// `GET /funcoes` devolve só `{id, nome, sistema}`. Sem enriquecer, a linha trazia o subtítulo fixo
// "biblioteca de funções" em todas — e não dava para escolher entre "FuncoesPadroes" e
// "Particularidades" sem abrir as duas. O que decide: QUAIS funções há dentro, quantas são, qual
// versão está publicada (só ela vale nos pipelines) e onde a biblioteca é usada.
// ============================================================================

const NF = new Intl.NumberFormat("pt-BR");
function plural(n: number, um: string, muitos: string): string {
  return `${NF.format(n)} ${n === 1 ? um : muitos}`;
}

interface EstadoBiblioteca {
  qtd: number;
  nomes: string[];
  descricao: string;
  publicada: { numero: number; em: string | null } | null;
  rascunhoAdiante: number | null; // versão salva à frente da publicada (nos pipelines vale a outra)
}

// Três fases distinguíveis: "lendo…" não pode virar "0 funções", e falha de leitura não pode virar
// zero.
type Fase<T> = { fase: "carregando" } | { fase: "ok"; dado: T } | { fase: "erro" };

async function lerEstadoBiblioteca(id: string): Promise<EstadoBiblioteca> {
  const [bib, versoes] = await Promise.all([api.obterFuncao(id), api.listarVersoesFuncao(id)]);
  const d = (bib.definicao ?? {}) as any;
  const fs: any[] = Array.isArray(d.funcoes) ? d.funcoes : [];
  const pub = versoes.find((v) => v.ativa) ?? null;
  const topo = versoes[0] ?? null;
  return {
    qtd: fs.length,
    nomes: fs.map((f) => String(f?.nome ?? "")).filter(Boolean),
    descricao: typeof d.descricao === "string" ? d.descricao : "",
    publicada: pub ? { numero: pub.numero, em: pub.criado_em } : null,
    rascunhoAdiante: pub && topo && topo.numero > pub.numero ? topo.numero : null,
  };
}

// Quais bibliotecas cada pipeline usa — mesma regra do backend
// (`cflow_core.pipelines.definicao.bibliotecas_usadas`): nível pipeline + `no.config.bibliotecas`.
//
// Sob demanda (botão), não automático: não existe endpoint de uso, então a única forma é baixar a
// definição de TODOS os pipelines — e uma delas tem 375 kB de código C#. Pagar isso toda vez que
// alguém abre a tela para escolher uma biblioteca é caro; pagar quando se pergunta "onde isto é
// usado?" é justo.
async function pipelinesPorBiblioteca(): Promise<{ mapa: Record<string, number>; total: number }> {
  const pipes = await api.listarPipelines();
  const porPipeline = await Promise.all(
    pipes.map(async (p) => {
      const det = await api.obterPipeline(p.id);
      const d = (det.definicao ?? {}) as any;
      const ids = new Set<string>();
      const juntar = (x: unknown) => {
        if (Array.isArray(x)) for (const v of x) if (v) ids.add(String(v));
      };
      juntar(d.bibliotecas);
      for (const n of Array.isArray(d.nos) ? d.nos : []) juntar(n?.config?.bibliotecas);
      return [...ids];
    }),
  );
  const mapa: Record<string, number> = {};
  for (const bibs of porPipeline) for (const b of bibs) mapa[b] = (mapa[b] ?? 0) + 1;
  return { mapa, total: pipes.length };
}

// Valor ausente/indeterminado da grade. `--ink-2` (e não `--ink-3`): este texto CARREGA informação
// ("não verificado", "nunca", "nenhum pipeline") e a regra transversal do CSS reserva `--ink-3`
// para separador, ícone decorativo e placeholder. Continua mais fraco que `--ink` do valor real.
function Fraco({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <span style={{ color: "var(--ink-2)" }} title={title}>
      {children}
    </span>
  );
}

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

// Amostra dos nomes das macros — é por ela que se escolhe a biblioteca sem abrir as duas.
function amostraNomes(nomes: string[]): string {
  if (nomes.length === 0) return "nenhuma função escrita ainda";
  const mostra = nomes.slice(0, 3).join(" · ");
  const resto = nomes.length - 3;
  return resto > 0 ? `${mostra} · +${NF.format(resto)}` : mostra;
}

export default function Funcoes() {
  const [aberto, setAberto] = useState<string | null>(null);
  const [listaRaw, setListaRaw] = useState<api.PipelineResumo[]>([]);
  const [carregando, setCarregando] = useState(true);
  const [ehAdmin, setEhAdmin] = useState(false);
  const [sistemaBusy, setSistemaBusy] = useState<string | null>(null);
  const [erroLista, setErroLista] = useState<ErroTela | null>(null);
  const [criando, setCriando] = useState(false);

  // Enriquecimento por biblioteca (ver bloco acima). `serieEnriq` = "última carga vence".
  const [estado, setEstado] = useState<Record<string, Fase<EstadoBiblioteca>>>({});
  const serieEnriq = useRef(0);
  // Uso nos pipelines: `null` = ainda não verificado (a varredura é sob demanda, não um vazio).
  const [uso, setUso] = useState<Fase<{ mapa: Record<string, number>; total: number }> | null>(null);

  const enriquecer = useCallback(async (ids: string[]) => {
    const meu = ++serieEnriq.current;
    setEstado(
      Object.fromEntries(ids.map((id) => [id, { fase: "carregando" } as Fase<EstadoBiblioteca>])),
    );
    await Promise.allSettled(
      ids.map(async (id) => {
        try {
          const dado = await lerEstadoBiblioteca(id);
          if (meu === serieEnriq.current) setEstado((s) => ({ ...s, [id]: { fase: "ok", dado } }));
        } catch {
          if (meu === serieEnriq.current) setEstado((s) => ({ ...s, [id]: { fase: "erro" } }));
        }
      }),
    );
  }, []);

  async function verificarUso() {
    setUso({ fase: "carregando" });
    try {
      setUso({ fase: "ok", dado: await pipelinesPorBiblioteca() });
    } catch {
      // Tudo-ou-nada de propósito: contagem parcial faria uma biblioteca em uso parecer livre.
      setUso({ fase: "erro" });
    }
  }

  const recarregarLista = useCallback(async () => {
    setCarregando(true);
    setErroLista(null);
    setUso(null); // pipelines podem ter mudado: a contagem anterior deixaria de ser evidência
    try {
      const lista = await api.listarFuncoes();
      setListaRaw(lista);
      void enriquecer(lista.map((b) => b.id));
    } catch (e: unknown) {
      setErroLista(erroDe(e, "Falha ao carregar as bibliotecas de funções."));
    } finally {
      setCarregando(false);
    }
  }, [enriquecer]);

  useEffect(() => {
    if (aberto === null) void recarregarLista();
  }, [aberto, recarregarLista]);

  useEffect(() => {
    void api.meAtual().then((u) => setEhAdmin(u.perfil === "admin")).catch(() => setEhAdmin(false));
  }, []);

  async function alternarSistema(id: string, sistema: boolean) {
    setErroLista(null);
    setSistemaBusy(id);
    setListaRaw((l) => l.map((b) => (b.id === id ? { ...b, sistema } : b)));
    try {
      await api.definirSistema("funcoes", id, sistema);
    } catch (e: unknown) {
      setListaRaw((l) => l.map((b) => (b.id === id ? { ...b, sistema: !sistema } : b)));
      setErroLista(erroDe(e, "Falha ao alterar a marca de sistema."));
    } finally {
      setSistemaBusy(null);
    }
  }

  const itens: ItemCadastro[] = useMemo(
    () =>
      listaRaw.map((b) => {
        const f = estado[b.id];
        const e = f?.fase === "ok" ? f.dado : null;

        // Sub-linha do nome = as macros que estão aqui dentro. É o que diferencia uma biblioteca
        // da outra; "biblioteca de funções" em toda linha não diferenciava nada.
        let meta = "lendo as funções…";
        if (f?.fase === "erro") meta = "não foi possível ler a definição desta biblioteca";
        else if (e) meta = amostraNomes(e.nomes);

        const pill = !e
          ? { label: f?.fase === "erro" ? "não lido" : "lendo…", cls: "off" }
          : !e.publicada
            ? { label: "nunca publicada", cls: "off" }
            : e.rascunhoAdiante
              ? { label: `rascunho v${e.rascunhoAdiante}`, cls: "skip" }
              : { label: "publicada", cls: "ok" };

        return {
          id: b.id,
          nome: b.nome,
          meta,
          status: pill.label,
          scls: pill.cls,
          campos: [
            {
              rotulo: "Funções",
              valor: celFase(f, (d) =>
                d.qtd ? (
                  <span title={d.descricao || "macros SQL reutilizáveis desta biblioteca"}>
                    {NF.format(d.qtd)}
                  </span>
                ) : (
                  <Fraco title="biblioteca vazia — nada a chamar no SQL dos nós">nenhuma</Fraco>
                ),
              ),
            },
            {
              rotulo: "Usada em",
              valor:
                uso === null ? (
                  <Fraco title='clique em "Verificar uso" na barra acima — a checagem lê a definição de todos os pipelines'>
                    não verificado
                  </Fraco>
                ) : (
                  celFase(uso, ({ mapa, total }) => {
                    const n = mapa[b.id] ?? 0;
                    return n ? (
                      <span title={`de ${plural(total, "pipeline", "pipelines")} verificados`}>
                        {plural(n, "pipeline", "pipelines")}
                      </span>
                    ) : (
                      <Fraco title={`nenhum dos ${total} pipelines chama esta biblioteca`}>
                        nenhum pipeline
                      </Fraco>
                    );
                  })
                ),
            },
            {
              rotulo: "Publicada (GMT-3)",
              valor: celFase(f, (d) =>
                d.publicada ? (
                  <span style={{ whiteSpace: "nowrap" }} title="só a versão publicada vale no SQL dos pipelines">
                    v{d.publicada.numero} · {quando(d.publicada.em)}
                  </span>
                ) : (
                  <Fraco title="enquanto não publicar, nenhum pipeline enxerga estas funções">
                    nunca
                  </Fraco>
                ),
              ),
            },
            campoSistema(ehAdmin, b.id, b.sistema === true),
          ].filter((x): x is NonNullable<typeof x> => x !== null),
        };
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [listaRaw, ehAdmin, sistemaBusy, estado, uso],
  );

  async function nova() {
    setErroLista(null);
    setCriando(true);
    try {
      const { id } = await api.criarFuncao("Nova biblioteca");
      setAberto(id);
    } catch (e: unknown) {
      setErroLista(erroDe(e, "Falha ao criar biblioteca de funções."));
    } finally {
      setCriando(false);
    }
  }

  // Exclusão: a confirmação é o modal do próprio GridLista (design system) — aqui só executa.
  async function excluir(id: string) {
    setErroLista(null);
    try {
      await api.excluirFuncao(id);
      await recarregarLista();
    } catch (e: unknown) {
      setErroLista(erroDe(e, "Falha ao excluir biblioteca de funções."));
    }
  }

  async function renomear(id: string, nome: string) {
    setErroLista(null);
    try {
      await api.renomearFuncao(id, nome);
      await recarregarLista();
    } catch (e: unknown) {
      setErroLista(erroDe(e, "Falha ao renomear biblioteca de funções."));
    }
  }

  if (aberto !== null) {
    return <Editor bibliotecaId={aberto} onVoltar={() => setAberto(null)} />;
  }

  const total = itens.length;
  return (
    <>
      <div className="page-head">
        <h1 className="display">Funções</h1>
        <p className="sub">
          Cada função é uma macro SQL versionada: escreva a regra uma vez, use em qualquer nó de
          qualquer pipeline. Publique para que os pipelines passem a ver a versão nova.
        </p>
      </div>
      <div className="pad">
        {erroLista && <Aviso erro={erroLista} onTentar={() => void recarregarLista()} />}

        <div className="lc-bar">
          <span data-testid="fn-contagem">
            {carregando
              ? "carregando…"
              : total === 1
                ? "1 biblioteca"
                : `${total} bibliotecas`}
          </span>
          {/* Varredura sob demanda: responder "onde isto é usado?" custa baixar a definição de
              todos os pipelines (não há endpoint de uso). Fora do caminho de quem só quer abrir
              uma biblioteca. */}
          {total > 0 && (
            <button
              type="button"
              className="pag-btn"
              style={{ marginLeft: 14 }}
              data-testid="fn-verificar-uso"
              disabled={uso?.fase === "carregando"}
              title="Lê a definição de todos os pipelines e conta quais chamam cada biblioteca"
              onClick={() => void verificarUso()}
            >
              {uso?.fase === "carregando" ? "Verificando…" : "Verificar uso"}
            </button>
          )}
          <button
            className="btn-stamp"
            style={{ marginLeft: "auto" }}
            data-testid="nova-funcao"
            disabled={criando}
            onClick={() => void nova()}
          >
            + {criando ? "Criando…" : "Nova biblioteca"}
          </button>
        </div>

        {uso?.fase === "erro" && (
          <div className="exec-aviso" role="alert" style={{ marginBottom: 14 }}>
            Não foi possível ler a definição de todos os pipelines — a coluna <b>Usada em</b> ficaria
            com uma contagem parcial, que numa análise de impacto é pior que nenhuma. Tente de novo.
          </div>
        )}

        <GridLista
              erro={!!erroLista}
          itens={itens}
          carregando={carregando}
          rotulo="Bibliotecas de funções"
          onAbrir={(id) => setAberto(id)}
          onExcluir={(id) => void excluir(id)}
          onRenomear={(id, nomeAtual) => void renomear(id, nomeAtual)}
          onSistema={ehAdmin ? (id, s) => void alternarSistema(id, s) : undefined}
          ehSistema={(it) => listaRaw.find((b) => b.id === it.id)?.sistema === true}
          vazio={
            <div className="card lc-empty">
              <div className="big">Nenhuma biblioteca de funções</div>
              <div>
                Clique <b>+ Nova biblioteca</b> para escrever a primeira regra reutilizável.
              </div>
            </div>
          }
        />
      </div>
    </>
  );
}

// ============ EDITOR (tabela de funções + avaliar) ============
function Editor({ bibliotecaId, onVoltar }: { bibliotecaId: string; onVoltar: () => void }) {
  const [nome, setNome] = useState("");
  const [nomeSalvo, setNomeSalvo] = useState("");
  const [renomeando, setRenomeando] = useState(false);
  const [baseNumero, setBaseNumero] = useState(0);
  const [carregando, setCarregando] = useState(true);

  const [descricao, setDescricao] = useState("");
  const [funcoes, setFuncoes] = useState<FuncaoDef[]>([]);
  // Busca por nome/descrição e quais funções estão expandidas (índices na lista `funcoes`).
  const [busca, setBusca] = useState("");
  const [abertas, setAbertas] = useState<Set<number>>(new Set());

  const [salvando, setSalvando] = useState(false);
  const [erro, setErro] = useState<ErroTela | null>(null);
  const [aviso, setAviso] = useState("");
  const [pub, setPub] = useState(false);
  // Edição pendente. Rascunho ≠ publicado: sair (ou trocar de versão) descartava em silêncio o que
  // ainda não tinha ido para o servidor — a mesma armadilha que o editor de pipeline já fechou.
  const [sujo, setSujo] = useState(false);
  // Remoção de função com conteúdo pede confirmação (o botão fica ao lado dos campos).
  const [removendo, setRemovendo] = useState<number | null>(null);
  // Saída/troca de versão que espera confirmação porque há edição pendente.
  const [saindo, setSaindo] = useState<{ tipo: "voltar" } | { tipo: "versao"; numero: number | null } | null>(null);
  // Versões (dropdown): `versaoSel` = versão CARREGADA (null = a última). baseNumero = nº máximo
  // (lock) — editar/publicar uma versão anterior gera uma NOVA (a anterior fica intacta).
  const [versoes, setVersoes] = useState<api.VersaoResumo[]>([]);
  const [versaoSel, setVersaoSel] = useState<number | null>(null);
  const ultima = versoes.length ? versoes[0].numero : null;

  const carregarVersoes = useCallback(() => {
    api.listarVersoesFuncao(bibliotecaId).then(setVersoes).catch(() => setVersoes([]));
  }, [bibliotecaId]);

  // Carrega a biblioteca (última, ou a versão `numero`). Definição vazia → uma função em branco.
  const carregar = useCallback(async (numero?: number) => {
    setCarregando(true);
    setErro(null);
    try {
      const b = await api.obterFuncao(bibliotecaId, numero);
      setNome(b.nome);
      setNomeSalvo(b.nome);
      setBaseNumero(b.ultimo ?? b.numero); // lock = máximo
      const d = b.definicao || {};
      setDescricao(typeof d.descricao === "string" ? d.descricao : "");
      const fs = Array.isArray(d.funcoes) ? d.funcoes.map((f: any) => normalizar(f)) : [];
      const lista: FuncaoDef[] = fs.length ? fs : [novaFuncao()];
      setFuncoes(lista);
      // Biblioteca pequena abre pronta para editar; grande abre como índice (ver ABRIR_TUDO_ATE).
      setAbertas(new Set(lista.length <= ABRIR_TUDO_ATE ? lista.map((_, i) => i) : []));
      setBusca("");
      setSujo(false);
    } catch (e: unknown) {
      setErro(erroDe(e, "Falha ao abrir biblioteca de funções."));
    } finally {
      setCarregando(false);
    }
  }, [bibliotecaId]);

  useEffect(() => {
    void carregar();
    carregarVersoes();
  }, [carregar, carregarVersoes]);

  const selecionarVersao = async (numero: number | null, forcar = false) => {
    // Trocar de versão RECARREGA a definição: com edição pendente, o que não foi salvo morre aqui.
    if (sujo && !forcar) {
      setSaindo({ tipo: "versao", numero });
      return;
    }
    setVersaoSel(numero);
    setErro(null);
    setAviso("");
    setPub(false);
    await carregar(numero ?? undefined);
  };

  // ---- Edição da tabela de funções ----
  const setFn = (i: number, patch: Partial<FuncaoDef>) =>
    setFuncoes((fs) => fs.map((f, j) => (j === i ? { ...f, ...patch } : f)));
  const addFn = () => {
    const i = funcoes.length;
    setFuncoes((fs) => [...fs, novaFuncao()]);
    setSujo(true);
    // Nasce expandida (é vazia, não há o que ler recolhido) e a busca sai do caminho — senão a
    // função nova nasceria fora do filtro, invisível, e o clique pareceria não ter feito nada.
    setAbertas((s) => new Set(s).add(i));
    setBusca("");
  };
  const delFn = (i: number) => {
    setFuncoes((fs) => fs.filter((_, j) => j !== i));
    setSujo(true);
    // `abertas` é indexado por posição: sem reindexar, remover a função 3 deixaria a 4 recolhida
    // e a 5 aberta no lugar errado.
    setAbertas((s) => {
      const out = new Set<number>();
      s.forEach((j) => {
        if (j !== i) out.add(j > i ? j - 1 : j);
      });
      return out;
    });
  };
  const alternar = (i: number) =>
    setAbertas((s) => {
      const out = new Set(s);
      if (!out.delete(i)) out.add(i);
      return out;
    });

  // Problemas por linha, nas MESMAS regras do backend (evita 422 depois de clicar em publicar).
  const problemas = useMemo(() => {
    const out: Record<number, string> = {};
    const contagem = new Map<string, number>();
    for (const f of funcoes) {
      const n = f.nome.trim();
      if (n) contagem.set(n, (contagem.get(n) ?? 0) + 1);
    }
    funcoes.forEach((f, i) => {
      const n = f.nome.trim();
      if (!n) out[i] = "Dê um nome à função — é por ele que o SQL do nó a chama.";
      else if (!IDENT.test(n))
        out[i] = "Nome inválido: comece com letra ou _ e use só letras, dígitos ou _.";
      else if ((contagem.get(n) ?? 0) > 1) out[i] = "Já existe outra função com este nome aqui.";
      else {
        const argRuim = (f.args ?? []).find((a) => !IDENT.test(a.trim()));
        if (argRuim) out[i] = `Argumento inválido: «${argRuim}» — use identificadores.`;
        else if (!f.corpo.trim()) out[i] = "Escreva a expressão SQL do corpo.";
      }
    });
    return out;
  }, [funcoes]);
  const qtdProblemas = Object.keys(problemas).length;
  const podeGravar = funcoes.length > 0 && qtdProblemas === 0;

  // Busca por nome (e descrição, que é onde está o "para que serve"). Cada item carrega o índice
  // ORIGINAL: é ele que endereça `setFn`/`delFn` e os `data-testid` das linhas — filtrar não pode
  // renumerar nada.
  const q = busca.trim().toLowerCase();
  const visiveis = useMemo(
    () =>
      funcoes
        .map((f, i) => ({ f, i }))
        .filter(
          ({ f }) =>
            !q ||
            f.nome.toLowerCase().includes(q) ||
            (f.descricao ?? "").toLowerCase().includes(q),
        ),
    [funcoes, q],
  );
  const ocultas = funcoes.length - visiveis.length;
  // Função com problema escondida pelo filtro é o caso perigoso: "corrija o que está marcado"
  // apontando para uma tela sem nada marcado.
  const problemasOcultos = Object.keys(problemas).filter(
    (k) => !visiveis.some((v) => v.i === Number(k)),
  ).length;

  // ---- Definição (JSON versionado) ----
  const montarDefinicao = useCallback((): api.BibliotecaDefinicao => {
    return {
      nome: nomeSalvo,
      descricao: descricao.trim() || undefined,
      funcoes: funcoes.map((f) => {
        const base: FuncaoDef = {
          nome: f.nome.trim(),
          args: (f.args ?? []).map((a) => a.trim()).filter(Boolean),
          corpo: f.corpo,
          retorno: f.retorno,
        };
        if (f.descricao && f.descricao.trim() !== "") base.descricao = f.descricao.trim();
        return base;
      }),
    };
  }, [descricao, funcoes, nomeSalvo]);

  async function renomear() {
    const limpo = nome.trim();
    if (!limpo || limpo === nomeSalvo) return;
    setRenomeando(true);
    setErro(null);
    setAviso("");
    try {
      const r = await api.renomearFuncao(bibliotecaId, limpo);
      setNome(r.nome);
      setNomeSalvo(r.nome);
      setAviso("Nome atualizado.");
    } catch (e: unknown) {
      setErro(erroDe(e, "Falha ao renomear biblioteca de funções."));
    } finally {
      setRenomeando(false);
    }
  }

  // Devolve o nº da versão salva, NÃO um booleano: `publicar` usa o retorno como `base_numero`, e
  // um `true` chegava ao backend como 1 → 409 "Versão-base desatualizada" em toda publicação (e cada
  // clique só empilhava rascunho). `setBaseNumero` não serve aqui: só vale no próximo render.
  async function salvar(): Promise<number | null> {
    if (!podeGravar) {
      // Põe o problema na tela antes de reclamar dele: recolhido ou filtrado, o "corrija o que
      // está marcado" mandaria procurar uma marca que não está visível.
      setBusca("");
      setAbertas((s) => {
        const out = new Set(s);
        for (const k of Object.keys(problemas)) out.add(Number(k));
        return out;
      });
      setErro({
        mensagem:
          "Corrija o que está marcado nas funções antes de salvar (a biblioteca precisa de ao menos uma função válida).",
      });
      return null;
    }
    setSalvando(true);
    setErro(null);
    setAviso("");
    setPub(false);
    try {
      const { numero } = await api.salvarFuncao(bibliotecaId, montarDefinicao(), baseNumero);
      setBaseNumero(numero);
      setVersaoSel(null);
      setSujo(false);
      carregarVersoes();
      setAviso(`Rascunho salvo (versão ${numero}). Ainda não vale nos pipelines — publique para isso.`);
      return numero;
    } catch (e: unknown) {
      setErro(erroDe(e, "Falha ao salvar rascunho."));
      return null;
    } finally {
      setSalvando(false);
    }
  }

  async function publicar() {
    // Sem alteração na tela, o rascunho vigente JÁ é o que se quer publicar (mesma regra do Cubo):
    // salvar de novo só criaria uma versão idêntica no histórico.
    const base = sujo ? await salvar() : baseNumero;
    if (base == null) return;
    setSalvando(true);
    setErro(null);
    try {
      const { numero } = await api.publicarFuncao(bibliotecaId, base);
      setBaseNumero(numero);
      setVersaoSel(null);
      carregarVersoes();
      setPub(true);
      setAviso("");
    } catch (e: unknown) {
      setErro(erroDe(e, "Falha ao publicar biblioteca de funções."));
    } finally {
      setSalvando(false);
    }
  }

  if (carregando) {
    return (
      <div className="page-head">
        <button className="link-back" onClick={onVoltar}>
          ← Funções
        </button>
        <h1 className="display">Biblioteca de funções</h1>
        <p className="sub" role="status">
          Carregando…
        </p>
      </div>
    );
  }

  const versaoAntiga = versaoSel !== null && ultima !== null && versaoSel !== ultima;

  return (
    <>
      {removendo !== null && (
        <ConfirmarAcao
          titulo="Remover função"
          corpo={
            <>
              Tirar <b>{funcoes[removendo]?.nome.trim() || "esta função"}</b> da biblioteca? O SQL de
              qualquer nó que a chame passa a falhar depois de publicar.
            </>
          }
          rotuloConfirmar="Remover"
          perigo
          onConfirmar={() => {
            delFn(removendo);
            setRemovendo(null);
          }}
          onCancelar={() => setRemovendo(null)}
        />
      )}
      {saindo !== null && (
        <ConfirmarAcao
          titulo={saindo.tipo === "voltar" ? "Sair sem salvar?" : "Trocar de versão sem salvar?"}
          corpo={
            saindo.tipo === "voltar" ? (
              <>
                As funções editadas <b>ainda não foram salvas</b> — nem como rascunho. Sair agora
                descarta o que você escreveu.
              </>
            ) : (
              <>
                Abrir outra versão <b>recarrega a definição</b> do servidor e descarta as funções que
                você editou aqui. Salve o rascunho primeiro se quiser manter.
              </>
            )
          }
          rotuloConfirmar={saindo.tipo === "voltar" ? "Sair e descartar" : "Trocar e descartar"}
          perigo
          onConfirmar={() => {
            const acao = saindo;
            setSaindo(null);
            if (acao.tipo === "voltar") onVoltar();
            else void selecionarVersao(acao.numero, true);
          }}
          onCancelar={() => setSaindo(null)}
        />
      )}

      <div className="page-head">
        <button
          className="link-back"
          data-testid="fn-voltar"
          onClick={() => (sujo ? setSaindo({ tipo: "voltar" }) : onVoltar())}
        >
          ← Funções
        </button>
        <div style={{ display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap" }}>
          <h1 className="display" style={{ margin: 0 }}>
            {nomeSalvo || "Biblioteca de funções"}
          </h1>
          {versoes.length > 0 && (
            <SeletorVersao
              versoes={versoes}
              ultima={ultima}
              sel={versaoSel}
              ocupado={salvando}
              onSelecionar={(n) => void selecionarVersao(n)}
            />
          )}
        </div>
        <p className="sub">
          Cada função abaixo vira uma macro reutilizável no SQL dos nós. Rascunho não vale nos
          pipelines — só a versão publicada.
          {sujo && (
            <>
              <br />
              <b data-testid="fn-sujo">Alterações não salvas</b> — nem como rascunho.
            </>
          )}
        </p>
      </div>
      <div className="pad">
        {erro && <Aviso erro={erro} />}

        {versaoAntiga && (
          <div className="exec-aviso" role="status" style={{ marginBottom: 14 }}>
            Você está vendo a <b>versão {versaoSel}</b>, não a última ({ultima}). Salvar ou publicar
            daqui cria uma versão NOVA — a {versaoSel} fica intacta.
          </div>
        )}

        <div style={{ maxWidth: 640, marginBottom: 14 }}>
          <label className="fld" htmlFor="funcao-nome">
            Nome da biblioteca
          </label>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <input
              id="funcao-nome"
              className="nm"
              data-testid="funcao-nome"
              style={{
                flex: 1,
                fontFamily: '"Space Grotesk"',
                fontWeight: 600,
                fontSize: 14,
              }}
              value={nome}
              onChange={(e) => setNome(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void renomear();
              }}
            />
            <button
              className="pag-btn"
              data-testid="renomear-funcao"
              disabled={renomeando || !nome.trim() || nome.trim() === nomeSalvo}
              onClick={() => void renomear()}
            >
              {renomeando ? "Renomeando…" : "Renomear"}
            </button>
          </div>
          <label style={{ display: "block", marginTop: 12 }}>
            <span className="fld">Descrição (opcional)</span>
            <input
              className="nm"
              data-testid="funcao-descricao"
              style={{ fontWeight: 400 }}
              value={descricao}
              placeholder="para que serve esta biblioteca"
              onChange={(e) => {
                setDescricao(e.target.value);
                setSujo(true);
              }}
            />
          </label>
        </div>

        {/* Tabela de funções (cada função vira uma MACRO reutilizável no SQL dos nós) */}
        <div className="cstep">
          <div className="ch">
            <span className="n">ƒ</span>
            <h3>Funções</h3>
            <span className="hint">
              {q && funcoes.length > 0
                ? `${visiveis.length} de ${funcoes.length} ${funcoes.length === 1 ? "função" : "funções"}`
                : funcoes.length === 1
                  ? "1 função"
                  : `${funcoes.length} funções`}
              {qtdProblemas > 0 && (
                <span className="exec-erro" style={{ fontSize: "inherit" }}>
                  {" "}
                  · {qtdProblemas === 1 ? "1 com problema" : `${qtdProblemas} com problema`}
                </span>
              )}
            </span>
          </div>
          <div className="cb">
            {/* Índice da biblioteca: buscar pelo nome e abrir só o que se vai mexer. Sem isto, 39
                funções eram uma rolagem única — não dava para saber o que existe nem chegar na
                última. */}
            {funcoes.length > 1 && (
              <div
                style={{
                  display: "flex",
                  gap: 10,
                  alignItems: "flex-end",
                  flexWrap: "wrap",
                  marginBottom: 12,
                }}
              >
                <label style={{ display: "flex", flexDirection: "column", gap: 4, flex: "1 1 260px" }}>
                  <span className="fld">Buscar função</span>
                  <input
                    className="nm"
                    type="search"
                    data-testid="fn-busca"
                    value={busca}
                    placeholder="nome ou descrição"
                    style={{ fontWeight: 400, fontFamily: "ui-monospace,Menlo,monospace" }}
                    onChange={(e) => setBusca(e.target.value)}
                  />
                </label>
                <button
                  type="button"
                  className="pag-btn"
                  data-testid="fn-expandir-todas"
                  disabled={abertas.size === funcoes.length}
                  onClick={() => setAbertas(new Set(funcoes.map((_, i) => i)))}
                >
                  Expandir todas
                </button>
                <button
                  type="button"
                  className="pag-btn"
                  data-testid="fn-recolher-todas"
                  disabled={abertas.size === 0}
                  onClick={() => setAbertas(new Set())}
                >
                  Recolher todas
                </button>
              </div>
            )}
            {funcoes.length === 0 ? (
              <div className="ld">Nenhuma função. Adicione uma para começar.</div>
            ) : visiveis.length === 0 ? (
              <p style={{ margin: 0, fontSize: 12.5, color: "var(--ink-2)" }} data-testid="fn-sem-resultado">
                Nenhuma função com «{busca.trim()}» nesta biblioteca ·{" "}
                <button type="button" className="lk" onClick={() => setBusca("")}>
                  limpar a busca
                </button>
              </p>
            ) : (
              <div
                style={{ display: "flex", flexDirection: "column", gap: 12 }}
                onChange={() => setSujo(true)}
              >
                {/* Cabeçalho do índice. Sem ele, "valor único" aparecia 39 vezes sem nada que
                    dissesse o que era — etiqueta repetida não é rótulo. Alinha com a linha:
                    `.cfg-item` tem 14px de padding + 3px de filete à esquerda. */}
                {funcoes.length > 1 && (
                  <div
                    aria-hidden="true"
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      gap: 10,
                      padding: "0 15px 0 18px",
                      marginBottom: -4,
                      fontSize: 10.5,
                      letterSpacing: ".05em",
                      textTransform: "uppercase",
                      color: "var(--ink-2)",
                      fontWeight: 600,
                    }}
                  >
                    <span>Assinatura — como o SQL do nó chama · o que faz</span>
                    <span>Devolve</span>
                  </div>
                )}
                {visiveis.map(({ f, i }) => {
                  const problema = problemas[i];
                  const assinatura = `${f.nome.trim() || "sem_nome"}(${(f.args ?? []).join(", ")})`;
                  const aberta = abertas.has(i);
                  const idCampos = `fn-campos-${i}`;
                  return (
                    <div
                      key={i}
                      className="cfg-item"
                      data-testid={`funcao-row-${i}`}
                      style={{
                        display: "flex",
                        flexDirection: "column",
                        gap: aberta ? 10 : 6,
                        borderLeftColor: problema ? "#c0444c" : undefined,
                      }}
                    >
                      {/* Assinatura da macro: é assim que o SQL do nó vai chamar a função — e é o
                          botão que abre/fecha a linha (recolhida, a assinatura é o índice).
                          O botão ocupa a LINHA INTEIRA: com ele do tamanho da assinatura, os ~800px
                          à direita (onde ficam justamente a descrição e o retorno que se está
                          lendo) não abriam a função. */}
                      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                        <button
                          type="button"
                          data-testid={`fn-toggle-${i}`}
                          aria-expanded={aberta}
                          aria-controls={idCampos}
                          onClick={() => alternar(i)}
                          style={{
                            display: "flex",
                            alignItems: "center",
                            gap: 8,
                            flex: "1 1 auto",
                            background: "none",
                            border: "none",
                            // Alvo clicável de verdade: com `padding:0` o disclosure tinha 11px de
                            // altura útil e é o controle mais usado desta tela.
                            padding: "6px 8px 6px 4px",
                            margin: "-6px -8px -6px -4px",
                            borderRadius: 7,
                            cursor: "pointer",
                            font: "inherit",
                            textAlign: "left",
                            minWidth: 0,
                          }}
                        >
                          <span
                            aria-hidden="true"
                            style={{ color: "var(--ink-2)", fontSize: 13, lineHeight: 1, width: 11, flex: "none" }}
                          >
                            {aberta ? "▾" : "▸"}
                          </span>
                          <span className="no-func-fx" aria-hidden="true">
                            fx
                          </span>
                          <span
                            data-testid={`fn-assinatura-${i}`}
                            style={{
                              fontFamily: "ui-monospace,Menlo,monospace",
                              fontSize: 12,
                              color: "var(--ink)",
                              flex: "none",
                              overflowWrap: "anywhere",
                            }}
                          >
                            {assinatura}
                          </span>
                          {!aberta && (f.descricao ?? "").trim() !== "" && (
                            // A descrição usa a LARGURA QUE SOBRA (era travada em 44ch e cortava no
                            // meio da palavra com meia linha vazia à direita). Só encurta quando
                            // realmente não cabe, e o texto inteiro fica no `title`.
                            <span
                              title={f.descricao}
                              data-testid={`fn-desc-linha-${i}`}
                              style={{
                                fontSize: 11.5,
                                color: "var(--ink-2)",
                                flex: "1 1 auto",
                                minWidth: 0,
                                overflow: "hidden",
                                textOverflow: "ellipsis",
                                whiteSpace: "nowrap",
                              }}
                            >
                              {f.descricao}
                            </span>
                          )}
                          {!aberta && (
                            <span
                              style={{
                                marginLeft: "auto",
                                paddingLeft: 10,
                                fontSize: 11,
                                color: "var(--ink-2)",
                                flex: "none",
                                whiteSpace: "nowrap",
                              }}
                            >
                              {/* "devolve" existe só para o leitor de tela: na tela quem explica a
                                  coluna é o cabeçalho, e repetir o verbo 39 vezes seria o ruído que
                                  o cabeçalho vem tirar. */}
                              <span style={SO_LEITOR}>devolve </span>
                              {f.retorno === "struct" ? "registro" : "valor único"}
                            </span>
                          )}
                        </button>
                      </div>
                      {aberta && (
                      <div id={idCampos} style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end" }}>
                          <label style={{ display: "flex", flexDirection: "column", gap: 4, flex: "1 1 200px" }}>
                            <span className="fld">Nome</span>
                            <input
                              className="nm"
                              data-testid={`fn-nome-${i}`}
                              style={{ fontWeight: 400, fontFamily: "ui-monospace,Menlo,monospace" }}
                              value={f.nome}
                              placeholder="ex.: tratar_identificador"
                              aria-invalid={!!problema}
                              onChange={(e) => setFn(i, { nome: e.target.value })}
                            />
                          </label>
                          <label style={{ display: "flex", flexDirection: "column", gap: 4, flex: "2 1 240px" }}>
                            <span className="fld">Argumentos (separados por vírgula)</span>
                            <input
                              className="nm"
                              data-testid={`fn-args-${i}`}
                              style={{ fontWeight: 400, fontFamily: "ui-monospace,Menlo,monospace" }}
                              value={(f.args ?? []).join(", ")}
                              placeholder="ex.: regra, valor, contexto"
                              onChange={(e) =>
                                setFn(i, {
                                  args: e.target.value.split(",").map((a) => a.trim()).filter(Boolean),
                                })
                              }
                            />
                          </label>
                          <label style={{ display: "flex", flexDirection: "column", gap: 4, flex: "0 1 180px" }}>
                            <span className="fld">Retorno</span>
                            <select
                              className="nm"
                              data-testid={`fn-retorno-${i}`}
                              style={{ fontWeight: 400 }}
                              value={f.retorno}
                              onChange={(e) => setFn(i, { retorno: e.target.value as FuncaoDef["retorno"] })}
                            >
                              {RETORNOS.map(([k, l]) => (
                                <option key={k} value={k}>
                                  {l}
                                </option>
                              ))}
                            </select>
                          </label>
                          <button
                            className="add"
                            data-testid={`fn-remover-${i}`}
                            aria-label={`Remover a função ${f.nome.trim() || `nº ${i + 1}`}`}
                            style={{ marginBottom: 1 }}
                            onClick={() => {
                              // Linha ainda vazia sai direto; linha com conteúdo pede confirmação.
                              if (!f.nome.trim() && !f.corpo.trim() && !(f.args ?? []).length) delFn(i);
                              else setRemovendo(i);
                            }}
                          >
                            remover
                          </button>
                        </div>
                        <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                          <span className="fld">Corpo (expressão SQL)</span>
                          <textarea
                            className="nm"
                            data-testid={`fn-corpo-${i}`}
                            rows={3}
                            style={{
                              fontWeight: 400,
                              fontFamily: "ui-monospace,Menlo,monospace",
                              fontSize: 12.5,
                              resize: "vertical",
                            }}
                            value={f.corpo}
                            placeholder="ex.: replace(valor, '/', '-')"
                            aria-invalid={!!problema}
                            onChange={(e) => setFn(i, { corpo: e.target.value })}
                          />
                        </label>
                        <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                          <span className="fld">Descrição (opcional)</span>
                          <input
                            className="nm"
                            data-testid={`fn-descricao-${i}`}
                            style={{ fontWeight: 400 }}
                            value={f.descricao ?? ""}
                            placeholder="o que esta função faz"
                            onChange={(e) => setFn(i, { descricao: e.target.value })}
                          />
                        </label>
                      </div>
                      )}
                      {/* O problema aparece MESMO recolhida: é o que faz o botão de salvar recusar,
                          e escondê-lo atrás de um clique deixaria a recusa sem causa visível. */}
                      {problema && (
                        <p
                          className="exec-erro"
                          data-testid={`fn-problema-${i}`}
                          role="alert"
                          style={{ margin: 0, fontSize: 12 }}
                        >
                          {problema}
                        </p>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
            {ocultas > 0 && (
              <p
                data-testid="fn-ocultas"
                style={{ margin: "10px 0 0", fontSize: 12, color: "var(--ink-2)" }}
              >
                {ocultas === 1 ? "1 função oculta pela busca" : `${ocultas} funções ocultas pela busca`}
                {problemasOcultos > 0 && (
                  <span className="exec-erro" style={{ fontSize: "inherit" }}>
                    {" "}
                    ·{" "}
                    {problemasOcultos === 1
                      ? "1 delas está com problema"
                      : `${problemasOcultos} delas estão com problema`}
                  </span>
                )}{" "}
                ·{" "}
                <button type="button" className="lk" style={{ fontSize: 12 }} onClick={() => setBusca("")}>
                  mostrar todas
                </button>
              </p>
            )}
            <button type="button" className="addmed" data-testid="add-funcao" onClick={addFn}>
              + Nova função
            </button>
          </div>
        </div>

        {/* Avaliar — testa uma chamada contra valores de amostra (DuckDB efêmero, sem salvar) */}
        <PainelAvaliar bibliotecaId={bibliotecaId} definicaoAtual={montarDefinicao} />

        <div style={{ marginTop: 13, display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <button
            className="btn-stamp"
            data-testid="publicar-funcao"
            onClick={() => void publicar()}
            disabled={salvando || !podeGravar}
            title={podeGravar ? "" : "Corrija as funções marcadas antes de publicar"}
          >
            <Icon name="stamp" cls="ic" /> {salvando ? "Publicando…" : "Publicar biblioteca"}
          </button>
          <button
            className="addmed"
            data-testid="salvar-funcao"
            style={{ marginTop: 0 }}
            onClick={() => void salvar()}
            disabled={salvando || !podeGravar}
            title={podeGravar ? "" : "Corrija as funções marcadas antes de salvar"}
          >
            Salvar rascunho
          </button>
          <span style={{ fontSize: 12, color: "var(--ink-2)" }}>
            {qtdProblemas > 0
              ? "Corrija as funções marcadas para poder salvar."
              : "Publique para as funções valerem nos pipelines."}
          </span>
        </div>
        <div role="status" aria-live="polite">
          {aviso && (
            <p style={{ marginTop: 8, fontSize: 12.5, color: "var(--ink-2)" }}>{aviso}</p>
          )}
          {pub && (
            <div className="run-banner" style={{ marginTop: 12 }}>
              <span className="dot" />
              <span>
                <b>Biblioteca publicada (versão {baseNumero})</b> · funções disponíveis para anexar a
                um pipeline.
              </span>
            </div>
          )}
        </div>
      </div>
    </>
  );
}

// ============ PAINEL AVALIAR ============
// Testa uma chamada (ex.: tratar_identificador('ReplaceBarForHyphen','AB/12','OBRA')) contra a
// definição EM EDIÇÃO (sem precisar salvar) — auxílio de edição do Studio.
function PainelAvaliar({
  bibliotecaId,
  definicaoAtual,
}: {
  bibliotecaId: string;
  definicaoAtual: () => api.BibliotecaDefinicao;
}) {
  const [chamada, setChamada] = useState("");
  const [resultado, setResultado] = useState<string | null>(null);
  const [temResultado, setTemResultado] = useState(false);
  const [erro, setErro] = useState("");
  const [avaliando, setAvaliando] = useState(false);

  async function avaliar() {
    const expr = chamada.trim();
    if (!expr) {
      setErro("Informe uma chamada para avaliar. Ex.: minha_funcao('valor').");
      return;
    }
    setAvaliando(true);
    setErro("");
    setTemResultado(false);
    try {
      // Envia a definição EM EDIÇÃO no corpo → testa o rascunho atual sem salvar.
      const r = await api.avaliarFuncao(bibliotecaId, expr, definicaoAtual());
      setResultado(r.resultado);
      setTemResultado(true);
    } catch (e: unknown) {
      setErro(erroDe(e, "Falha ao avaliar a função.").mensagem);
    } finally {
      setAvaliando(false);
    }
  }

  return (
    <div className="cstep" data-testid="painel-avaliar">
      <div className="ch">
        <span className="n">✓</span>
        <h3>Avaliar</h3>
        <span className="hint">roda a chamada contra a definição em edição, sem salvar</span>
      </div>
      <div className="cb">
        <div style={{ display: "flex", gap: 8, alignItems: "flex-end", flexWrap: "wrap" }}>
          <label style={{ display: "flex", flexDirection: "column", gap: 4, flex: "1 1 360px" }}>
            <span className="fld">Chamada</span>
            <input
              className="nm"
              data-testid="avaliar-chamada"
              style={{ fontWeight: 400, fontFamily: "ui-monospace,Menlo,monospace", fontSize: 12.5 }}
              value={chamada}
              placeholder="ex.: tratar_identificador('ReplaceBarForHyphen','AB/12','OBRA')"
              onChange={(e) => setChamada(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void avaliar();
              }}
            />
          </label>
          <button
            className="addmed"
            data-testid="avaliar-funcao"
            style={{ marginTop: 0 }}
            onClick={() => void avaliar()}
            disabled={avaliando}
          >
            {avaliando ? "Avaliando…" : "Avaliar"}
          </button>
        </div>
        <div role="status" aria-live="polite">
          {erro && (
            <p
              className="exec-erro"
              style={{ marginTop: 10, marginBottom: 0, fontSize: 12.5 }}
              data-testid="avaliar-erro"
            >
              {erro}
            </p>
          )}
          {temResultado && !erro && (
            <div
              className="run-banner"
              data-testid="avaliar-resultado"
              style={{ marginTop: 10, marginBottom: 0 }}
            >
              <span className="dot" />
              <span>
                resultado:{" "}
                <b style={{ fontFamily: "ui-monospace,Menlo,monospace" }}>
                  {resultado === null ? "NULL" : resultado === "" ? "(vazio)" : resultado}
                </b>
              </span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ---- Helpers ----
function novaFuncao(): FuncaoDef {
  return { nome: "", args: [], corpo: "", retorno: "scalar" };
}

// Normaliza uma função vinda do JSON (tolera args/retorno ausentes).
function normalizar(f: any): FuncaoDef {
  return {
    nome: typeof f?.nome === "string" ? f.nome : "",
    args: Array.isArray(f?.args) ? f.args.map((a: any) => String(a)) : [],
    corpo: typeof f?.corpo === "string" ? f.corpo : "",
    retorno: f?.retorno === "struct" ? "struct" : "scalar",
    descricao: typeof f?.descricao === "string" ? f.descricao : undefined,
  };
}
