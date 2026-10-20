import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import * as api from "../../api/client";
import { SeletorVersao } from "../SeletorVersao";
import { campoSistema, ConfirmarAcao, Icon, ListaCadastro, type ItemCadastro } from "../ui";
import { diaSP, fmtHora, rotuloDia } from "../execucao/formato";
import { type EstadoView, estadosDoResumo, estadoViewDoResumo, type Fase, usoEmPaginas } from "./listasResumo";

// ============================================================================
// View — lista + construtor do pivot sobre um cubo.
//
// A view NÃO calcula: ela escolhe dimensões e MEDIDAS NOMEADAS do cubo e diz como arrumá-las
// (linhas / colunas / valores). Por isso a prévia aqui só reagrupa o resultado da
// consulta; nenhum número é somado pelo front.
//
// `filtros_liberados` é o que o operador pode filtrar no Portal. Era editado por um checkbox
// alimentado pela zona "Filtrar", mas a zona era descartada no salvamento (`filtros: []`) — na
// reabertura a lista sumia da tela e continuava valendo no Portal. Agora a zona É a lista.
// ============================================================================

// --- Helpers locais de API (sem endpoint no client.ts) ---
async function jsonOuErro(r: Response, fallback: string): Promise<any> {
  if (r.ok) return r.status === 204 ? null : r.json();
  const j = await r.json().catch(() => null);
  throw new Error(j?.erro?.mensagem ?? fallback);
}
async function criarView(nome: string): Promise<{ id: string }> {
  const r = await api.apiFetch("POST", "/views", { nome });
  return jsonOuErro(r, "Falha ao criar view");
}
async function obterView(
  id: string,
  numero?: number,
): Promise<{ nome: string; numero: number; ultimo?: number; definicao: any }> {
  const r = await api.apiFetch(
    "GET",
    numero != null ? `/views/${id}?numero=${numero}` : `/views/${id}`,
  );
  return jsonOuErro(r, "Falha ao abrir view");
}
async function salvarViewRascunho(
  id: string,
  definicao: unknown,
  base_numero: number,
): Promise<{ numero: number }> {
  const r = await api.apiFetch("PUT", `/views/${id}/rascunho`, { definicao, base_numero });
  return jsonOuErro(r, "Falha ao salvar rascunho da view");
}
async function publicarViewVersao(
  id: string,
  base_numero: number,
  nome?: string,
): Promise<{ numero: number }> {
  const r = await api.apiFetch("POST", `/views/${id}/publicar`, {
    base_numero,
    nota: "publicado pelo Construtor de view",
    nome,
  });
  return jsonOuErro(r, "Falha ao publicar view");
}

// ---- Exibição (pt-BR) ----
const NF = new Intl.NumberFormat("pt-BR");
function plural(n: number, um: string, muitos: string): string {
  return `${NF.format(n)} ${n === 1 ? um : muitos}`;
}
// Lista longa em etiqueta de cabeçalho: enfileirar 20 nomes fazia o muro empurrar o título
// "Prévia do relatório" para três linhas. Primeiros + "+N", lista inteira no `title`.
function resumoLista(itens: string[], limite = 4): { texto: string; titulo: string } {
  const titulo = itens.join(", ");
  if (itens.length <= limite) return { texto: titulo, titulo };
  return { texto: `${itens.slice(0, limite).join(", ")} +${itens.length - limite}`, titulo };
}
// Separador de milhar sobre o TEXTO: DECIMAL chega da API como string e passar por float perderia
// dígito exato.
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
function celulaDim(v: unknown): Celula {
  if (v === null || v === undefined) return { texto: "—", titulo: "sem valor (NULL)", vazia: true };
  if (v === "") return { texto: "(vazio)", titulo: "texto vazio", vazia: true };
  return { texto: String(v) };
}

type ZonaId = "linhas" | "colunas" | "valores" | "filtros";
type Zonas = Record<ZonaId, string[]>;
const ZONAS_VAZIAS: Zonas = { linhas: [], colunas: [], valores: [], filtros: [] };
const ZONAS: ZonaId[] = ["linhas", "colunas", "valores", "filtros"];

// Rótulo, dica e o que a zona aceita. A 4ª zona é a lista de dimensões que o OPERADOR pode
// filtrar no Portal (`filtros_liberados`) — o nome antigo ("Filtrar") deixava entender que a
// própria view filtraria algo aqui, o que nunca aconteceu.
const ZL: Record<ZonaId, { titulo: string; dica: string; aceita: "dim" | "med" }> = {
  linhas: { titulo: "Agrupar por", dica: "cada valor vira uma linha", aceita: "dim" },
  colunas: { titulo: "Dividir em colunas", dica: "opcional · compara lado a lado", aceita: "dim" },
  valores: { titulo: "Somar / medir", dica: "as medidas nomeadas do cubo", aceita: "med" },
  filtros: {
    titulo: "Filtro do operador",
    dica: "opcional · o operador filtra por estes no Portal",
    aceita: "dim",
  },
};

// Caixa natural do clique num campo da prateleira: texto → "Agrupar por", número → "Somar / medir".
const NATURAL: Record<"dim" | "med", ZonaId> = { dim: "linhas", med: "valores" };

function ouLista(itens: string[]): string {
  if (itens.length <= 1) return itens[0] ?? "";
  return `${itens.slice(0, -1).join(", ")} ou ${itens[itens.length - 1]}`;
}

// Estado vazio da caixa: só os caminhos que REALMENTE chegam nela. Clicar num campo da esquerda o
// leva à caixa NATURAL — em "Dividir em colunas" e "Filtro do operador" o clique nunca entrou, e a
// frase antiga ("arraste um campo aqui ou clique num campo da esquerda") era igual nas quatro.
function dicaVazia(z: ZonaId, quantosDoTipo: number): string {
  const aceita = ZL[z].aceita;
  const oQue = aceita === "med" ? "uma medida" : "um campo de texto";
  if (quantosDoTipo === 0) {
    return aceita === "med"
      ? "o cubo escolhido não publica medida para pôr aqui"
      : "o cubo escolhido não tem campo de texto para pôr aqui";
  }
  const caminhos = [`arraste ${oQue} aqui`];
  if (NATURAL[aceita] === z) {
    caminhos.push(aceita === "med" ? "clique numa medida à esquerda" : "clique num campo à esquerda");
  }
  // A caixa está vazia, então todo campo do tipo é candidato: o "+ adicionar" existe.
  caminhos.push("use “+ adicionar” no título");
  return ouLista(caminhos);
}

interface Previa {
  colunas: string[];
  linhas: Record<string, unknown>[];
  execucaoId: string | null;
}
interface Check {
  ok: boolean;
  msg: string;
  bloqueia: boolean;
}
interface Validacao {
  ok: boolean;
  checks: Check[];
}

// ============================================================================
// LISTA — estado real de cada view
//
// `GET /views` devolve só `{id, nome, sistema}`. Sem enriquecer, toda linha repetia "abrir para
// editar o pivot sobre o cubo" — e omitia justamente **de qual cubo a view sai**, que é o dado que
// define uma view. Sem "usada em N páginas", mexer num cubo é adivinhar o que quebra.
// O estado de cada linha e o uso em páginas vêm no `resumo` da própria listagem
// (`GET /views?resumo=true`, uma consulta no servidor; ver `listasResumo.ts`). Antes eram duas
// chamadas por view e uma por página publicada — centenas de idas pelo gateway numa navegação.
// ============================================================================

function Fraco({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <span style={{ color: "var(--ink-3)" }} title={title}>
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

export default function View() {
  const [aberto, setAberto] = useState<string | null>(null);
  const [viewsRaw, setViewsRaw] = useState<api.PipelineResumo[]>([]);
  const [ehAdmin, setEhAdmin] = useState(false);
  const [sistemaBusy, setSistemaBusy] = useState<string | null>(null);
  const [carregando, setCarregando] = useState(true);
  const [erroLista, setErroLista] = useState<string | null>(null);

  // Enriquecimento (ver bloco acima). `serieEnriq` = "última carga vence".
  const [estado, setEstado] = useState<Record<string, Fase<EstadoView>>>({});
  const [nomeCubo, setNomeCubo] = useState<Record<string, string>>({});
  const [emPaginas, setEmPaginas] = useState<Fase<{ mapa: Record<string, number>; ocultas: number }>>(
    { fase: "carregando" },
  );
  const serieEnriq = useRef(0);

  const enriquecer = useCallback((lista: api.ViewComResumo[]) => {
    const meu = ++serieEnriq.current;
    setEstado(estadosDoResumo(lista, estadoViewDoResumo));
    setEmPaginas(usoEmPaginas(lista));

    // Nomes dos cubos: UMA chamada resolve a origem de todas as linhas (a definição guarda só o id).
    void api
      .listarCubos()
      .then((cs) => {
        if (meu === serieEnriq.current) setNomeCubo(Object.fromEntries(cs.map((c) => [c.id, c.nome])));
      })
      .catch(() => undefined);
  }, []);

  const recarregar = useCallback(async () => {
    setCarregando(true);
    setErroLista(null);
    try {
      const lista = await api.listarViewsComResumo();
      setViewsRaw(lista);
      enriquecer(lista);
    } catch (e) {
      setErroLista(e instanceof Error ? e.message : "Falha ao carregar as views.");
    } finally {
      setCarregando(false);
    }
  }, [enriquecer]);

  useEffect(() => {
    if (!aberto) void recarregar();
  }, [aberto, recarregar]);

  useEffect(() => {
    void api
      .meAtual()
      .then((u) => setEhAdmin(u.perfil === "admin"))
      .catch(() => setEhAdmin(false));
  }, []);

  async function alternarSistema(id: string, sistema: boolean) {
    setErroLista(null);
    setSistemaBusy(id);
    setViewsRaw((l) => l.map((v) => (v.id === id ? { ...v, sistema } : v)));
    try {
      await api.definirSistema("views", id, sistema);
    } catch (e) {
      setViewsRaw((l) => l.map((v) => (v.id === id ? { ...v, sistema: !sistema } : v)));
      setErroLista(e instanceof Error ? e.message : "Falha ao alterar a marca de sistema.");
    } finally {
      setSistemaBusy(null);
    }
  }

  const views: ItemCadastro[] = useMemo(
    () =>
      viewsRaw.map((v) => {
        const f = estado[v.id];
        const e = f?.fase === "ok" ? f.dado : null;

        // Sub-linha do nome = O CUBO. É o dado que define uma view; a lista antes não o mostrava.
        let meta = "lendo o cubo de origem…";
        if (f?.fase === "erro") meta = "não foi possível ler a definição desta view";
        else if (e && !e.cuboId) meta = "sem cubo — a view não tem de onde tirar número";
        else if (e) {
          const nome = nomeCubo[e.cuboId];
          meta = nome
            ? `cubo ${nome}`
            : `cubo ${e.cuboId.slice(0, 8)} — não está na sua lista de cubos`;
        }

        const pill = !e
          ? { label: f?.fase === "erro" ? "não lido" : "lendo…", cls: "off" }
          : !e.publicada
            ? { label: "nunca publicada", cls: "off" }
            : e.rascunhoAdiante
              ? { label: `rascunho v${e.rascunhoAdiante}`, cls: "skip" }
              : { label: "publicada", cls: "ok" };

        return {
          id: v.id,
          nome: v.nome,
          meta,
          status: pill.label,
          scls: pill.cls,
          campos: [
            {
              // Uma coluna só (agrupamento + medidas + filtro liberado). Em colunas separadas os
              // cabeçalhos (que não quebram) roubavam a largura da coluna do NOME, e os nomes de
              // view quebravam em três linhas.
              rotulo: "Montagem",
              valor: celFase(f, (d) => (
                <span
                  title={
                    `${d.agrupa} campo(s) agrupando` +
                    (d.divide ? `, ${d.divide} dividindo em colunas` : "") +
                    `, ${d.medidas} medida(s) do cubo, ${d.liberados} filtro(s) liberado(s) ao operador`
                  }
                >
                  {d.agrupa ? (
                    `${plural(d.agrupa, "campo", "campos")}${d.divide ? ` (+${NF.format(d.divide)} em coluna)` : ""}`
                  ) : (
                    <Fraco title="sem agrupamento a view não pode ser publicada">sem agrupamento</Fraco>
                  )}
                  {" · "}
                  {d.medidas ? (
                    plural(d.medidas, "medida", "medidas")
                  ) : (
                    <Fraco title="sem medida a view não mostra número nenhum">sem medida</Fraco>
                  )}
                  {" · "}
                  {d.liberados ? (
                    plural(d.liberados, "filtro", "filtros")
                  ) : (
                    <Fraco title="o operador vê o relatório inteiro, sem filtrar">sem filtro</Fraco>
                  )}
                </span>
              )),
            },
            {
              rotulo: "Usada em",
              valor: celFase(emPaginas, ({ mapa, ocultas }) => {
                const n = mapa[v.id] ?? 0;
                const ressalva = ocultas
                  ? ` (${plural(ocultas, "página de sistema", "páginas de sistema")} não pôde ser verificada com o seu perfil)`
                  : "";
                return n ? (
                  <span title={`páginas publicadas no catálogo que exibem esta view${ressalva}`}>
                    {plural(n, "página", "páginas")}
                    {ocultas > 0 && <Fraco title={ressalva}> +?</Fraco>}
                  </span>
                ) : (
                  <Fraco title={`nenhuma página publicada exibe esta view${ressalva}`}>
                    {ocultas > 0 ? "nenhuma visível" : "nenhuma página"}
                  </Fraco>
                );
              }),
            },
            {
              rotulo: "Publicada (GMT-3)",
              valor: celFase(f, (d) =>
                d.publicada ? (
                  // nowrap: carimbo quebrado em "v8 ·" / "25/06/2026 12:13" não se lê como um momento.
                  <span style={{ whiteSpace: "nowrap" }} title="versão que a página e o Portal enxergam">
                    v{d.publicada.numero} · {quando(d.publicada.em)}
                  </span>
                ) : (
                  <Fraco title="só a versão publicada chega ao Portal">nunca</Fraco>
                ),
              ),
            },
            campoSistema(ehAdmin, v.id, v.sistema === true),
          ].filter((x): x is NonNullable<typeof x> => x !== null),
        };
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [viewsRaw, ehAdmin, sistemaBusy, estado, nomeCubo, emPaginas],
  );

  async function novo() {
    setErroLista(null);
    try {
      // Fuso explícito: o nome default carrega uma data, e no navegador de quem estivesse fora do
      // Brasil ela nascia de outro dia que não o do produto (America/Sao_Paulo).
      const hoje = new Date().toLocaleDateString("pt-BR", { timeZone: "America/Sao_Paulo" });
      const { id } = await criarView(`Nova view ${hoje}`);
      setAberto(id);
    } catch (e) {
      setErroLista(e instanceof Error ? e.message : "Falha ao criar view.");
    }
  }

  // Sem `window.confirm`: o GridLista já confirma no modal do design system.
  async function excluir(id: string) {
    setErroLista(null);
    try {
      await api.excluirView(id);
      await recarregar();
    } catch (e) {
      setErroLista(e instanceof Error ? e.message : "Falha ao excluir view.");
    }
  }

  async function renomear(id: string, nome: string) {
    setErroLista(null);
    try {
      await api.renomearView(id, nome);
      await recarregar();
    } catch (e) {
      setErroLista(e instanceof Error ? e.message : "Falha ao renomear view.");
    }
  }

  if (aberto) return <Construtor viewId={aberto} onVoltar={() => setAberto(null)} />;

  // Carregando / falhou / vazio são estados DIFERENTES (o mesmo card dizia "Nada cadastrado
  // ainda" nos três casos).
  if (carregando && viewsRaw.length === 0) {
    return (
      <>
        <div className="page-head">
          <h1 className="display">Views</h1>
        </div>
        <div className="pad">
          <div className="card lc-empty" role="status">
            Carregando views…
          </div>
        </div>
      </>
    );
  }

  if (erroLista && viewsRaw.length === 0) {
    return (
      <>
        <div className="page-head">
          <h1 className="display">Views</h1>
        </div>
        <div className="pad">
          <div className="exec-banner-falha" role="alert">
            <span className="dot" />
            <span>{erroLista}</span>
            <button type="button" className="pag-btn" onClick={() => void recarregar()}>
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
        titulo="Views"
        novoLabel="Nova view"
        itens={views}
        contagem={plural(views.length, "view", "views")}
        carregando={carregando && viewsRaw.length === 0}
        onNovo={() => void novo()}
        onAbrir={(id) => setAberto(id)}
        onExcluir={(id) => void excluir(id)}
        onRenomear={(id, nomeAtual) => void renomear(id, nomeAtual)}
        onSistema={ehAdmin ? (id, s) => void alternarSistema(id, s) : undefined}
        ehSistema={(it) => viewsRaw.find((v) => v.id === it.id)?.sistema === true}
      />
    </>
  );
}

// ============================ CONSTRUTOR ============================
function Construtor({ viewId, onVoltar }: { viewId: string; onVoltar: () => void }) {
  const [nome, setNome] = useState("");
  const [nomeSalvo, setNomeSalvo] = useState("");
  const [cubos, setCubos] = useState<{ id: string; nome: string }[]>([]);
  const [cuboId, setCuboId] = useState<string>("");
  const [dimsDisp, setDimsDisp] = useState<string[]>([]);
  const [medsDisp, setMedsDisp] = useState<string[]>([]);
  const [fmtMed, setFmtMed] = useState<Record<string, string>>({});
  const [cuboCarregando, setCuboCarregando] = useState(false);
  const [erroCubo, setErroCubo] = useState<string | null>(null);
  const [zonas, setZonas] = useState<Zonas>(ZONAS_VAZIAS);
  // Filtros FIXOS da definição (objetos {dimensao,op,valor}) — esta tela não os edita, mas
  // preserva no salvamento: sobrescrevê-los com [] apagaria definição alheia.
  const [filtrosFixos, setFiltrosFixos] = useState<unknown[]>([]);
  const [over, setOver] = useState<ZonaId | null>(null);
  const [busca, setBusca] = useState("");
  const [recado, setRecado] = useState("");

  const [previa, setPrevia] = useState<Previa | null>(null);
  const [previaErro, setPreviaErro] = useState<string | null>(null);
  const [carregandoPrevia, setCarregandoPrevia] = useState(false);
  const [val, setVal] = useState<Validacao | null>(null);
  const [pub, setPub] = useState<number | null>(null);
  const [aviso, setAviso] = useState("");
  const [confirmando, setConfirmando] = useState<null | "publicar" | "voltar">(null);

  const [baseNumero, setBaseNumero] = useState(0);
  const [carregando, setCarregando] = useState(true);
  const [erro, setErro] = useState<string | null>(null);
  const [salvando, setSalvando] = useState(false);
  const [assinaturaSalva, setAssinaturaSalva] = useState("");

  // Versões. `versaoSel` = versão CARREGADA (null = última). baseNumero = nº máximo (lock).
  const [versoes, setVersoes] = useState<api.VersaoResumo[]>([]);
  const [versaoSel, setVersaoSel] = useState<number | null>(null);
  const ultima = versoes.length ? versoes[0].numero : null;

  const carregarVersoes = useCallback(() => {
    api.listarVersoesView(viewId).then(setVersoes).catch(() => setVersoes([]));
  }, [viewId]);

  const aplicarView = useCallback((view: any) => {
    setNome(view.nome ?? "");
    setNomeSalvo(view.nome ?? "");
    setBaseNumero(view.ultimo ?? view.numero ?? 0); // lock = máximo
    const d = view.definicao || {};
    const cid = d.cubo_id ? String(d.cubo_id) : "";
    setCuboId(cid);
    const z: Zonas = cid
      ? {
          linhas: d.linhas ?? [],
          colunas: d.colunas ?? [],
          valores: d.valores ?? [],
          // A zona do operador é `filtros_liberados` (lista de nomes). `filtros` guarda filtro
          // FIXO (objeto) e não cabe em chip.
          filtros: d.filtros_liberados ?? [],
        }
      : ZONAS_VAZIAS;
    setZonas(z);
    setFiltrosFixos(Array.isArray(d.filtros) ? d.filtros : []);
    setAssinaturaSalva(assinar(cid, z));
  }, []);

  // Última chamada vence (mesma régua do Pipeline): abrir o construtor dispara MAIS de uma carga do
  // detalhe — o StrictMode dobra o efeito de montagem no dev, e trocar de versão depressa faz o
  // mesmo em produção. Aplicar qualquer resposta deixava a primeira a chegar mostrar o formulário e
  // a outra, chegando DEPOIS de a pessoa digitar, reescrever o nome com o default: a view era
  // publicada como "Nova view …" (medido no trace do `criar-pagina.spec.ts` com a suíte em
  // paralelo). Só a carga mais recente aplica, e só ela tira o "carregando".
  const serieCarga = useRef(0);
  const carregar = useCallback(
    async (numero?: number) => {
      const minha = ++serieCarga.current;
      setCarregando(true);
      setErro(null);
      try {
        const det = await obterView(viewId, numero);
        if (minha !== serieCarga.current) return;
        aplicarView(det);
      } catch (e) {
        if (minha !== serieCarga.current) return;
        setErro(e instanceof Error ? e.message : "Falha ao abrir o construtor.");
      } finally {
        if (minha === serieCarga.current) setCarregando(false);
      }
    },
    [viewId, aplicarView],
  );

  useEffect(() => {
    api
      .listarCubos()
      .then((lc) => setCubos(lc.map((c) => ({ id: c.id, nome: c.nome }))))
      .catch(() => setCubos([]));
    carregarVersoes();
    void carregar();
  }, [carregar, carregarVersoes]);

  const selecionarVersao = async (numero: number | null) => {
    setVersaoSel(numero);
    setErro(null);
    setAviso("");
    setPub(null);
    setPrevia(null);
    setVal(null);
    await carregar(numero ?? undefined);
  };

  // Dimensões/medidas do cubo escolhido. "Última chamada vence": trocar de cubo duas vezes rápido
  // não pode deixar na tela os campos do cubo anterior.
  const serieCubo = useRef(0);
  useEffect(() => {
    if (!cuboId) {
      setDimsDisp([]);
      setMedsDisp([]);
      setFmtMed({});
      setErroCubo(null);
      return;
    }
    const meu = ++serieCubo.current;
    setCuboCarregando(true);
    setErroCubo(null);
    (async () => {
      try {
        const c = await api.obterCubo(cuboId);
        if (meu !== serieCubo.current) return;
        const d = c.definicao || {};
        setDimsDisp((d.dimensoes ?? []).map((x: any) => String(x.nome)));
        setMedsDisp((d.medidas ?? []).map((x: any) => String(x.nome)));
        const f: Record<string, string> = {};
        for (const m of d.medidas ?? []) f[String(m.nome)] = String(m.formato ?? "Número");
        setFmtMed(f);
      } catch (e) {
        if (meu !== serieCubo.current) return;
        setDimsDisp([]);
        setMedsDisp([]);
        setErroCubo(e instanceof Error ? e.message : "Falha ao carregar o cubo.");
      } finally {
        if (meu === serieCubo.current) setCuboCarregando(false);
      }
    })();
  }, [cuboId]);

  // Tipo do campo NO CUBO. "fora" = a view referencia um campo que o cubo (na versão publicada)
  // não tem mais — a consulta responde 400 e é isso que a tela precisa mostrar.
  const tipoDe = useCallback(
    (campo: string): "dim" | "med" | "fora" =>
      medsDisp.includes(campo) ? "med" : dimsDisp.includes(campo) ? "dim" : "fora",
    [dimsDisp, medsDisp],
  );

  const usados = useMemo(
    () => new Set([...zonas.linhas, ...zonas.colunas, ...zonas.valores]),
    [zonas],
  );
  // Enquanto o cubo não chega, TODO campo pareceria "fora do cubo" — o alerta piscaria a cada
  // abertura. Só depois de ter a definição na mão a comparação significa algo.
  const cuboPronto = !!cuboId && !cuboCarregando && !erroCubo;
  const foraDoCubo = useMemo(
    () => (cuboPronto ? ZONAS.flatMap((z) => zonas[z]).filter((c) => tipoDe(c) === "fora") : []),
    [cuboPronto, zonas, tipoDe],
  );

  function limparResultado() {
    setVal(null);
    setPub(null);
    setPrevia(null);
    setPreviaErro(null);
  }

  function escolherCubo(id: string) {
    setCuboId(id);
    setZonas(ZONAS_VAZIAS);
    setFiltrosFixos([]);
    setRecado("");
    limparResultado();
  }

  // Em qual das três zonas de POSIÇÃO o campo está agora (exclusivas entre si). "Filtro do
  // operador" não entra: ali é permissão, e o mesmo campo pode agrupar E ser filtrável.
  const ondeEsta = useCallback(
    (campo: string): ZonaId | null =>
      (["linhas", "colunas", "valores"] as ZonaId[]).find((k) => zonas[k].includes(campo)) ?? null,
    [zonas],
  );

  // Um campo só entra onde faz sentido: medida em "Agrupar por" (ou dimensão em "Somar / medir")
  // vira 400 do cubo ("Dimensão 'Qtd' não existe") — recusar na hora explica melhor.
  function mover(zona: ZonaId, campo: string) {
    if (!campo) return;
    const t = tipoDe(campo);
    if (t === "fora") {
      setRecado(`«${campo}» não existe no cubo escolhido.`);
      return;
    }
    if (ZL[zona].aceita !== t) {
      setRecado(
        t === "med"
          ? `«${campo}» é medida: só entra em "Somar / medir".`
          : `«${campo}» é dimensão: "Somar / medir" aceita só medidas do cubo.`,
      );
      return;
    }
    // Nada mudou: dizer "«X» em Agrupar por" aqui anunciava uma ação que não aconteceu.
    if (zonas[zona].includes(campo)) {
      setRecado(`«${campo}» já está em ${ZL[zona].titulo}.`);
      return;
    }
    const de = zona === "filtros" ? null : ondeEsta(campo);
    limparResultado();
    setZonas((z) => {
      if (zona === "filtros") {
        return z.filtros.includes(campo) ? z : { ...z, filtros: [...z.filtros, campo] };
      }
      const nz: Zonas = { ...z };
      for (const k of ["linhas", "colunas", "valores"] as ZonaId[]) {
        nz[k] = z[k].filter((x) => x !== campo);
      }
      if (!nz[zona].includes(campo)) nz[zona] = [...nz[zona], campo];
      return nz;
    });
    // Mover entre caixas TIRA da anterior — o retorno diz isso, senão o campo "desaparece" de uma
    // caixa sem explicação.
    setRecado(
      de ? `«${campo}» movido de ${ZL[de].titulo} para ${ZL[zona].titulo}.` : `«${campo}» em ${ZL[zona].titulo}.`,
    );
  }

  function remover(zona: ZonaId, campo: string) {
    limparResultado();
    setZonas((z) => ({ ...z, [zona]: z[zona].filter((x) => x !== campo) }));
    setRecado(`«${campo}» removido de ${ZL[zona].titulo}.`);
  }

  const drag = (e: React.DragEvent, id: string) => e.dataTransfer.setData("f", id);
  // Zona natural do clique — o caminho sem mouse (arrastar exige mouse: WCAG 2.1.1). Sai do mesmo
  // mapa que a legenda e a dica da caixa vazia usam.
  const zonaNatural = (campo: string): ZonaId => NATURAL[tipoDe(campo) === "med" ? "med" : "dim"];

  const camposVisiveis = useCallback(
    (lista: string[]) => {
      const q = busca.trim().toLowerCase();
      return q ? lista.filter((c) => c.toLowerCase().includes(q)) : lista;
    },
    [busca],
  );

  const definicao = useCallback(
    () => ({
      cubo_id: cuboId,
      linhas: zonas.linhas,
      colunas: zonas.colunas,
      valores: zonas.valores,
      filtros: filtrosFixos,
      filtros_liberados: zonas.filtros,
    }),
    [cuboId, zonas, filtrosFixos],
  );

  const sujo = assinar(cuboId, zonas) !== assinaturaSalva;

  async function carregarPrevia(): Promise<Previa | null> {
    if (!cuboId) {
      setPreviaErro("Escolha um cubo primeiro.");
      return null;
    }
    setCarregandoPrevia(true);
    setPreviaErro(null);
    try {
      const dims = [...zonas.linhas, ...zonas.colunas];
      const res = await api.consultarCubo(cuboId, dims, zonas.valores);
      const p: Previa = {
        colunas: res.colunas ?? [],
        linhas: res.linhas ?? [],
        execucaoId: res.origem?.execucao_id ? String(res.origem.execucao_id) : null,
      };
      setPrevia(p);
      return p;
    } catch (e) {
      setPreviaErro(e instanceof Error ? e.message : "Falha na prévia.");
      setPrevia(null);
      return null;
    } finally {
      setCarregandoPrevia(false);
    }
  }

  // Gate de publicação. "A prévia trouxe linhas" é AVISO, não bloqueio: snapshot vazio (pipeline
  // que hoje não achou dado) não é view errada — bloquear aí travava trabalho legítimo.
  async function validar() {
    const p = await carregarPrevia();
    const n = p ? p.linhas.length : 0;
    const checks: Check[] = [
      { ok: !!cuboId, msg: "Cubo escolhido", bloqueia: true },
      { ok: zonas.linhas.length > 0, msg: 'Ao menos um campo em "Agrupar por"', bloqueia: true },
      {
        ok: zonas.valores.length > 0,
        msg: 'Ao menos uma medida em "Somar / medir"',
        bloqueia: true,
      },
      {
        ok: foraDoCubo.length === 0,
        msg:
          foraDoCubo.length === 0
            ? "Todos os campos existem no cubo"
            : `Campos que não existem mais no cubo: ${foraDoCubo.join(", ")}`,
        bloqueia: true,
      },
      { ok: !!p, msg: "A consulta ao cubo funcionou", bloqueia: true },
      {
        ok: !!p && n > 0,
        msg: p
          ? n > 0
            ? `A prévia trouxe ${plural(n, "linha", "linhas")}`
            : "A prévia não trouxe linhas — o snapshot da execução está vazio (não impede publicar)"
          : "A prévia não pôde ser gerada",
        bloqueia: false,
      },
    ];
    setVal({ ok: checks.every((c) => c.ok || !c.bloqueia), checks });
  }

  async function salvarRascunho() {
    setSalvando(true);
    setErro(null);
    setAviso("");
    try {
      const { numero } = await salvarViewRascunho(viewId, definicao(), baseNumero);
      setBaseNumero(numero);
      setVersaoSel(null);
      setAssinaturaSalva(assinar(cuboId, zonas));
      carregarVersoes();
      setAviso(`Rascunho salvo como versão v${numero} (ainda não publicada).`);
      return numero;
    } catch (e) {
      setErro(e instanceof Error ? e.message : "Falha ao salvar o rascunho.");
      return null;
    } finally {
      setSalvando(false);
    }
  }

  async function salvarEPublicar() {
    if (!(val && val.ok)) return;
    setSalvando(true);
    setErro(null);
    setAviso("");
    try {
      const { numero } = await salvarViewRascunho(viewId, definicao(), baseNumero);
      const r = await publicarViewVersao(viewId, numero, nome.trim() || undefined);
      setBaseNumero(r.numero);
      setNomeSalvo(nome.trim() || nomeSalvo);
      setAssinaturaSalva(assinar(cuboId, zonas));
      setPub(r.numero);
      setVersaoSel(null);
      carregarVersoes();
    } catch (e) {
      setErro(e instanceof Error ? e.message : "Falha ao salvar e publicar.");
    } finally {
      setSalvando(false);
    }
  }

  const sair = () => (sujo ? setConfirmando("voltar") : onVoltar());

  if (carregando) {
    return (
      <>
        <div className="page-head">
          <button type="button" className="link-back" onClick={onVoltar}>
            ← Views
          </button>
          <h1 className="display">Construtor de view</h1>
          <p className="sub" role="status">
            Carregando a view…
          </p>
        </div>
      </>
    );
  }

  const podePublicar = !!(val && val.ok) && !salvando;
  // Mesmas regras do `validar_view` do backend, ditas antes de gastar um round-trip que voltaria
  // 400 (o rascunho da view TAMBÉM é validado — não é só o publicar).
  const motivoSemSalvar = !cuboId
    ? "Escolha um cubo primeiro."
    : zonas.valores.length === 0
      ? 'Leve ao menos uma medida para "Somar / medir": sem medida a view não é salva nem publicada — é da medida do cubo que sai todo número.'
      : null;
  const podeSalvar = motivoSemSalvar === null;
  const rLinhas = resumoLista(zonas.linhas);
  const rColunas = resumoLista(zonas.colunas);
  const rValores = resumoLista(zonas.valores);
  const layout = { linhas: zonas.linhas, colunas: zonas.colunas, valores: zonas.valores };
  const grade = previa ? pivotar(previa, layout) : null;
  const colsMedida = new Set(
    grade ? grade.colunas.filter((c) => grade.medidaDaColuna[c] !== undefined) : [],
  );

  return (
    <>
      {confirmando === "publicar" && (
        <ConfirmarAcao
          titulo="Publicar a view"
          corpo={
            <>
              Publicar cria a versão <b>v{baseNumero + 2}</b> da view, registra quem publicou na
              trilha de auditoria e passa a valer para as páginas que a usam — inclusive no Portal
              do operador. A versão publicada não pode ser apagada, só substituída.
              {zonas.filtros.length > 0 && (
                <>
                  {" "}
                  O operador poderá filtrar por <b>{zonas.filtros.join(", ")}</b>.
                </>
              )}
            </>
          }
          rotuloConfirmar="Publicar view"
          onConfirmar={() => {
            setConfirmando(null);
            void salvarEPublicar();
          }}
          onCancelar={() => setConfirmando(null)}
        />
      )}
      {confirmando === "voltar" && (
        <ConfirmarAcao
          titulo="Sair sem salvar"
          corpo={<>Há mudanças que não foram salvas nesta view. Se sair agora, elas são perdidas.</>}
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
          ← Views
        </button>
        <div style={{ display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap" }}>
          <h1 className="display" style={{ margin: 0 }}>
            Construtor de view
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
            <span className="ver-aviso" data-testid="view-sujo">
              mudanças não salvas
            </span>
          )}
        </div>
        <p className="sub">
          A view arruma as dimensões e as <b>medidas nomeadas</b> de um cubo em um relatório. Ela não
          calcula nada: todo número vem de uma medida do cubo.
        </p>
      </div>

      <div className="pad">
        {erro && (
          <div className="exec-banner-falha" role="alert" style={{ marginBottom: 12 }}>
            <span className="dot" />
            <span>{erro}</span>
          </div>
        )}

        <div className="note-line">
          <span className="badge">como funciona</span>
          <span>
            Escolha um cubo e leve um campo da esquerda para uma caixa. <b>Arraste</b> chega em
            qualquer caixa; <b>clicar no campo</b> o manda para a caixa natural (
            <Marca tipo="dim">texto</Marca> → <b>Agrupar por</b> ·{" "}
            <Marca tipo="med">número</Marca> → <b>Somar / medir</b>). Nas outras duas, arraste ou
            use o <b>+ adicionar</b> do título dela.
          </span>
        </div>

        <div className="card shelf" style={{ marginBottom: 14, padding: "13px 15px" }}>
          <h4>
            <label htmlFor="view-nome-in">Nome da view</label>
          </h4>
          <input
            id="view-nome-in"
            className="nm"
            data-testid="view-nome"
            value={nome}
            onChange={(e) => setNome(e.target.value)}
            placeholder="ex.: Vendas — resumo por data"
          />
          <p className="vw-nota" style={{ margin: "6px 0 0" }}>
            É o nome que o operador vê no Portal. Gravado ao publicar.
          </p>
        </div>

        <div className="card shelf" style={{ marginBottom: 14, padding: "13px 15px" }}>
          <h4>
            <label htmlFor="view-cubo">Cubo</label>
          </h4>
          <select
            id="view-cubo"
            className="nm"
            value={cuboId}
            onChange={(e) => escolherCubo(e.target.value)}
          >
            <option value="">— escolha um cubo —</option>
            {cubos.map((c) => (
              <option key={c.id} value={c.id}>
                {c.nome}
              </option>
            ))}
          </select>
          <p className="vw-nota" style={{ margin: "6px 0 0" }}>
            {!cuboId ? (
              "A view sempre nasce de um cubo publicado — é dele que vêm as dimensões e as medidas."
            ) : cuboCarregando ? (
              "Lendo as dimensões e medidas do cubo…"
            ) : erroCubo ? (
              <span className="vw-erro-inline">{erroCubo}</span>
            ) : (
              <>
                {plural(dimsDisp.length, "dimensão", "dimensões")} ·{" "}
                {plural(medsDisp.length, "medida", "medidas")} disponíveis
                {dimsDisp.length + medsDisp.length === 0 &&
                  " — este cubo ainda não tem definição publicada com dimensões/medidas."}
              </>
            )}
          </p>
        </div>

        {foraDoCubo.length > 0 && (
          <div className="exec-aviso" role="alert" style={{ marginBottom: 14 }}>
            Esta view usa {plural(foraDoCubo.length, "campo que o cubo não tem", "campos que o cubo não tem")}{" "}
            mais: <b>{foraDoCubo.join(", ")}</b>. Remova-os (ou volte a criá-los no cubo) — a
            consulta responde erro enquanto eles estiverem aqui.
          </div>
        )}

        <div className="build-grid">
          {/* ---------- Prateleira de campos ---------- */}
          <div className="card shelf">
            {dimsDisp.length + medsDisp.length > 10 && (
              <input
                className="nm vw-busca"
                type="search"
                value={busca}
                aria-label="Filtrar campos por nome"
                placeholder="filtrar campos…"
                onChange={(e) => setBusca(e.target.value)}
              />
            )}
            <h4>
              Campos de texto{" "}
              <span className="vw-h4-leve">· agrupar</span>
            </h4>
            {dimsDisp.length === 0 ? (
              <p className="vw-vazio">{cuboId ? "o cubo não tem dimensões" : "escolha um cubo"}</p>
            ) : camposVisiveis(dimsDisp).length === 0 ? (
              <p className="vw-vazio">nenhuma dimensão com «{busca}»</p>
            ) : (
              camposVisiveis(dimsDisp).map((f) => (
                <PillCampo
                  key={f}
                  campo={f}
                  tipo="dim"
                  usado={usados.has(f)}
                  destino={ZL[zonaNatural(f)].titulo}
                  onDragStart={drag}
                  onClick={() => mover(zonaNatural(f), f)}
                />
              ))
            )}
            <h4>
              Números <span className="vw-h4-leve">· medidas do cubo</span>
            </h4>
            {medsDisp.length === 0 ? (
              <p className="vw-vazio">{cuboId ? "o cubo não tem medidas" : "escolha um cubo"}</p>
            ) : camposVisiveis(medsDisp).length === 0 ? (
              <p className="vw-vazio">nenhuma medida com «{busca}»</p>
            ) : (
              camposVisiveis(medsDisp).map((f) => (
                <PillCampo
                  key={f}
                  campo={f}
                  tipo="med"
                  usado={usados.has(f)}
                  destino={ZL.valores.titulo}
                  onDragStart={drag}
                  onClick={() => mover("valores", f)}
                />
              ))
            )}
          </div>

          <div>
            <div className="zones">
              {ZONAS.map((z) => {
                const aceita = ZL[z].aceita;
                const doTipo = aceita === "med" ? medsDisp : dimsDisp;
                // Candidato = tudo do tipo aceito que ainda NÃO está nesta caixa — inclusive o que
                // está em outra caixa, porque arrastar já movia. Excluir o que estava "em uso"
                // deixava a montagem SEM caminho de teclado (WCAG 2.1.1) exatamente no caso comum:
                // cubo com uma dimensão só, já em "Agrupar por", não tinha como ir para "Dividir em
                // colunas" a não ser arrastando com o mouse.
                const candidatos = doTipo.filter((c) => !zonas[z].includes(c));
                return (
                  <div
                    key={z}
                    className={"zone" + (over === z ? " over" : "")}
                    onDragOver={(e) => {
                      e.preventDefault();
                      setOver(z);
                    }}
                    onDragLeave={(e) => {
                      // Sem a guarda, mover o ponteiro para um chip DENTRO da própria caixa
                      // disparava o leave e apagava o realce de "solte aqui".
                      if (!e.currentTarget.contains(e.relatedTarget as Node)) setOver(null);
                    }}
                    onDrop={(e) => {
                      e.preventDefault();
                      mover(z, e.dataTransfer.getData("f"));
                      setOver(null);
                    }}
                  >
                    <h5>
                      {ZL[z].titulo}
                      <span className="zhint">{ZL[z].dica}</span>
                      {/* Caminho de TECLADO (arrastar exige mouse — WCAG 2.1.1). Fica no CABEÇALHO
                          da caixa, não no meio dela: no centro, ele virava o alvo do arrasto e o
                          campo largado em cima do select simplesmente não entrava. */}
                      {candidatos.length > 0 && (
                        <select
                          className="vw-add"
                          // Estreito e sem crescer: com os 130px do padrão, título + dica + select
                          // não caíam na largura da caixa e o cabeçalho quebrava em duas linhas.
                          style={{ flex: "none", maxWidth: 104 }}
                          value=""
                          aria-label={`Adicionar campo em ${ZL[z].titulo}`}
                          onChange={(e) => {
                            mover(z, e.target.value);
                            e.currentTarget.value = "";
                          }}
                        >
                          <option value="">+ adicionar</option>
                          {candidatos.map((c) => {
                            // Escolher um campo que está em outra caixa MOVE (as três caixas de
                            // posição são exclusivas). A opção avisa antes, não depois.
                            const de = z === "filtros" ? null : ondeEsta(c);
                            return (
                              <option key={c} value={c}>
                                {c}
                                {de ? ` — mover de ${ZL[de].titulo}` : ""}
                              </option>
                            );
                          })}
                        </select>
                      )}
                    </h5>
                    <div className="drop-pills">
                      {zonas[z].length === 0 && (
                        <span className="empty">{dicaVazia(z, doTipo.length)}</span>
                      )}
                      {zonas[z].map((id) => {
                        const t = cuboPronto ? tipoDe(id) : ZL[z].aceita;
                        return (
                          <span
                            key={id}
                            className={"zchip " + (t === "med" ? "med" : "dim") + (t === "fora" ? " vw-fora" : "")}
                            title={t === "fora" ? "campo não existe no cubo escolhido" : undefined}
                          >
                            <span className="dt" aria-hidden="true" />
                            {id}
                            {t === "fora" && <span className="vw-fora-tag">fora do cubo</span>}
                            {/* 24×24: o × era um alvo de ~10×14 px (padding 0 2px) para a única
                                ação destrutiva da montagem — abaixo do mínimo do WCAG 2.5.8. */}
                            <button
                              type="button"
                              className="x"
                              style={{
                                width: 24,
                                height: 24,
                                padding: 0,
                                display: "inline-flex",
                                alignItems: "center",
                                justifyContent: "center",
                                borderRadius: "50%",
                              }}
                              aria-label={`Remover ${id} de ${ZL[z].titulo}`}
                              title={`Remover ${id} de ${ZL[z].titulo}`}
                              onClick={() => remover(z, id)}
                            >
                              ×
                            </button>
                          </span>
                        );
                      })}
                    </div>
                  </div>
                );
              })}
            </div>

            {/* Retorno das ações de montagem (também para leitor de tela). */}
            <p className="vw-nota" role="status" aria-live="polite" style={{ margin: "0 0 12px" }}>
              {recado}
            </p>

            <div className="card pivot-wrap">
              <div className="card-h">
                {/* nowrap: com a etiqueta longa ao lado, o título quebrava em três linhas. */}
                <h3 style={{ whiteSpace: "nowrap" }}>Prévia do relatório</h3>
                <span
                  className="tag junto"
                  title={`agrupado por ${rLinhas.titulo || "—"} · medidas ${rValores.titulo || "—"}${
                    zonas.colunas.length ? ` · dividido por ${rColunas.titulo}` : ""
                  }`}
                >
                  {zonas.linhas.length ? `por ${rLinhas.texto}` : "sem agrupamento"} ·{" "}
                  {zonas.valores.length ? rValores.texto : "sem medida"}
                  {zonas.colunas.length ? ` · dividido por ${rColunas.texto}` : ""}
                </span>
                <span className="vw-nota" style={{ marginLeft: "auto" }}>
                  agrega o snapshot pela definição publicada do cubo
                </span>
              </div>

              {previaErro ? (
                <div className="vw-previa-estado vw-erro-inline" role="alert">
                  {previaErro}
                </div>
              ) : carregandoPrevia ? (
                <div className="vw-previa-estado" role="status">
                  Consultando o cubo…
                </div>
              ) : !grade ? (
                <div className="vw-previa-estado">
                  Monte as caixas e clique <b>Validar prévia</b> para gerar o relatório.
                </div>
              ) : grade.linhas.length === 0 ? (
                <div className="vw-previa-estado">
                  A consulta funcionou e não devolveu nenhuma linha — o snapshot da execução usada
                  está vazio. Não é erro da view.
                </div>
              ) : (
                <>
                  {/* Scrollport PRÓPRIO da grade (não o card: o rodapé sairia de vista junto). É
                      ele que faz o `position:sticky` do `thead th` valer — sem altura máxima o
                      cabeçalho sumia com a rolagem da página e o relatório longo virava um bloco de
                      números sem nome de coluna. */}
                  <div style={{ overflow: "auto", maxHeight: "min(62vh, 620px)" }}>
                    <table className="pivot">
                      <thead>
                        <tr>
                          {grade.colunas.map((c) => {
                            const ehMed = colsMedida.has(c);
                            return (
                              <th
                                scope="col"
                                key={c}
                                className={ehMed ? "num" : undefined}
                                // Dimensão é rótulo, não número: `table.pivot` alinhava tudo à
                                // direita (o `table.bmini` do Portal já foi corrigido assim).
                                style={{ textAlign: ehMed ? "right" : "left" }}
                              >
                                {c}
                              </th>
                            );
                          })}
                        </tr>
                      </thead>
                      <tbody>
                        {grade.linhas.map((linha, i) => (
                          <tr key={i}>
                            {grade.colunas.map((c) => {
                              const med = grade.medidaDaColuna[c];
                              const cel =
                                med !== undefined
                                  ? celulaMedida(linha[c], fmtMed[med])
                                  : celulaDim(linha[c]);
                              return (
                                <td
                                  key={c}
                                  className={
                                    (med !== undefined ? "num" : "") +
                                    (cel.vazia ? " cel-vazia" : "")
                                  }
                                  style={{ textAlign: med !== undefined ? "right" : "left" }}
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
                  {/* Sem linha de Total: a view não calcula. Somar no front daria número
                      certo para Soma e errado para Média/Mínimo/Máximo — e nenhum deles sai da
                      definição. Total no relatório é opção do BLOCO da página. */}
                  <div className="vw-previa-estado vw-nota">
                    {plural(grade.linhas.length, "linha", "linhas")}
                    {previa?.execucaoId && <> · snapshot da execução {previa.execucaoId.slice(0, 8)}</>}
                    {" · total do relatório é opção do bloco na página"}
                  </div>
                </>
              )}
            </div>

            {zonas.filtros.length > 0 && (
              <div className="card shelf" style={{ marginTop: 13, padding: "13px 15px" }}>
                <h4>O operador poderá filtrar por</h4>
                <div className="vw-liberados">
                  {zonas.filtros.map((f) => (
                    <span key={f} className="chip">
                      {f}
                    </span>
                  ))}
                </div>
                <p className="vw-nota" style={{ margin: "8px 0 0" }}>
                  No Portal, o operador escolhe o valor destes campos antes de gerar o relatório.
                  Para tirar um, remova-o da caixa <b>Filtro do operador</b>.
                </p>
              </div>
            )}

            {filtrosFixos.length > 0 && (
              <p className="vw-nota" style={{ margin: "10px 0 0" }}>
                Esta definição tem {plural(filtrosFixos.length, "filtro fixo", "filtros fixos")} que
                este construtor não edita — eles são preservados ao salvar.
              </p>
            )}

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
                className="top-action"
                disabled={carregandoPrevia || salvando}
                onClick={() => void validar()}
              >
                {carregandoPrevia ? "Validando…" : "Validar prévia"}
              </button>
              {/* Sem medida o backend RECUSA até o rascunho ("View precisa de ao menos um valor").
                  Habilitado, o botão gastava um round-trip para devolver 400 — e o caminho é o mais
                  comum de todos: escolher um cubo zera as caixas. A mesma regra dita antes. */}
              <button
                type="button"
                className="addmed"
                style={{ marginTop: 0 }}
                disabled={salvando || !podeSalvar}
                title={motivoSemSalvar ?? undefined}
                onClick={() => void salvarRascunho()}
              >
                {salvando ? "Salvando…" : "Salvar rascunho"}
              </button>
              {/* Sem `opacity` inline: `.btn-stamp:disabled` DESSATURA de propósito (um botão cheio
                  a meio-tom continua parecendo clicável) e o opacity vencia essa decisão. */}
              <button
                type="button"
                className="btn-stamp"
                disabled={!podePublicar}
                title={podePublicar ? undefined : "Valide a prévia para liberar a publicação"}
                onClick={() => setConfirmando("publicar")}
              >
                <Icon name="stamp" cls="ic" />{" "}
                {salvando ? "Publicando…" : "Salvar e publicar view"}
              </button>
              {/* O motivo do bloqueio fica VISÍVEL, não só no `title`: tooltip não existe para
                  teclado nem para toque, e é a única explicação de por que o botão não responde. */}
              <span className="vw-nota">
                {motivoSemSalvar
                  ? motivoSemSalvar
                  : val
                    ? val.ok
                      ? "Prévia validada · publicar cria versão numerada e auditada."
                      : "Corrija os itens da validação para liberar a publicação."
                    : "Valide a prévia antes de publicar · versão numerada e auditada."}
              </span>
            </div>

            {val && (
              <div className="card" style={{ marginTop: 12, padding: "12px 14px" }} role="status">
                <div className="eyebrow" style={{ marginBottom: 8 }}>
                  Validação da prévia
                </div>
                {val.checks.map((c, i) => (
                  <div key={i} className="vw-check">
                    <span
                      className={"cdot " + (c.ok ? "ok" : c.bloqueia ? "falha" : "skip")}
                      aria-hidden="true"
                    />
                    <span className={c.ok ? undefined : c.bloqueia ? "vw-erro-inline" : "vw-nota"}>
                      {c.msg}
                    </span>
                  </div>
                ))}
                <div className={"vw-check-fim " + (val.ok ? "ok" : "falha")}>
                  {val.ok
                    ? "Prévia válida — publicação liberada."
                    : "Corrija os itens acima para liberar a publicação."}
                </div>
              </div>
            )}

            <div role="status" aria-live="polite">
              {aviso && (
                <p className="vw-nota" style={{ margin: "9px 0 0" }}>
                  {aviso}
                </p>
              )}
              {pub !== null && (
                <div className="run-banner">
                  <span className="dot" />
                  <span>
                    <b>View publicada — versão v{pub}</b> · já disponível no Construtor de página. A
                    publicação ficou registrada na trilha de auditoria.
                  </span>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </>
  );
}

// Legenda que usa a MESMA marca dos pills, reaproveitando `.zchip.dim/.med .dt` (quadrado azul de
// dimensão, círculo lime de medida). A legenda anterior desenhava glifos de texto que não eram os
// da tela ("● texto · ◆ número" para um quadrado e um círculo) — uma legenda que não bate com o
// que legenda não ajuda a ler nada.
function Marca({ tipo, children }: { tipo: "dim" | "med"; children: ReactNode }) {
  return (
    <b
      className={`zchip ${tipo}`}
      style={{
        background: "none",
        border: "none",
        boxShadow: "none",
        padding: 0,
        fontSize: "inherit",
        color: "inherit",
        gap: 5,
      }}
    >
      <span className="dt" aria-hidden="true" />
      {children}
    </b>
  );
}

// Campo arrastável da prateleira. É um <button>: clicar leva para a caixa natural — arrastar
// sozinho deixava a montagem inacessível por teclado.
function PillCampo({
  campo,
  tipo,
  usado,
  destino,
  onDragStart,
  onClick,
}: {
  campo: string;
  tipo: "dim" | "med";
  usado: boolean;
  destino: string;
  onDragStart: (e: React.DragEvent, id: string) => void;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      className={`fpill ${tipo}` + (usado ? " vw-usado" : "")}
      draggable
      onDragStart={(e) => onDragStart(e, campo)}
      onClick={onClick}
      title={`${campo} — clique para colocar em "${destino}" (ou arraste para qualquer caixa)`}
    >
      <span className="dt" aria-hidden="true" />
      <span className="vw-pill-nome">{campo}</span>
      {usado && <span className="vw-usado-tag">em uso</span>}
      <span className="gr" aria-hidden="true">
        ⠿
      </span>
    </button>
  );
}

// Reagrupa o resultado do cubo (formato longo) conforme o layout da view: as dimensões de
// `colunas` viram colunas. É REARRANJO, não conta — o número de cada célula continua sendo o que
// a medida do cubo devolveu. Espelha o `pivotar` do Portal para que a prévia mostre o
// mesmo desenho do relatório (ver relatório: candidato a módulo compartilhado).
function pivotar(
  p: Previa,
  layout: { linhas: string[]; colunas: string[]; valores: string[] },
): {
  colunas: string[];
  linhas: Record<string, unknown>[];
  medidaDaColuna: Record<string, string>;
} {
  const { linhas: rowDims, colunas: colDims, valores } = layout;
  if (colDims.length === 0 || rowDims.length === 0 || valores.length === 0) {
    const medidaDaColuna: Record<string, string> = {};
    for (const v of valores) if (p.colunas.includes(v)) medidaDaColuna[v] = v;
    return { colunas: p.colunas, linhas: p.linhas, medidaDaColuna };
  }
  const txt = (v: unknown) => (v === null || v === undefined ? "—" : String(v));
  const chaveCol = (l: Record<string, unknown>) => colDims.map((d) => txt(l[d])).join(" · ");
  const chaveLin = (l: Record<string, unknown>) => rowDims.map((d) => txt(l[d])).join(" ");
  const colVals: string[] = [];
  for (const l of p.linhas) {
    const k = chaveCol(l);
    if (!colVals.includes(k)) colVals.push(k);
  }
  colVals.sort((a, b) => a.localeCompare(b, "pt-BR", { numeric: true }));
  const multi = valores.length > 1;
  const rotulo = (cv: string, v: string) => (multi ? `${cv} · ${v}` : cv);
  const colunas = [...rowDims, ...colVals.flatMap((cv) => valores.map((v) => rotulo(cv, v)))];
  const medidaDaColuna: Record<string, string> = {};
  for (const cv of colVals) for (const v of valores) medidaDaColuna[rotulo(cv, v)] = v;
  const grupos = new Map<string, Record<string, unknown>>();
  const ordem: string[] = [];
  for (const l of p.linhas) {
    const rk = chaveLin(l);
    if (!grupos.has(rk)) {
      const base: Record<string, unknown> = {};
      for (const d of rowDims) base[d] = l[d];
      grupos.set(rk, base);
      ordem.push(rk);
    }
    const g = grupos.get(rk)!;
    const cv = chaveCol(l);
    for (const v of valores) g[rotulo(cv, v)] = l[v];
  }
  return { colunas, linhas: ordem.map((rk) => grupos.get(rk)!), medidaDaColuna };
}

// Assinatura do que é editável na tela — só para saber se há mudança não salva.
function assinar(cuboId: string, z: Zonas): string {
  return JSON.stringify([cuboId, z.linhas, z.colunas, z.valores, z.filtros]);
}
