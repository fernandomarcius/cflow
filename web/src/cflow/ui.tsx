import { Fragment, useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
// Import de TIPO apenas (apagado na compilação): o design system não passa a depender do cliente
// da API em runtime, mas a prop de camada fica com o mesmo tipo que a rota devolve — duas
// definições do enum divergiriam no dia em que uma camada nova entrasse.
import type { Camada } from "../api/client";

export { LOGO_URI } from "./logo";

// --- Ícones (stroke inline, sem emoji) — portados do protótipo ---
export type IconName =
  | "studio"
  | "modelo"
  | "portal"
  | "run"
  | "search"
  | "stamp"
  | "arrow"
  | "chevron"
  | "grid"
  | "layout"
  | "download"
  | "relogio"
  | "runs"
  | "plug"
  | "funcao"
  | "pessoas"
  | "chave"
  | "carta";

interface IconProps {
  name: IconName;
  cls?: string;
}

export function Icon({ name, cls }: IconProps) {
  const p = {
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.6,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
  };
  const paths: Record<IconName, ReactNode> = {
    // Conexões/Funções/Usuários/Permissões usavam os ícones de Pipeline e de carimbo: quatro itens
    // do menu com o desenho de outro item, e o ícone deixava de identificar a tela.
    plug: (
      <g {...p}>
        <path d="M7 3v4M13 3v4" />
        <path d="M5 7h10v3a5 5 0 0 1-10 0z" />
        <path d="M10 15v3" />
      </g>
    ),
    funcao: (
      <g {...p}>
        <path d="M12.5 3.5h-1.2A2.3 2.3 0 0 0 9 5.8V7" />
        <path d="M6.5 7h5" />
        <path d="M9 7v6.2A2.3 2.3 0 0 0 11.3 15.5h1.2" />
        <path d="M4.5 16.5c2.5 0 3-3 5.5-3s3 3 5.5 3" />
      </g>
    ),
    pessoas: (
      <g {...p}>
        <circle cx="7.5" cy="7" r="2.6" />
        <path d="M3 16.5c0-2.5 2-4.2 4.5-4.2s4.5 1.7 4.5 4.2" />
        <path d="M13 5.6a2.6 2.6 0 0 1 0 5" />
        <path d="M14.2 12.6c1.8.4 2.8 1.9 2.8 3.9" />
      </g>
    ),
    chave: (
      <g {...p}>
        <circle cx="7" cy="7" r="3.2" />
        <path d="M9.4 9.4l6 6" />
        <path d="M13.4 13.4l1.6-1.6M15.4 15.4l1.6-1.6" />
      </g>
    ),
    // Nó de e-mail (009): envelope. Distinto do `download` (grava dado) e do `arrow` (HTTP) —
    // três nós de efeito com o mesmo desenho não identificariam nada no canvas.
    carta: (
      <g {...p}>
        <rect x="2.5" y="5" width="15" height="10" rx="1.6" />
        <path d="M3 6l7 5 7-5" />
      </g>
    ),
    studio: (
      <g {...p}>
        <circle cx="5" cy="5" r="2.2" />
        <circle cx="5" cy="15" r="2.2" />
        <circle cx="15" cy="10" r="2.2" />
        <path d="M7 6l6 3M7 14l6-3" />
      </g>
    ),
    modelo: (
      <g {...p}>
        <path d="M10 3l7 4v6l-7 4-7-4V7z" />
        <path d="M3 7l7 4 7-4M10 11v6" />
      </g>
    ),
    portal: (
      <g {...p}>
        <rect x="3" y="4" width="14" height="12" rx="1.5" />
        <path d="M3 8h14M7 12h6" />
      </g>
    ),
    run: (
      <g {...p}>
        <path d="M5 4l11 6-11 6z" fill="currentColor" stroke="none" />
      </g>
    ),
    relogio: (
      <g {...p}>
        <circle cx="10" cy="10" r="7" />
        <path d="M10 6v4l3 2" />
      </g>
    ),
    search: (
      <g {...p}>
        <circle cx="8.5" cy="8.5" r="5" />
        <path d="M12.5 12.5L16 16" />
      </g>
    ),
    stamp: (
      <g {...p}>
        <path d="M7 3h6v5l-1.5 2h-3L7 8z" />
        <path d="M4 16h12M5 13h10v3H5z" />
      </g>
    ),
    arrow: (
      <g {...p}>
        <path d="M5 10h9M10 6l4 4-4 4" />
      </g>
    ),
    chevron: (
      <g {...p}>
        <path d="M8 5l5 5-5 5" />
      </g>
    ),
    grid: (
      <g {...p}>
        <rect x="3" y="3" width="6" height="6" rx="1" />
        <rect x="11" y="3" width="6" height="6" rx="1" />
        <rect x="3" y="11" width="6" height="6" rx="1" />
        <rect x="11" y="11" width="6" height="6" rx="1" />
      </g>
    ),
    layout: (
      <g {...p}>
        <rect x="3" y="4" width="14" height="12" rx="1.5" />
        <path d="M3 8h14M10 8v8" />
      </g>
    ),
    runs: (
      <g {...p}>
        <path d="M4 6h11M4 10h11M4 14h6" />
        <circle cx="15.5" cy="14" r="2" fill="currentColor" stroke="none" />
      </g>
    ),
    download: (
      <g {...p}>
        <path d="M10 3v9M6.5 8.5L10 12l3.5-3.5" />
        <path d="M4 14v2.5h12V14" />
      </g>
    ),
  };
  return (
    <svg className={cls} viewBox="0 0 20 20" width="1em" height="1em" aria-hidden="true">
      {paths[name]}
    </svg>
  );
}

// --- Confirmação de ação (modal do design system) ---
// Existe para tirar `window.confirm`/`window.prompt` do caminho das ações irreversíveis das
// listagens: diálogo do navegador fica fora do tema, sem foco controlado e sem o vocabulário do
// produto. Excluir UM NÓ já tinha modal próprio; excluir um pipeline inteiro, não.
export function ConfirmarAcao({
  titulo,
  corpo,
  rotuloConfirmar,
  perigo,
  valorInicial,
  rotuloCampo,
  campoSegredo,
  campoNumero,
  campoOpcoes,
  extra,
  erro,
  onConfirmar,
  onCancelar,
}: {
  titulo: string;
  corpo: ReactNode;
  rotuloConfirmar: string;
  perigo?: boolean;
  // Quando presente, o modal pede um texto (usado por "Renomear").
  valorInicial?: string;
  rotuloCampo?: string;
  // O texto pedido é uma credencial (ex.: "sua senha atual" para desligar o MFA): campo mascarado
  // e sem oferta de salvar do navegador. Sem isto a senha ficava visível na tela.
  campoSegredo?: boolean;
  // Campo numérico (ex.: tamanho da amostra). Mantém o MESMO modal do resto do produto em vez de
  // um diálogo próprio — que teria de reimplementar Escape, foco e ciclo de Tab.
  campoNumero?: boolean;
  // Campo de ESCOLHA fechada (ex.: a camada do pipeline). Um enum de quatro valores num campo de
  // texto livre convida a typo — e o valor não é livre, é uma lista. Continua o MESMO modal
  // (Escape, foco, ciclo de Tab, Enter submete) em vez de um diálogo próprio.
  campoOpcoes?: { valor: string; rotulo: string }[];
  // Conteúdo extra abaixo do campo (ex.: atalhos de valor). Recebe o setter para poder preencher
  // o campo; é render prop justamente porque atalho sem escrever no campo não serve para nada.
  extra?: (definir: (v: string) => void, valor: string) => ReactNode;
  // Recusa da ação (ex.: valor inválido). Fica DENTRO da caixa: o modal cobre a tela, então um
  // aviso renderizado na página atrás dele não é visto — a pessoa clica de novo achando que travou.
  erro?: ReactNode;
  onConfirmar: (texto: string) => void;
  onCancelar: () => void;
}) {
  const [texto, setTexto] = useState(valorInicial ?? "");
  const campoRef = useRef<HTMLInputElement | null>(null);
  const selectRef = useRef<HTMLSelectElement | null>(null);
  const botaoRef = useRef<HTMLButtonElement | null>(null);
  const caixaRef = useRef<HTMLDivElement | null>(null);
  // `useId`: o id do título era fixo, e duas confirmações na mesma tela (Conexões, Usuários)
  // duplicavam o id — o leitor de tela anunciava o diálogo errado.
  const tituloId = useId();
  useEffect(() => {
    // Foco no que a pessoa vai usar primeiro; sem isto o foco ficava no botão atrás do overlay.
    const anterior = document.activeElement as HTMLElement | null;
    (campoRef.current ?? selectRef.current ?? botaoRef.current)?.focus();
    // E devolve ao gatilho ao fechar: sem isto o teclado voltava para o topo do documento e a
    // pessoa refazia o caminho até a linha em que estava.
    return () => anterior?.focus?.();
  }, []);

  // Escape no DOCUMENTO (estava no onKeyDown do overlay: parava de funcionar assim que o foco
  // saía dali) e ciclo de Tab preso na caixa — sem isso o Tab passeava pela tela atrás do modal.
  useEffect(() => {
    const tecla = (ev: KeyboardEvent) => {
      if (ev.key === "Escape") {
        ev.stopPropagation();
        onCancelar();
        return;
      }
      if (ev.key !== "Tab" || !caixaRef.current) return;
      const focaveis = caixaRef.current.querySelectorAll<HTMLElement>(
        'button, input, select, textarea, a[href], [tabindex]:not([tabindex="-1"])',
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
  }, [onCancelar]);
  const confirmar = () => {
    if (valorInicial !== undefined && !texto.trim()) return;
    onConfirmar(texto.trim());
  };
  return (
    <div
      className="modal-fundo"
      role="dialog"
      aria-modal="true"
      aria-labelledby={tituloId}
      onClick={(e) => e.target === e.currentTarget && onCancelar()}
      onKeyDown={(e) => {
        if (e.key === "Enter" && valorInicial !== undefined) confirmar();
      }}
    >
      <div className="modal-caixa" ref={caixaRef}>
        <h3 id={tituloId}>{titulo}</h3>
        <p>{corpo}</p>
        {valorInicial !== undefined && (
          <label className="fld" style={{ display: "block", marginBottom: 14 }}>
            {rotuloCampo ?? "Nome"}
            {campoOpcoes ? (
              <select
                ref={selectRef}
                className="nm"
                value={texto}
                onChange={(e) => setTexto(e.target.value)}
                data-testid="confirmar-campo"
              >
                {campoOpcoes.map((o) => (
                  <option key={o.valor} value={o.valor}>
                    {o.rotulo}
                  </option>
                ))}
              </select>
            ) : (
              <input
                ref={campoRef}
                className="nm"
                type={campoSegredo ? "password" : campoNumero ? "number" : "text"}
                inputMode={campoNumero ? "numeric" : undefined}
                autoComplete={campoSegredo ? "current-password" : undefined}
                value={texto}
                onChange={(e) => setTexto(e.target.value)}
                data-testid="confirmar-campo"
              />
            )}
            {extra?.(setTexto, texto)}
          </label>
        )}
        {erro && (
          <p className="exec-erro" role="alert" data-testid="confirmar-erro">
            {erro}
          </p>
        )}
        <div className="modal-acoes">
          <button type="button" className="btn-sec" onClick={onCancelar}>
            Cancelar
          </button>
          <button
            type="button"
            ref={botaoRef}
            className={perigo ? "btn-perigo" : "btn-stamp"}
            data-testid="confirmar-ok"
            onClick={confirmar}
          >
            {rotuloConfirmar}
          </button>
        </div>
      </div>
    </div>
  );
}

// --- Listagem genérica (reutilizável: Pipeline/Cubo/View/Página) ---
// Campo extra rotulado de um item do grid (ex.: "Página" / "Usuário" numa execução).
export interface CampoItem {
  // Contagem alinha à direita com dígitos tabulares: `.dgrid td.num` já existia no CSS e nada o
  // aplicava, então números de colunas diferentes não se comparavam na vertical.
  num?: boolean;
  rotulo: string;
  valor: ReactNode;
}

export interface ItemCadastro {
  id: string;
  nome: string;
  // `ReactNode` porque a sub-linha carrega o dado que DEFINE a entidade (origem, cubo, views) e
  // precisa poder ter `title` com a lista completa quando a célula corta.
  meta: ReactNode;
  status?: string;
  scls?: string;
  // Rótulo da coluna de status. Default "Status"; telas em que o pill é o desfecho de OUTRA coisa
  // (ex.: a última execução do pipeline) precisam dizer de quê, senão "falha" acusa a entidade.
  statusRotulo?: string;
  campos?: CampoItem[];
  testid?: string; // data-testid opcional no card (preserva hooks de e2e)
}

// Valor que representa "sem camada" no <select> da confirmação. Não é `""` porque o guard de
// `ConfirmarAcao` recusa confirmar com o campo vazio (regra boa: protege Renomear de apagar o
// nome) — e "sem camada" é uma escolha legítima, não um campo em branco. Vira `null` na chamada.
const SEM_CAMADA = "nenhuma";

interface GridListaProps {
  itens: ItemCadastro[];
  onAbrir?: (id: string) => void;
  abrirLabel?: string; // rótulo do botão de abrir (default "Editar")
  onExcluir?: (id: string) => void;
  // Renomear in-line por card: recebe (id, nomeAtual). O próprio Grid pede o novo nome
  // (prompt pt-BR) e só chama de volta se o usuário confirmar um nome não-vazio diferente.
  onRenomear?: (id: string, nomeAtual: string) => void;
  // Ações customizadas por card (sobrepõe os botões padrão). Use quando a tela tem
  // ações próprias (ex.: Testar conexão, Ver execução).
  acoes?: (it: ItemCadastro) => ReactNode;
  // Ações EXTRAS no menu "⋯", ao lado das padrão (ao contrário de `acoes`, não as substitui). Para
  // ação secundária que só uma tela tem — ex.: o carimbo de obsoleto do pipeline, de admin.
  acoesMenu?: (it: ItemCadastro) => ReactNode;
  vazio?: ReactNode; // estado vazio (default: nada)
  // Marca/desmarca "item de sistema" (admin). Ação DELIBERADA com confirmação: ela muda quem vê o
  // item, e como checkbox na linha clicável era o acidente mais barato da tela.
  onSistema?: (id: string, sistema: boolean) => void;
  ehSistema?: (it: ItemCadastro) => boolean;
  // Define (ou remove) a camada do medalhão do item — hoje só pipelines têm camada. Vive no menu
  // "⋯" pelo mesmo motivo da marca de sistema: é escolha deliberada, não um select na linha
  // clicável (a linha abre o editor; um select ali disputa o mesmo pixel do clique de abrir).
  //
  // NÃO confundir com `onSistema`: camada é CLASSIFICAÇÃO (em que aba o item aparece), `sistema` é
  // VISIBILIDADE (quem enxerga o item). São ortogonais — um item pode ser das duas coisas.
  onCamada?: (id: string, camada: Camada | null) => void;
  camadaDe?: (it: ItemCadastro) => Camada | null;
  // Ativa/desativa a EXECUÇÃO do item (hoje só pipelines). Ao contrário de `onSistema` (confirma
  // nos dois sentidos), só DESATIVAR confirma — mesmo padrão de `alternarAtivo` em Agendamentos:
  // religar não é a direção perigosa.
  onAtivo?: (id: string, ativo: boolean) => void;
  ativoDe?: (it: ItemCadastro) => boolean;
  // Nome da tabela para leitor de tela (a tela costuma ter mais de uma listagem).
  rotulo?: string;
  // Enquanto carrega, "Nada cadastrado ainda" é MENTIRA — e era o que toda tela mostrava.
  carregando?: boolean;
  // Depois de FALHAR também é mentira: seis telas escreviam "Nenhum X" logo abaixo da faixa de
  // erro. O componente precisa dos três estados, senão cada tela resolve (ou esquece) por conta.
  erro?: boolean;
}

// `MenuMaisAcoes` é o mesmo menu, mantido como nome para as telas que já o usavam. A implementação
// única é `MenuAcoes` (abaixo): o popup precisa sair do contêiner rolável da tabela, senão fica
// recortado e por baixo da seção seguinte.
export function MenuMaisAcoes({ nome, children }: { nome: string; children: ReactNode }) {
  return <MenuAcoes rotulo={`Mais ações de ${nome}`}>{children}</MenuAcoes>;
}

// Compat: o menu agora fecha sozinho ao acionar qualquer item (o clique no popup o fecha), então
// este helper virou no-op. Mantido para não obrigar as telas a mudarem junto.
export function fecharMenuDoBotao(_e: { currentTarget: HTMLElement }): void {
  /* o próprio MenuAcoes fecha ao clicar em um item */
}

// O elemento está à vista: dentro da janela E da área visível de todo ancestral que recorta
// (overflow diferente de visible). Gatilho rolado para baixo do cabeçalho de um contêiner com
// rolagem continua "dentro da janela", mas não se vê — e um menu ancorado nele flutuaria solto.
function visivelNaTela(el: HTMLElement): boolean {
  const r = el.getBoundingClientRect();
  if (r.width === 0 && r.height === 0) return false;
  if (r.bottom <= 0 || r.top >= window.innerHeight) return false;
  for (let p = el.parentElement; p; p = p.parentElement) {
    const s = getComputedStyle(p);
    if (s.overflowY !== "visible" || s.overflowX !== "visible") {
      const c = p.getBoundingClientRect();
      if (r.bottom <= c.top || r.top >= c.bottom) return false;
    }
  }
  return true;
}

// Menu "⋯" de ações secundárias da linha.
//
// O popup é renderizado em PORTAL com `position:fixed`: a tabela vive dentro de `.dgrid-wrap`, que
// tem `overflow-x:auto` para caber em telas estreitas — e um contêiner com overflow RECORTA
// qualquer filho posicionado, então o menu aparecia cortado e por baixo da seção de baixo. Nenhum
// z-index resolve isso; só sair do contêiner resolve.
export function MenuAcoes({ rotulo, children }: { rotulo: string; children: ReactNode }) {
  const [aberto, setAberto] = useState(false);
  const [caixa, setCaixa] = useState<{
    top: number;
    right: number;
    ancora: { topo: number; base: number };
  } | null>(null);
  const gatilho = useRef<HTMLButtonElement | null>(null);
  const pop = useRef<HTMLDivElement | null>(null);

  const abrir = () => {
    const r = gatilho.current?.getBoundingClientRect();
    if (r)
      setCaixa({
        top: r.bottom + 4,
        right: Math.max(8, window.innerWidth - r.right),
        ancora: { topo: r.top, base: r.bottom },
      });
    setAberto(true);
  };

  // Abre para CIMA quando não cabe embaixo. O popup é `position:fixed`: o que passa da borda de
  // baixo não se alcança nem rolando — na linha do rodapé da lista, o último item do menu (Excluir,
  // ou uma ação extra) ficava fora da tela e inalcançável, e o E2E do carimbo tropeçou nisso.
  // Medido depois de montar e antes de pintar; só regrava quando a posição muda (sem laço).
  useLayoutEffect(() => {
    if (!aberto || !caixa || !pop.current) return;
    const altura = pop.current.getBoundingClientRect().height;
    const cabeEmbaixo = caixa.ancora.base + 4 + altura <= window.innerHeight - 8;
    const topo = cabeEmbaixo ? caixa.ancora.base + 4 : Math.max(8, caixa.ancora.topo - 4 - altura);
    if (topo !== caixa.top) setCaixa({ ...caixa, top: topo });
  }, [aberto, caixa]);
  const fechar = (devolverFoco = false) => {
    setAberto(false);
    if (devolverFoco) gatilho.current?.focus();
  };

  useEffect(() => {
    if (!aberto) return;
    const tecla = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      setAberto(false);
      gatilho.current?.focus();
    };
    // Rolar ou redimensionar com o menu aberto: o popup ACOMPANHA o gatilho e fecha só quando o
    // gatilho sai da vista. Fechar a qualquer rolagem fechava o menu recém-aberto da linha que o
    // navegador acabara de trazer à vista — o evento de rolagem chega um quadro DEPOIS do clique,
    // já com este ouvinte registrado (a inércia do trackpad; o scrollIntoView do Playwright).
    // Medido: 8 de 8 na última linha de uma lista de 160, 0 de 8 na primeira.
    const mover = () => {
      const alvo = gatilho.current;
      if (!alvo || !visivelNaTela(alvo)) {
        setAberto(false);
        return;
      }
      const r = alvo.getBoundingClientRect();
      setCaixa({
        top: r.bottom + 4,
        right: Math.max(8, window.innerWidth - r.right),
        ancora: { topo: r.top, base: r.bottom },
      });
    };
    document.addEventListener("keydown", tecla, true);
    window.addEventListener("scroll", mover, true);
    window.addEventListener("resize", mover);
    return () => {
      document.removeEventListener("keydown", tecla, true);
      window.removeEventListener("scroll", mover, true);
      window.removeEventListener("resize", mover);
    };
  }, [aberto]);

  return (
    <>
      <button
        type="button"
        ref={gatilho}
        className={"dg-mais-btn" + (aberto ? " on" : "")}
        aria-label={rotulo}
        aria-expanded={aberto}
        aria-haspopup="menu"
        title="Mais ações"
        onClick={() => (aberto ? fechar() : abrir())}
      >
        ⋯
      </button>
      {aberto &&
        caixa &&
        createPortal(
          <>
            <div className="dg-mais-fundo" onClick={() => fechar()} />
            <div
              ref={pop}
              className="dg-mais-pop"
              role="menu"
              style={{ position: "fixed", top: caixa.top, right: caixa.right }}
              onClick={() => fechar()}
            >
              {children}
            </div>
          </>,
          document.body,
        )}
    </>
  );
}

// Interruptor (switch) acessível reusando as classes .sw/.on do design system. Promovido de
// Pagina.tsx (nasceu ali, ligando "página parametrizada") para cá: agora o GridLista o usa na
// coluna Ativo — mesma decisão de consolidar `MenuAcoes`, uma implementação em vez de duas.
// O `<span onClick>` original não era alcançável por teclado nem anunciado por leitor de tela.
export function Interruptor({
  ligado,
  rotulo,
  onAlternar,
  testid,
}: {
  ligado: boolean;
  rotulo: string;
  onAlternar: () => void;
  testid?: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={ligado}
      aria-label={rotulo}
      data-testid={testid}
      className={"sw" + (ligado ? " on" : "")}
      // .sw foi escrito para <span>: zera a borda/padding que o <button> traz por padrão.
      style={{ border: "none", padding: 0 }}
      onClick={onAlternar}
    >
      <i />
    </button>
  );
}

// Data grid (TABELA) reutilizável de listagem — linha por item, colunas = nome + campos +
// status + ações. É o ÚNICO componente de listagem do produto (todas as telas, direto ou via
// `ListaCadastro`). Não é card: é tabela/listagem de dados.
export function GridLista({
  itens,
  onAbrir,
  abrirLabel = "Editar",
  onExcluir,
  onRenomear,
  acoes,
  acoesMenu,
  vazio,
  onSistema,
  ehSistema,
  onCamada,
  camadaDe,
  onAtivo,
  ativoDe,
  rotulo,
  carregando,
  erro,
}: GridListaProps) {
  const [pedindo, setPedindo] = useState<{
    tipo: "excluir" | "renomear" | "sistema" | "camada" | "ativo";
    it: ItemCadastro;
  } | null>(null);
  if (carregando)
    return (
      <div className="card lc-empty" role="status">
        <div className="big">Carregando…</div>
      </div>
    );
  // Com erro, a tela NÃO afirma vazio: quem mostra o que houve é a faixa de erro da própria tela.
  if (erro && itens.length === 0) return null;
  if (itens.length === 0) return <>{vazio}</>;
  // Colunas de campos = união dos rótulos (na ordem de 1ª aparição).
  const camposCols: string[] = [];
  const colsNum = new Set<string>();
  for (const it of itens)
    for (const c of it.campos ?? []) {
      if (!camposCols.includes(c.rotulo)) camposCols.push(c.rotulo);
      if (c.num) colsNum.add(c.rotulo);
    }
  const temStatus = itens.some((it) => it.status);
  const rotuloStatus = itens.find((it) => it.statusRotulo)?.statusRotulo ?? "Status";
  const temAcoes = Boolean(
    acoes || onAbrir || onRenomear || onExcluir || onSistema || onCamada || acoesMenu,
  );
  // Coluna PRÓPRIA (não menu, não pill): "ativo" é um interruptor de segurança — precisa estar
  // visível e alcançável direto na linha, nunca escondido atrás de um clique em "⋯". Sempre
  // mostrada quando `onAtivo` existe (ao contrário de `campoCamada`/`campoSistema`, que só
  // ocupam coluna no estado excepcional): um switch escondido quando "ligado" não se descobre.
  const temAtivo = Boolean(onAtivo);
  const marcado = pedindo?.tipo === "sistema" ? (ehSistema?.(pedindo.it) ?? false) : false;
  // Sentinela: "sem camada" viaja como texto porque o guard de `ConfirmarAcao` recusa valor vazio
  // (`!texto.trim()` → não confirma). Converter para `null` é a última coisa que acontece.
  const camadaAtual = pedindo?.tipo === "camada" ? (camadaDe?.(pedindo.it) ?? null) : null;
  return (
    <>
    {pedindo?.tipo === "excluir" && (
      <ConfirmarAcao
        titulo="Excluir definitivamente"
        corpo={
          <>
            Remover <b>{pedindo.it.nome}</b>, com todas as suas versões e histórico? Esta ação não
            pode ser desfeita.
          </>
        }
        rotuloConfirmar="Excluir"
        perigo
        onConfirmar={() => {
          onExcluir?.(pedindo.it.id);
          setPedindo(null);
        }}
        onCancelar={() => setPedindo(null)}
      />
    )}
    {pedindo?.tipo === "renomear" && (
      <ConfirmarAcao
        titulo="Renomear"
        corpo={<>Como este item deve se chamar?</>}
        rotuloCampo="Nome"
        valorInicial={pedindo.it.nome}
        rotuloConfirmar="Salvar nome"
        onConfirmar={(novo) => {
          if (novo && novo !== pedindo.it.nome) onRenomear?.(pedindo.it.id, novo);
          setPedindo(null);
        }}
        onCancelar={() => setPedindo(null)}
      />
    )}
    {pedindo?.tipo === "ativo" && (
      <ConfirmarAcao
        titulo="Desativar pipeline"
        corpo={
          <>
            <b>{pedindo.it.nome}</b> deixa de EXECUTAR — inclusive por gatilho externo, agendamento
            e reexecução. O rascunho continua editável e publicável; só RODAR fica bloqueado. Dá
            para reativar depois.
          </>
        }
        rotuloConfirmar="Desativar"
        perigo
        onConfirmar={() => {
          onAtivo?.(pedindo.it.id, false);
          setPedindo(null);
        }}
        onCancelar={() => setPedindo(null)}
      />
    )}
    {pedindo?.tipo === "sistema" && (
      <ConfirmarAcao
        titulo={marcado ? "Deixar de ser item de sistema" : "Marcar como item de sistema"}
        corpo={
          marcado ? (
            <>
              <b>{pedindo.it.nome}</b> voltará a aparecer para especialistas e operadores.
            </>
          ) : (
            <>
              <b>{pedindo.it.nome}</b> deixa de aparecer para especialista e operador — só admin
              passa a ver e editar. O Portal continua consumindo as páginas publicadas.
            </>
          )
        }
        rotuloConfirmar={marcado ? "Desmarcar" : "Marcar como sistema"}
        onConfirmar={() => {
          onSistema?.(pedindo.it.id, !marcado);
          setPedindo(null);
        }}
        onCancelar={() => setPedindo(null)}
      />
    )}
    {pedindo?.tipo === "camada" && (
      <ConfirmarAcao
        titulo="Definir a camada"
        corpo={
          <>
            Em que camada do medalhão <b>{pedindo.it.nome}</b> se classifica? A camada é só
            classificação: ela define em qual aba desta lista o item aparece e <b>não</b> muda quem
            pode vê-lo ou executá-lo.
          </>
        }
        rotuloCampo="Camada"
        valorInicial={camadaAtual ?? SEM_CAMADA}
        campoOpcoes={[
          { valor: SEM_CAMADA, rotulo: "sem camada" },
          { valor: "bronze", rotulo: "bronze" },
          { valor: "silver", rotulo: "silver" },
          { valor: "gold", rotulo: "gold" },
        ]}
        rotuloConfirmar="Salvar camada"
        onConfirmar={(v) => {
          onCamada?.(pedindo.it.id, v === SEM_CAMADA ? null : (v as Camada));
          setPedindo(null);
        }}
        onCancelar={() => setPedindo(null)}
      />
    )}
    {/* Rolagem horizontal própria: `.dgrid` tem overflow:hidden por causa do raio, e em notebook a
        coluna de ações (onde vive o Excluir) era recortada em vez de alcançável. */}
    <div className="dgrid-wrap" tabIndex={0} role="region" aria-label={rotulo}>
    <table className="dgrid" aria-label={rotulo}>
      <thead>
        <tr>
          <th scope="col">Nome</th>
          {camposCols.map((c) => (
            <th key={c} scope="col" className={colsNum.has(c) ? "num" : undefined}>
              {c}
            </th>
          ))}
          {temStatus && <th scope="col">{rotuloStatus}</th>}
          {temAtivo && <th scope="col">Ativo</th>}
          {temAcoes && (
            <th className="dg-acth" scope="col">
              Ações
            </th>
          )}
        </tr>
      </thead>
      <tbody>
        {itens.map((it) => {
          const campoDe = (rot: string) => it.campos?.find((c) => c.rotulo === rot);
          const valDe = (rot: string) => campoDe(rot)?.valor ?? "";
          return (
            <tr
              key={it.id}
              data-testid={it.testid}
              className={onAbrir ? "clic" : undefined}
              onClick={onAbrir ? () => onAbrir(it.id) : undefined}
            >
              <td className="dg-nome">
                <span className="dg-nome-t">{it.nome}</span>
                {it.meta && <span className="dg-sub">{it.meta}</span>}
              </td>
              {camposCols.map((c) => (
                <td key={c} className={campoDe(c)?.num ? "num" : undefined}>
                  {valDe(c)}
                </td>
              ))}
              {temStatus && (
                <td>
                  {/* Sem classe informada o pill era VERDE: o desconhecido nascia com o desfecho
                      bom, no componente que pinta o status de todas as listas. */}
                  {it.status && (
                    <span className={"statuspill " + (it.scls || "skip")}>
                      <span className="d" />
                      {it.status}
                    </span>
                  )}
                </td>
              )}
              {temAtivo && (
                <td onClick={(e) => e.stopPropagation()}>
                  <Interruptor
                    ligado={ativoDe?.(it) ?? true}
                    rotulo={
                      (ativoDe?.(it) ?? true)
                        ? `Desativar ${it.nome}`
                        : `Ativar ${it.nome}`
                    }
                    testid={`ativo-linha-${it.id}`}
                    onAlternar={() =>
                      (ativoDe?.(it) ?? true)
                        ? setPedindo({ tipo: "ativo", it })
                        : onAtivo?.(it.id, true)
                    }
                  />
                </td>
              )}
              {temAcoes && (
                <td className="dg-acts" onClick={(e) => e.stopPropagation()}>
                  {acoes ? (
                    acoes(it)
                  ) : (
                    <>
                      {/* Só EDITAR fica fora. Uma linha com quatro botões de mesmo peso faz a
                          ação corriqueira e a irreversível disputarem o mesmo pixel — e o Excluir
                          era o mais exposto de todos. O resto vai para o menu, que é alcançável
                          por teclado e nomeia o item. */}
                      {onAbrir && (
                        <button aria-label={`${abrirLabel} ${it.nome}`} onClick={() => onAbrir(it.id)}>
                          {abrirLabel}
                        </button>
                      )}
                      {(onRenomear || onSistema || onCamada || onExcluir || acoesMenu) && (
                        <MenuAcoes rotulo={`Mais ações de ${it.nome}`}>
                          {onRenomear && (
                            <button onClick={() => setPedindo({ tipo: "renomear", it })}>
                              Renomear
                            </button>
                          )}
                          {onSistema && (
                            <button
                              data-testid={`sistema-acao-${it.id}`}
                              onClick={() => setPedindo({ tipo: "sistema", it })}
                            >
                              {ehSistema?.(it) ? "Tirar de sistema" : "Marcar como sistema"}
                            </button>
                          )}
                          {onCamada && (
                            <button
                              data-testid={`camada-acao-${it.id}`}
                              onClick={() => setPedindo({ tipo: "camada", it })}
                            >
                              Definir camada…
                            </button>
                          )}
                          {acoesMenu?.(it)}
                          {onExcluir && (
                            <button
                              className="del"
                              data-testid={`excluir-acao-${it.id}`}
                              onClick={() => setPedindo({ tipo: "excluir", it })}
                            >
                              Excluir
                            </button>
                          )}
                        </MenuAcoes>
                      )}
                    </>
                  )}
                </td>
              )}
            </tr>
          );
        })}
      </tbody>
    </table>
    </div>
    </>
  );
}

// Campo "Sistema" (admin-only) de uma linha do GridLista. Retorna null quando não-admin — aí o item
// nem aparece na lista (o backend filtra).
//
// É LEITURA, não toggle. Marcar um item como "de sistema" muda quem o vê: ele desaparece da lista
// para especialista e operador. Um checkbox de 13px dentro de uma linha clicável fazia dessa
// mudança de visibilidade o acidente mais barato da tela — bastava errar o alvo do clique que abre
// o editor (aconteceu numa captura de tela: marcou o pipeline do Check e ele sumiu para não-admin).
// A troca virou ação deliberada, com confirmação, na coluna de ações.
export function campoSistema(ehAdmin: boolean, id: string, marcado: boolean): CampoItem | null {
  // Só quem ESTÁ marcado ocupa a coluna: sem isto a tabela reservava uma coluna inteira para
  // mostrar "—" em todas as linhas em cinco telas. A ação de marcar/desmarcar vive na coluna de
  // ações e continua acessível ao admin.
  if (ehAdmin && !marcado) return null;
  if (!ehAdmin) return null;
  return {
    rotulo: "Sistema",
    valor: marcado ? (
      <span className="statuspill skip" data-testid={`sistema-linha-${id}`}>
        <span className="d" />
        sistema
      </span>
    ) : (
      <span data-testid={`sistema-linha-${id}`} style={{ color: "var(--ink-2)" }}>
        —
      </span>
    ),
  };
}

// Campo "Camada" de uma linha do GridLista. `null` (sem camada) devolve `null`: assim a coluna só
// existe quando ALGUMA linha tem camada — mesmo critério de `campoSistema`, e o que evita reservar
// uma coluna inteira de "—" na aba Geral, onde por definição ninguém tem camada.
//
// O pill é NEUTRO (`off`, cinza) de propósito. Reusar `.ok` (verde) faria "silver" ler como
// "execução bem-sucedida" — é a mesma coluna de pill que a lista usa para status.
export function campoCamada(id: string, camada: Camada | null): CampoItem | null {
  if (!camada) return null;
  return {
    rotulo: "Camada",
    valor: (
      <span className="statuspill off" data-testid={`camada-linha-${id}`}>
        <span className="d" />
        {camada}
      </span>
    ),
  };
}

// Uma aba de filtro da listagem. `contagem` aparece no rótulo — aba sem número obriga a clicar em
// cada uma para descobrir onde está o que se procura.
export interface AbaLista {
  chave: string;
  rotulo: string;
  contagem?: number;
}

interface AbasFiltroProps {
  abas: AbaLista[];
  ativa?: string;
  onAba?: (chave: string) => void;
  // Vai para o `aria-label` do tablist: "Filtrar pipelines por camada", "Filtrar conexões por tipo".
  // Parametrizado porque estava escrito "por camada" no corpo do componente — correto na listagem
  // de pipelines e mentira em qualquer outra tela que reusasse a régua.
  rotulo: string;
  idPainel: string;
  // Prefixo dos ids dos botões. Duas réguas na MESMA página colidiriam em `aba-<chave>`, e id
  // duplicado quebra o `aria-controls` de ambas.
  idPrefixo?: string;
}

// Régua de abas de FILTRO. Um componente só, usado pela listagem-padrão (`ListaCadastro`) e por
// telas que montam a própria grade (Conexões) — a alternativa era um segundo `role="tablist"`
// escrito à mão, e este repo já pagou por ter duas implementações do mesmo menu.
//
// `tablist` de verdade: `role`, `aria-selected`, `aria-controls`, `tabIndex` roving e ←/→ movendo o
// foco. Aba desenhada com `<div>` clicável é invisível para teclado e leitor de tela.
export function AbasFiltro({
  abas,
  ativa,
  onAba,
  rotulo,
  idPainel,
  idPrefixo = "aba-lista",
}: AbasFiltroProps) {
  if (!abas.length) return null;
  return (
    <div
      className="cfg-tabs"
      data-testid="lista-abas"
      role="tablist"
      aria-label={rotulo}
      onKeyDown={(ev) => {
        // Setas navegam entre abas (padrão ARIA): sem isto o teclado só alcança a aba ativa,
        // porque as inativas ficam com tabIndex -1.
        const delta = ev.key === "ArrowRight" ? 1 : ev.key === "ArrowLeft" ? -1 : 0;
        if (!delta) return;
        ev.preventDefault();
        const i = abas.findIndex((a) => a.chave === ativa);
        const prox = abas[(Math.max(i, 0) + delta + abas.length) % abas.length];
        onAba?.(prox.chave);
        document.getElementById(`${idPrefixo}-${prox.chave}`)?.focus();
      }}
    >
      {abas.map((a) => {
        const sel = a.chave === ativa;
        return (
          <button
            key={a.chave}
            type="button"
            id={`${idPrefixo}-${a.chave}`}
            data-testid={`${idPrefixo}-${a.chave}`}
            role="tab"
            aria-selected={sel}
            aria-controls={idPainel}
            tabIndex={sel ? 0 : -1}
            className={sel ? "on" : ""}
            onClick={() => onAba?.(a.chave)}
          >
            {a.rotulo}
            {a.contagem === undefined ? "" : ` (${a.contagem})`}
          </button>
        );
      })}
    </div>
  );
}

// Sub-abas (chips) de uma aba — Chips e não um segundo
// `tablist`: duas réguas de abas empilhadas leem como dois níveis de navegação, e isto é um recorte
// DENTRO da aba ativa. `aria-pressed`, como os chips de status de Execuções. `seta` desenha "→"
// entre uma sub-aba e a seguinte quando elas se sucedem no fluxo do dado.
export interface SubAba {
  chave: string;
  rotulo: string;
  contagem?: number;
  seta?: boolean;
}

export function SubAbas({
  subAbas,
  ativa,
  onSub,
  rotulo,
  idPrefixo = "sub-aba",
}: {
  subAbas: SubAba[];
  ativa: string;
  onSub: (chave: string) => void;
  rotulo: string;
  idPrefixo?: string;
}) {
  return (
    <div className="sub-abas" role="group" aria-label={rotulo} data-testid={`${idPrefixo}s`}>
      {subAbas.map((s) => (
        <Fragment key={s.chave}>
          <button
            type="button"
            className={"chip" + (s.chave === ativa ? " on" : "")}
            aria-pressed={s.chave === ativa}
            data-testid={`${idPrefixo}-${s.chave}`}
            onClick={() => onSub(s.chave)}
          >
            {s.rotulo}
            {s.contagem !== undefined && (
              <span className={"cnt" + (s.contagem === 0 ? " zero" : "")}> · {s.contagem}</span>
            )}
          </button>
          {s.seta && (
            <span className="sub-abas-seta" aria-hidden="true">
              →
            </span>
          )}
        </Fragment>
      ))}
    </div>
  );
}

interface ListaCadastroProps {
  titulo: string;
  sub?: string; // mantido por compat; não é mais exibido (sem subtítulo explicativo)
  novoLabel: string;
  itens: ItemCadastro[];
  onNovo: () => void;
  onAbrir: (id: string) => void;
  onExcluir?: (id: string) => void;
  onRenomear?: (id: string, nomeAtual: string) => void;
  onSistema?: (id: string, sistema: boolean) => void;
  ehSistema?: (it: ItemCadastro) => boolean;
  onCamada?: (id: string, camada: Camada | null) => void;
  camadaDe?: (it: ItemCadastro) => Camada | null;
  // Ativa/desativa a EXECUÇÃO do item (hoje só pipelines). Ver `GridListaProps.onAtivo`.
  onAtivo?: (id: string, ativo: boolean) => void;
  ativoDe?: (it: ItemCadastro) => boolean;
  // Ações extras no menu "⋯" da linha. Ver `GridListaProps.acoesMenu`.
  acoesMenu?: (it: ItemCadastro) => ReactNode;
  // Texto do contador da barra ("7 pipelines"), no plural certo. Sem isto vinha "7 cadastrado(s)".
  contagem?: string;
  // Abas de FILTRO da listagem. Ausentes → nada muda (as outras telas seguem idênticas). A aba
  // ativa é decidida por quem chama, porque o filtro mora na querystring: ele tem de ser linkável
  // e sobreviver ao voltar do navegador, o que estado local do componente não dá.
  abas?: AbaLista[];
  abaAtiva?: string;
  onAba?: (chave: string) => void;
  // Recorte DENTRO da aba ativa (sub-abas), desenhado logo abaixo da régua.
  subAbas?: ReactNode;
  // Estado vazio. Com aba ativa, "Nada cadastrado ainda" é MENTIRA (há itens, só não nesta aba) —
  // por isso a tela pode substituir o texto.
  vazio?: ReactNode;
  // Repassados ao grid: sem eles a tela dizia "Nada cadastrado ainda" enquanto carregava — e
  // também depois de a chamada falhar.
  carregando?: boolean;
  erro?: boolean;
}

// Tela-padrão de listagem (cabeçalho + barra + grid). Usa `GridLista`.
export function ListaCadastro({
  titulo,
  novoLabel,
  itens,
  onNovo,
  onAbrir,
  onExcluir,
  onRenomear,
  onSistema,
  ehSistema,
  onCamada,
  camadaDe,
  onAtivo,
  ativoDe,
  acoesMenu,
  contagem,
  abas,
  abaAtiva,
  onAba,
  subAbas,
  vazio,
  carregando,
  erro,
}: ListaCadastroProps) {
  // id do painel só existe quando há abas — `aria-controls` apontando para um id inexistente é pior
  // que não ter o atributo (o leitor de tela anuncia uma relação que não se pode seguir).
  const idPainel = "lista-painel";
  return (
    <>
      <div className="page-head">
        <h1 className="display">{titulo}</h1>
      </div>
      <div className="pad">
        {abas && abas.length > 0 && (
          <AbasFiltro
            abas={abas}
            ativa={abaAtiva}
            onAba={onAba}
            rotulo={`Filtrar ${titulo}`}
            idPainel={idPainel}
          />
        )}
        {subAbas}
        <div className="lc-bar">
          <span>
            {carregando
              ? "carregando…"
              : erro
                ? "não foi possível carregar"
                : (contagem ?? `${itens.length} ${itens.length === 1 ? "item" : "itens"}`)}
          </span>
          <button className="btn-stamp" style={{ marginLeft: "auto" }} onClick={onNovo}>
            + {novoLabel}
          </button>
        </div>
        <div
          id={idPainel}
          role={abas && abas.length > 0 ? "tabpanel" : undefined}
          aria-labelledby={abas && abas.length > 0 ? `aba-lista-${abaAtiva}` : undefined}
        >
          <GridLista
            itens={itens}
            onAbrir={onAbrir}
            onExcluir={onExcluir}
            onRenomear={onRenomear}
            onSistema={onSistema}
            ehSistema={ehSistema}
            onCamada={onCamada}
            camadaDe={camadaDe}
            onAtivo={onAtivo}
            ativoDe={ativoDe}
            acoesMenu={acoesMenu}
            carregando={carregando}
            erro={erro}
            rotulo={titulo}
            vazio={
              vazio ?? (
                <div className="card lc-empty">
                  <div className="big">Nada cadastrado ainda</div>
                  <div>
                    Clique <b>+ {novoLabel}</b> para começar.
                  </div>
                </div>
              )
            }
          />
        </div>
      </div>
    </>
  );
}

// --- Bloco genérico: renderiza uma view/medida como pivot/tabela/KPI ---
// Usado na Página e no Portal. (Página não calcula — só referencia o modelo.)
export interface BlocoView {
  cols: string[];
  mini: ReactNode[][];
  kpi: { label: string; val: string };
}

export type BlocoTipo = "pivot" | "tabela" | "valor";

interface BlocoProps {
  view: BlocoView;
  tipo: BlocoTipo;
  // Opcionais (usados no Portal, ignorados na pré-visualização da Página):
  onSort?: (col: string) => void; // cabeçalho clicável p/ ordenar
  sort?: { col: string; dir: "asc" | "desc" };
  total?: ReactNode[]; // linha de Total no rodapé (1 célula por coluna)
  // Índices das colunas NUMÉRICAS: alinhamento vem do tipo do dado, não da posição — sem isso
  // "NOME" e "CPF" saíam encostados à direita junto dos valores.
  colsNum?: Set<number>;
  // Nome da grade para leitor de tela (a visualização tem várias na mesma página).
  rotulo?: string;
}

// Acima disto a grade é tratada como larga mesmo que o bloco seja um pivot.
export const LIMITE_COLUNAS_GRADE_LARGA = 8;

export function Bloco({ view, tipo, onSort, sort, total, colsNum, rotulo }: BlocoProps) {
  if (tipo === "valor") {
    return (
      <div className="kpi">
        <div className="kl">{view.kpi.label}</div>
        <div className="kn">{view.kpi.val}</div>
      </div>
    );
  }
  // Grade LARGA (a tabela do relatório com dezenas de colunas): sem `larga` a tabela dividia os 100% do
  // card entre todas as colunas e cada uma quebrava o texto letra a letra — o dado ficava ilegível e a
  // rolagem horizontal do `.pbbody` nunca aparecia, porque a tabela sempre "cabia". Com `larga` ela
  // tem a largura do conteúdo e quem rola é o card.
  const larga = tipo === "tabela" || view.cols.length > LIMITE_COLUNAS_GRADE_LARGA;
  return (
    <table className={"bmini" + (larga ? " bmini-larga" : "")} aria-label={rotulo}>
      <thead>
        <tr>
          {view.cols.map((c, j) => (
            <th
              key={c}
              scope="col"
              className={colsNum?.has(j) ? "num" : undefined}
              aria-sort={
                sort?.col === c ? (sort.dir === "asc" ? "ascending" : "descending") : undefined
              }
            >
              {/* Ordenar precisa ser um BOTÃO: como `th onClick` a ordenação era inalcançável por
                  teclado e o leitor de tela não anunciava nem a ação nem a direção. */}
              {onSort ? (
                <button
                  type="button"
                  className="bmini-sort"
                  aria-label={`Ordenar por ${c}`}
                  onClick={() => onSort(c)}
                >
                  {c}
                  {sort?.col === c ? (sort.dir === "asc" ? " ▲" : " ▼") : ""}
                </button>
              ) : (
                c
              )}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {view.mini.map((r, i) => (
          <tr key={i}>
            {r.map((c, j) => (
              <td key={j} className={colsNum?.has(j) ? "num" : undefined}>
                {c}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
      {total && (
        <tfoot>
          <tr className="tot-row">
            {total.map((c, j) => (
              <td key={j} className={colsNum?.has(j) ? "num" : undefined}>
                {c}
              </td>
            ))}
          </tr>
        </tfoot>
      )}
    </table>
  );
}

export { Fragment };
