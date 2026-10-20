import { python } from "@codemirror/lang-python";
import { sql } from "@codemirror/lang-sql";
import CodeMirror from "@uiw/react-codemirror";
import { memo, useEffect, useMemo, useState, type MouseEvent, type PointerEvent as ReactPointerEvent } from "react";
import { createPortal } from "react-dom";
import { Handle, Position, type NodeProps } from "reactflow";

import * as api from "../../api/client";
import { celula, fmtHora, perfisDaAmostra, type PerfilColuna } from "../execucao/formato";
import { Icon } from "../ui";
import { useEditorNo } from "./contexto";
import { bibsDetectadas, refsDoNo } from "./deteccao";
import { sqlSintaxeOk, validacaoSql } from "./lintsql";
import { categoriaDe, META, type CategoriaNo } from "./portas";
import { realceParametros } from "./realce";

// Conexão com a flag de escrita (008). A coluna `permitir_escrita` é admin-only (liga em
// Conexões, migration 0021); tipada localmente até client.ts reconciliar o contrato.
type ConexaoComEscrita = api.Conexao & { permitir_escrita?: boolean };

// O dry-run do nó de efeito (008/009) vem no `efeito` do preview, tipado em `api.EfeitoPreview`
// (contrato em specs/008-nos-escrita-http/contracts/ e specs/009-no-email/contracts/). Havia uma
// segunda declaração do mesmo objeto aqui: duas verdades para o mesmo payload, e a daqui não
// acompanhava o backend.

// Tamanho legível de anexo. Sem isto o painel dizia "8421" e ninguém sabe se cabe no e-mail.
function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toLocaleString("pt-BR", { maximumFractionDigits: 1 })} KB`;
  return `${(n / 1024 / 1024).toLocaleString("pt-BR", { maximumFractionDigits: 1 })} MB`;
}

// ============================================================================
// Redimensionar janela flutuante (.code-janela) por QUALQUER borda/canto — o CSS `resize` nativo
// só dá uma alça no canto inferior-direito. Cada uma das 8 alças ajusta a geometria via drag;
// arrastar pela borda N/W desloca `left`/`top` mantendo o lado oposto fixo.
// ============================================================================
type Geometria = { left: number; top: number; largura: number; altura: number };
type Borda = "n" | "s" | "e" | "w" | "ne" | "nw" | "se" | "sw";
const GEOMETRIA_MIN = { largura: 340, altura: 220 };

function clampGeometria(g: Geometria): Geometria {
  const maxLargura = window.innerWidth * 0.96;
  const maxAltura = window.innerHeight * 0.84;
  return {
    left: g.left,
    top: g.top,
    largura: Math.min(Math.max(g.largura, GEOMETRIA_MIN.largura), maxLargura),
    altura: Math.min(Math.max(g.altura, GEOMETRIA_MIN.altura), maxAltura),
  };
}

function useJanelaGeometria(inicial: Geometria) {
  const [geo, setGeo] = useState<Geometria>(() => clampGeometria(inicial));
  // Reposiciona sem mexer no tamanho — usado ao (re)abrir a janela num ponto novo.
  const reposicionar = (left: number, top: number) => setGeo((g) => ({ ...g, left, top }));

  const iniciarResize = (borda: Borda) => (e: ReactPointerEvent) => {
    e.preventDefault();
    e.stopPropagation();
    const ini = geo;
    const x0 = e.clientX;
    const y0 = e.clientY;
    const mover = (ev: PointerEvent) => {
      const dx = ev.clientX - x0;
      const dy = ev.clientY - y0;
      let { left, top, largura, altura } = ini;
      if (borda.includes("e")) largura = ini.largura + dx;
      if (borda.includes("s")) altura = ini.altura + dy;
      if (borda.includes("w")) {
        largura = ini.largura - dx;
        left = ini.left + dx;
      }
      if (borda.includes("n")) {
        altura = ini.altura - dy;
        top = ini.top + dy;
      }
      setGeo(clampGeometria({ left, top, largura, altura }));
    };
    const soltar = () => {
      window.removeEventListener("pointermove", mover);
      window.removeEventListener("pointerup", soltar);
    };
    window.addEventListener("pointermove", mover);
    window.addEventListener("pointerup", soltar);
  };

  return { geo, iniciarResize, reposicionar };
}

const BORDAS: Borda[] = ["n", "s", "e", "w", "ne", "nw", "se", "sw"];
function AlcasRedimensionar({ iniciar }: { iniciar: (b: Borda) => (e: ReactPointerEvent) => void }) {
  return (
    <>
      {BORDAS.map((b) => (
        <div key={b} className={`jf-alca jf-alca-${b}`} onPointerDown={iniciar(b)} />
      ))}
    </>
  );
}

// Área de código: o botão rotulado abre uma JANELA FLUTUANTE (fora do card, via portal p/ escapar
// do zoom do canvas) — redimensionável em qualquer borda, com fechar e salvar. A edição do texto é
// ao vivo (autosave no rascunho EM MEMÓRIA); "Salvar" persiste o grafo (mesma ação do toolbar) e
// autodeclara qualquer `:param` novo referenciado no SQL antes de gravar.
function CodeArea({
  label,
  titulo,
  value,
  onChange,
  extensions,
  tema,
  validarSintaxe = sqlSintaxeOk,
}: {
  label: string;
  titulo: string;
  value: string;
  onChange: (v: string) => void;
  extensions: unknown[];
  tema: "light" | "dark";
  // valida a sintaxe p/ habilitar o "Salvar" e mostrar aviso. Default = SQL; Python passa () => true.
  validarSintaxe?: (v: string) => boolean;
}) {
  const ctx = useEditorNo();
  const [aberto, setAberto] = useState(false);
  const { geo, iniciarResize, reposicionar } = useJanelaGeometria({ left: 0, top: 0, largura: 560, altura: 380 });
  const [editou, setEditou] = useState(false);
  const [valido, setValido] = useState(true);
  const [salvando, setSalvando] = useState(false);
  const [erroSalvar, setErroSalvar] = useState<string | null>(null);

  const abrir = (e: MouseEvent) => {
    // A janela abre AO LADO do card, nunca por cima dele: posicionar pelo retângulo do BOTÃO e
    // depois grudar na borda da tela fazia a janela cobrir justamente o nó que se está editando —
    // não dava para ver o SQL e a configuração do nó ao mesmo tempo.
    const botao = e.currentTarget as HTMLElement;
    const card = botao.closest(".nopipe")?.getBoundingClientRect() ?? botao.getBoundingClientRect();
    const W = 560;
    const MARGEM = 16;
    const cabeDireita = card.right + MARGEM + W + 12 <= window.innerWidth;
    const cabeEsquerda = card.left - MARGEM - W >= 12;
    const left = cabeDireita
      ? card.right + MARGEM
      : cabeEsquerda
        ? card.left - MARGEM - W
        : // Não cabe dos dois lados: encosta onde sobra mais espaço, ainda sem cobrir o card
          // inteiro (a pessoa arrasta a janela se quiser outra posição).
          Math.max(12, Math.min(window.innerWidth - W - 12, card.left - W / 2));
    const top = Math.min(Math.max(12, card.top - 8), window.innerHeight - 280);
    reposicionar(left, top);
    setEditou(false);
    setValido(validarSintaxe(value));
    setErroSalvar(null);
    setAberto(true);
  };

  const salvar = async () => {
    setSalvando(true);
    setErroSalvar(null);
    try {
      await ctx.salvarGrafo();
      setAberto(false);
    } catch (e: any) {
      setErroSalvar(e?.message ?? "Falha ao salvar");
    } finally {
      setSalvando(false);
    }
  };

  return (
    <>
      <button type="button" className="no-code-abrir" data-testid="abrir-codigo" onClick={abrir}>
        <Icon name="studio" />
        {label}
      </button>
      {aberto &&
        createPortal(
          <div
            onKeyDown={(ev) => {
              // Atalhos da janela de código: Escape fecha, Ctrl/Cmd+Enter salva. Sem eles, editar
              // SQL exigia voltar ao mouse a cada janela.
              if (ev.key === "Escape") {
                ev.stopPropagation();
                setAberto(false);
              } else if ((ev.metaKey || ev.ctrlKey) && ev.key === "Enter" && valido) {
                ev.preventDefault();
                void salvar();
              }
            }}
            className="code-janela nodrag nowheel"
            style={{ left: geo.left, top: geo.top, width: geo.largura, height: geo.altura }}
            onClick={(e) => e.stopPropagation()}
            onPointerDown={(e) => e.stopPropagation()}
          >
            <AlcasRedimensionar iniciar={iniciarResize} />
            <div className="code-janela-cab">
              <span className="code-janela-tit">{titulo}</span>
              <button type="button" className="code-janela-x" data-testid="fechar-codigo" title="Fechar" onClick={() => setAberto(false)}>
                <svg viewBox="0 0 20 20" width="1em" height="1em" aria-hidden="true">
                  <path d="M6 6l8 8M14 6l-8 8" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
                </svg>
              </button>
            </div>
            <div className="code-janela-corpo">
              <CodeMirror
                value={value}
                theme={tema}
                height="100%"
                extensions={extensions as never}
                onChange={(v) => {
                  setEditou(true);
                  setValido(validarSintaxe(v));
                  onChange(v);
                }}
              />
            </div>
            <div className="code-janela-rodape">
              {erroSalvar ? (
                <span className="code-janela-aviso">{erroSalvar}</span>
              ) : (
                // "sintaxe inválida" prometia validação de SQL: a checagem client-side só olha
                // parênteses e aspas (o parser de verdade é o do backend, no preview/execução).
                editou && !valido && (
                  <span className="code-janela-aviso">
                    parêntese ou aspa não fechados — a validação completa do SQL acontece no preview
                  </span>
                )
              )}
              <button
                type="button"
                className="code-janela-salvar"
                data-testid="salvar-codigo"
                disabled={!editou || !valido || salvando}
                title={!editou ? "Edite o código" : !valido ? "Corrija a sintaxe" : "Salvar"}
                onClick={() => void salvar()}
              >
                {salvando ? "Salvando…" : "Salvar"}
              </button>
            </div>
          </div>,
          document.body,
        )}
    </>
  );
}

// Editor de pares de→para (usado no mapeamento de colunas do escrita_sql e no corpo por-colunas
// do http). Cada linha = dois inputs + remover; um botão adiciona. O form-pai decide se a lista
// vazia vira `undefined` (ausente = colunas homônimas / todas as colunas).
function EditorPares({
  valor,
  onChange,
  testid,
  phDe = "coluna de origem",
  phPara = "destino",
  rotuloAdd = "+ Adicionar coluna",
  listaPara,
}: {
  valor: { de: string; para: string }[];
  onChange: (v: { de: string; para: string }[]) => void;
  testid: string;
  phDe?: string;
  phPara?: string;
  rotuloAdd?: string;
  // id de um <datalist> com sugestões para o campo "para" (ex.: os `:parametro` declarados).
  listaPara?: string;
}) {
  const pares = valor ?? [];
  const setPar = (i: number, campo: "de" | "para", v: string) =>
    onChange(pares.map((p, j) => (j === i ? { ...p, [campo]: v } : p)));
  const remover = (i: number) => onChange(pares.filter((_, j) => j !== i));
  const adicionar = () => onChange([...pares, { de: "", para: "" }]);
  return (
    <div data-testid={testid} style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {pares.map((p, i) => (
        <div key={i} style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <input
            data-testid={`${testid}-de-${i}`}
            value={p.de}
            placeholder={phDe}
            style={{ flex: 1, minWidth: 0 }}
            onChange={(e) => setPar(i, "de", e.target.value)}
          />
          <span aria-hidden="true">→</span>
          <input
            data-testid={`${testid}-para-${i}`}
            value={p.para}
            placeholder={phPara}
            list={listaPara}
            style={{ flex: 1, minWidth: 0 }}
            onChange={(e) => setPar(i, "para", e.target.value)}
          />
          <button
            type="button"
            className="nopipe-btn nopipe-x"
            data-testid={`${testid}-remover-${i}`}
            title="Remover par"
            aria-label="Remover par"
            onClick={() => remover(i)}
          >
            <svg viewBox="0 0 20 20" width="1em" height="1em" aria-hidden="true">
              <path d="M6 6l8 8M14 6l-8 8" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
            </svg>
          </button>
        </div>
      ))}
      <button type="button" className="no-code-abrir" data-testid={`${testid}-add`} onClick={adicionar} style={{ alignSelf: "flex-start" }}>
        {rotuloAdd}
      </button>
    </div>
  );
}

// Editor de MAPA chave→valor: a partição que a fonte `publicado` pede e o `limpar_escopo` da escrita.
// O persistido é um objeto, sem linha em branco (chave vazia não é chave) — mas quem digita precisa
// da linha em branco. As linhas vivem em estado LOCAL e só as completas vão para a config; se a
// config mudar por fora (outra versão aberta no canvas), as linhas são refeitas a partir dela.
function EditorMapa({
  valor,
  onChange,
  testid,
  phChave,
  phValor,
  rotuloAdd,
  listaValor,
}: {
  valor: Record<string, unknown> | undefined;
  onChange: (v: Record<string, string> | undefined) => void;
  testid: string;
  phChave: string;
  phValor: string;
  rotuloAdd: string;
  listaValor?: string;
}) {
  const paraLinhas = (m: Record<string, unknown> | undefined) =>
    Object.entries(m ?? {}).map(([de, para]) => ({ de, para: String(para ?? "") }));
  const doMapa = (ls: { de: string; para: string }[]) =>
    Object.fromEntries(ls.filter((l) => l.de.trim()).map((l) => [l.de.trim(), l.para.trim()]));
  const [linhas, setLinhas] = useState(() => paraLinhas(valor));
  const externo = JSON.stringify(valor ?? {});
  useEffect(() => {
    if (JSON.stringify(doMapa(linhas)) !== externo) setLinhas(paraLinhas(valor));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [externo]);
  const mudar = (ls: { de: string; para: string }[]) => {
    setLinhas(ls);
    const m = doMapa(ls);
    onChange(Object.keys(m).length ? m : undefined);
  };
  return (
    <EditorPares
      testid={testid}
      valor={linhas}
      onChange={mudar}
      phDe={phChave}
      phPara={phValor}
      rotuloAdd={rotuloAdd}
      listaPara={listaValor}
    />
  );
}

// Um item da partição da SAÍDA. `chave` ausente = `nome` em minúsculas no caminho.
type ItemParticao = { origem: string; nome: string; chave?: string; [k: string]: unknown };

// Partição da SAÍDA no lake: lista ORDENADA — a ordem é o caminho `<chave1>=<v1>/<chave2>=<v2>`, e
// trocar a ordem muda onde o dado mora. Mesmo arranjo do `EditorMapa`: linhas locais, e só as
// completas (com `nome`) vão para a config. Campo que o editor não conhece fica no item.
function EditorParticaoSaida({
  valor,
  onChange,
  listaParametros,
}: {
  valor: ItemParticao[] | undefined;
  onChange: (v: ItemParticao[] | undefined) => void;
  listaParametros: string;
}) {
  const completas = (ls: ItemParticao[]) =>
    ls
      .filter((l) => String(l.nome ?? "").trim())
      .map(({ chave, ...resto }) => ({
        ...resto,
        origem: resto.origem === "coluna" ? "coluna" : "parametro",
        nome: String(resto.nome).trim(),
        ...(String(chave ?? "").trim() ? { chave: String(chave).trim() } : {}),
      }));
  const [linhas, setLinhas] = useState<ItemParticao[]>(() => (valor ?? []).map((p) => ({ ...p })));
  const externo = JSON.stringify(valor ?? []);
  useEffect(() => {
    if (JSON.stringify(completas(linhas)) !== externo) setLinhas((valor ?? []).map((p) => ({ ...p })));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [externo]);
  const mudar = (ls: ItemParticao[]) => {
    setLinhas(ls);
    const c = completas(ls);
    onChange(c.length ? c : undefined);
  };
  const patch = (i: number, campos: Partial<ItemParticao>) =>
    mudar(linhas.map((l, j) => (j === i ? { ...l, ...campos } : l)));
  const mover = (i: number, d: -1 | 1) => {
    const j = i + d;
    if (j < 0 || j >= linhas.length) return;
    const ls = [...linhas];
    [ls[i], ls[j]] = [ls[j], ls[i]];
    mudar(ls);
  };
  return (
    <div data-testid="saida-particao" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {linhas.map((l, i) => (
        <div key={i} style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <select
            data-testid={`saida-particao-origem-${i}`}
            value={l.origem === "coluna" ? "coluna" : "parametro"}
            onChange={(e) => patch(i, { origem: e.target.value })}
          >
            <option value="parametro">Parâmetro</option>
            <option value="coluna">Coluna</option>
          </select>
          <input
            data-testid={`saida-particao-nome-${i}`}
            value={String(l.nome ?? "")}
            placeholder={l.origem === "coluna" ? "coluna do dado" : "parâmetro"}
            list={l.origem === "coluna" ? undefined : listaParametros}
            style={{ flex: 1, minWidth: 0 }}
            onChange={(e) => patch(i, { nome: e.target.value })}
          />
          <input
            data-testid={`saida-particao-chave-${i}`}
            value={String(l.chave ?? "")}
            placeholder="chave (opcional)"
            title="Nome do segmento no caminho. Em branco = o nome em minúsculas."
            style={{ width: 110 }}
            onChange={(e) => patch(i, { chave: e.target.value })}
          />
          <button type="button" className="nopipe-btn" title="Subir" aria-label="Subir partição" onClick={() => mover(i, -1)}>
            ↑
          </button>
          <button type="button" className="nopipe-btn" title="Descer" aria-label="Descer partição" onClick={() => mover(i, 1)}>
            ↓
          </button>
          <button
            type="button"
            className="nopipe-btn nopipe-x"
            data-testid={`saida-particao-remover-${i}`}
            title="Remover partição"
            aria-label="Remover partição"
            onClick={() => mudar(linhas.filter((_, j) => j !== i))}
          >
            <svg viewBox="0 0 20 20" width="1em" height="1em" aria-hidden="true">
              <path d="M6 6l8 8M14 6l-8 8" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
            </svg>
          </button>
        </div>
      ))}
      <button
        type="button"
        className="no-code-abrir"
        data-testid="saida-particao-add"
        style={{ alignSelf: "flex-start" }}
        onClick={() => setLinhas([...linhas, { origem: "parametro", nome: "" }])}
      >
        + Adicionar partição
      </button>
    </div>
  );
}

// Sugestões de `:parametro` para campos que pedem um (partição lida, escopo da escrita). Credencial
// não entra: não delimita escopo nem vira caminho — o backend recusa as duas coisas.
function DatalistParametros({ id }: { id: string }) {
  const ctx = useEditorNo();
  return (
    <datalist id={id}>
      {ctx.parametros
        .filter((p) => !p.segredo && p.nome)
        .map((p) => (
          <option key={p.nome} value={`:${p.nome}`} />
        ))}
    </datalist>
  );
}

// Editor de anexos do nó `email` (009). Duas origens, com campos DIFERENTES: `dados` monta o
// arquivo aqui (formato/delimitador/codificação) e `coluna` só aponta as colunas onde o nó anterior
// já entregou nome e conteúdo. Mostrar os campos das duas ao mesmo tempo fazia o formulário pedir
// coisa que não se aplica — cada origem revela só o que usa.
type AnexoEmail = {
  origem?: "dados" | "coluna";
  formato?: string;
  nome?: string;
  de?: string;
  delimitador?: string;
  incluir_cabecalho?: boolean;
  codificacao?: string;
  permitir_vazio?: boolean;
  coluna_nome?: string;
  coluna_conteudo?: string;
  codificacao_conteudo?: string;
};

function EditorAnexos({
  valor,
  onChange,
  testid,
}: {
  valor: AnexoEmail[];
  onChange: (v: AnexoEmail[]) => void;
  testid: string;
}) {
  const anexos = valor ?? [];
  const patch = (i: number, campos: Partial<AnexoEmail>) =>
    onChange(anexos.map((a, j) => (j === i ? { ...a, ...campos } : a)));
  const remover = (i: number) => onChange(anexos.filter((_, j) => j !== i));
  return (
    <div data-testid={testid} style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {anexos.map((a, i) => {
        const origem = a.origem === "coluna" ? "coluna" : "dados";
        return (
          <div key={i} className="no-anexo">
            <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <select
                data-testid={`${testid}-origem-${i}`}
                value={origem}
                style={{ flex: 1, minWidth: 0 }}
                onChange={(e) => patch(i, { origem: e.target.value as "dados" | "coluna" })}
              >
                <option value="dados">Montar arquivo com o resultado do nó</option>
                <option value="coluna">Arquivo já pronto (vem numa coluna)</option>
              </select>
              <button
                type="button"
                className="nopipe-btn nopipe-x"
                data-testid={`${testid}-remover-${i}`}
                title="Remover anexo"
                aria-label="Remover anexo"
                onClick={() => remover(i)}
              >
                <svg viewBox="0 0 20 20" width="1em" height="1em" aria-hidden="true">
                  <path d="M6 6l8 8M14 6l-8 8" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
                </svg>
              </button>
            </div>
            {origem === "dados" ? (
              <>
                <div className="no-form-dupla">
                  <label>
                    Formato:
                    <select
                      data-testid={`${testid}-formato-${i}`}
                      value={a.formato ?? "csv"}
                      onChange={(e) => patch(i, { formato: e.target.value })}
                    >
                      <option value="csv">CSV</option>
                      <option value="xlsx">Excel (xlsx)</option>
                      <option value="json">JSON</option>
                      <option value="parquet">Parquet</option>
                    </select>
                  </label>
                  <label>
                    Nome do arquivo:
                    <input
                      data-testid={`${testid}-nome-${i}`}
                      value={a.nome ?? ""}
                      placeholder="relatorio_{{param.competencia}}.csv"
                      onChange={(e) => patch(i, { nome: e.target.value || undefined })}
                    />
                  </label>
                </div>
                {(a.formato ?? "csv") === "csv" && (
                  <div className="no-form-dupla">
                    <label>
                      Delimitador:
                      <input
                        data-testid={`${testid}-delimitador-${i}`}
                        value={a.delimitador ?? ";"}
                        maxLength={1}
                        onChange={(e) => patch(i, { delimitador: e.target.value.slice(0, 1) || undefined })}
                      />
                    </label>
                    <label>
                      Codificação:
                      <select
                        data-testid={`${testid}-codificacao-${i}`}
                        value={a.codificacao ?? "utf-8-sig"}
                        onChange={(e) => patch(i, { codificacao: e.target.value })}
                      >
                        <option value="utf-8-sig">UTF-8 com BOM (Excel pt-BR)</option>
                        <option value="utf-8">UTF-8</option>
                        <option value="latin-1">Latin-1</option>
                      </select>
                    </label>
                  </div>
                )}
                {(a.formato ?? "csv") === "csv" && (
                  <label className="no-check">
                    <input
                      type="checkbox"
                      data-testid={`${testid}-cabecalho-${i}`}
                      checked={a.incluir_cabecalho !== false}
                      onChange={(e) => patch(i, { incluir_cabecalho: e.target.checked })}
                    />
                    Incluir cabeçalho (nomes das colunas)
                  </label>
                )}
                <label>
                  Nó de origem (opcional):
                  <input
                    data-testid={`${testid}-de-${i}`}
                    value={a.de ?? ""}
                    placeholder="em branco = o nó conectado"
                    onChange={(e) => patch(i, { de: e.target.value.trim() || undefined })}
                  />
                </label>
              </>
            ) : (
              <>
                <div className="no-form-dupla">
                  <label>
                    Coluna com o conteúdo:
                    <input
                      data-testid={`${testid}-coluna-conteudo-${i}`}
                      value={a.coluna_conteudo ?? ""}
                      placeholder="ex.: arquivo_conteudo"
                      onChange={(e) => patch(i, { coluna_conteudo: e.target.value.trim() || undefined })}
                    />
                  </label>
                  <label>
                    Coluna com o nome:
                    <input
                      data-testid={`${testid}-coluna-nome-${i}`}
                      value={a.coluna_nome ?? ""}
                      placeholder="ex.: arquivo_nome"
                      onChange={(e) => patch(i, { coluna_nome: e.target.value.trim() || undefined })}
                    />
                  </label>
                </div>
                <label>
                  Conteúdo está em:
                  <select
                    data-testid={`${testid}-codificacao-conteudo-${i}`}
                    value={a.codificacao_conteudo ?? "base64"}
                    onChange={(e) => patch(i, { codificacao_conteudo: e.target.value })}
                  >
                    <option value="base64">Base64</option>
                    <option value="hex">Hexadecimal</option>
                    <option value="texto">Texto puro</option>
                  </select>
                </label>
              </>
            )}
            <label className="no-check">
              <input
                type="checkbox"
                data-testid={`${testid}-permitir-vazio-${i}`}
                checked={!!a.permitir_vazio}
                onChange={(e) => patch(i, { permitir_vazio: e.target.checked })}
              />
              Permitir anexo vazio (0 linhas não aborta)
            </label>
          </div>
        );
      })}
      <button
        type="button"
        className="no-code-abrir"
        data-testid={`${testid}-add`}
        style={{ alignSelf: "flex-start" }}
        onClick={() => onChange([...anexos, { origem: "dados", formato: "csv" }])}
      >
        + Adicionar anexo
      </button>
    </div>
  );
}

// Select de conexão para os nós de EFEITO (008): filtra pelos tipos aceitos e DESABILITA as
// conexões sem escrita habilitada (a flag `permitir_escrita` é admin-only). O front só monta a
// config; a compatibilidade real da conexão é conferida no publicar.
/** Corrente serial por grupo (`config.grupo_serial`). Nós de efeito e saída fora do lake
 *  rodam um por vez; declarar o mesmo grupo em dois nós mantém a fila ENTRE eles e deixa grupos
 *  diferentes rodarem ao mesmo tempo. Um nó de efeito SEM grupo espera todos os grupos — se cair
 *  no meio, vira barreira. O campo é o mesmo em todos os nós seriais; escrito uma vez. */
function CampoGrupoSerial({ id, cfg }: { id: string; cfg: any }) {
  const ctx = useEditorNo();
  return (
    <label>
      Corrente serial (opcional):
      <input
        data-testid="grupo-serial"
        value={cfg.grupo_serial ?? ""}
        placeholder="ex.: banco_b1"
        onChange={(e) => ctx.patchConfig(id, { grupo_serial: e.target.value.trim() || undefined })}
      />
      <small>
        Nós de efeito rodam um por vez. Com o mesmo nome aqui, a fila vale só entre os nós do grupo;
        grupos diferentes rodam ao mesmo tempo. Nó de efeito sem grupo espera todos os grupos
        terminarem. Dois grupos na mesma API ou tabela somam a taxa e podem se travar.
      </small>
    </label>
  );
}

function SelectConexaoEscrita({
  valor,
  tipos,
  onChange,
  testid,
}: {
  valor: string;
  tipos: string[];
  onChange: (v: string) => void;
  testid: string;
}) {
  const ctx = useEditorNo();
  const conexoes = (ctx.conexoes as ConexaoComEscrita[]).filter((c) => tipos.includes(c.tipo));
  return (
    <label>
      Conexão:
      <select data-testid={testid} value={valor} onChange={(e) => onChange(e.target.value)}>
        <option value="">— escolha —</option>
        {conexoes.map((c) => {
          const semEscrita = c.permitir_escrita === false;
          return (
            <option
              key={c.id}
              value={c.id}
              disabled={semEscrita}
              title={semEscrita ? "Conexão sem escrita habilitada (admin liga em Conexões)" : undefined}
            >
              {c.nome} ({c.tipo}){semEscrita ? " — sem escrita" : ""}
            </option>
          );
        })}
        {/* Referência salva a uma conexão que sumiu do filtro (tipo trocado/removida) — preserva. */}
        {valor && !conexoes.some((c) => c.id === valor) && <option value={valor}>{valor} (não encontrada)</option>}
      </select>
    </label>
  );
}

// Editor de fonte/guiada/SQL renderizado DENTRO do card (substitui o inspetor lateral).
function Editor({ id, data, cat }: { id: string; data: any; cat: CategoriaNo }) {
  const ctx = useEditorNo();
  const extFonte = [sql(), realceParametros, validacaoSql];
  // Nós do pipeline ESCOLHIDO na fonte tipo-pipeline (drill-down do "Nó de saída" — qualquer nó do
  // grafo gera snapshot consultável por id, não só os do tipo "Saída").
  const pipelineIdSelecionado = cat === "fonte" ? (data.config?.pipeline_id as string | undefined) : undefined;
  const [nosPipelineFonte, setNosPipelineFonte] = useState<{ id: string; tipo: string; rotulo?: string }[]>([]);
  useEffect(() => {
    if (!pipelineIdSelecionado) {
      setNosPipelineFonte([]);
      return;
    }
    let vivo = true;
    api
      .obterPipeline(pipelineIdSelecionado)
      .then((det) => {
        if (vivo) setNosPipelineFonte(det.definicao?.nos ?? []);
      })
      .catch(() => {
        if (vivo) setNosPipelineFonte([]);
      });
    return () => {
      vivo = false;
    };
  }, [pipelineIdSelecionado]);
  if (cat === "fonte") {
    const cfg = data.config ?? {};
    // Quatro modos: Conexão (fonte externa), Pipeline (saída de outro pipeline), Lake
    // (dataset do bronze) e Publicado (o que um nó Saída publicou na silver/gold, lido pelo ponteiro
    // do escopo). O SELECT existe nos quatro; fora da conexão a origem é materializada na
    // tabela `fonte_{id}` e o SQL do nó roda em cima dela. Um nó `publicado` abria como "Conexão"
    // antes deste modo existir: a tela mostrava uma origem que não era a dele.
    type ModoFonte = "conexao" | "pipeline" | "lake" | "publicado";
    const modo: ModoFonte =
      cfg.origem === "pipeline"
        ? "pipeline"
        : cfg.origem === "lake"
          ? "lake"
          : cfg.origem === "publicado"
            ? "publicado"
            : "conexao";
    const tipoConex = ctx.conexoes.find((c) => c.id === cfg.conexao_id)?.tipo;
    const ehBanco =
      tipoConex === "postgres" || tipoConex === "sqlserver" || tipoConex === "api_query" || !tipoConex;
    const alias = `fonte_${id}`;
    // Ao trocar de modo, limpa os campos dos OUTROS: config meio de um modo e meio de outro é
    // recusada na validação, e o erro sairia só no publicar.
    const semPublicado = { dataset: undefined, camada: undefined, particao: undefined };
    const trocarModo = (m: ModoFonte) => {
      if (m === "pipeline")
        ctx.patchConfig(id, {
          origem: "pipeline", conexao_id: undefined, fonte_id: undefined,
          chave_min: undefined, chave_max: undefined, sql: "", ...semPublicado,
        });
      else if (m === "lake")
        ctx.patchConfig(id, {
          origem: "lake", conexao_id: undefined, pipeline_id: undefined, no_id: undefined, sql: "",
          ...semPublicado,
        });
      else if (m === "publicado")
        ctx.patchConfig(id, {
          origem: "publicado", conexao_id: undefined, pipeline_id: undefined, no_id: undefined,
          fonte_id: undefined, chave_min: undefined, chave_max: undefined, sql: "", camada: "gold",
        });
      else
        ctx.patchConfig(id, {
          origem: undefined, pipeline_id: undefined, no_id: undefined, fonte_id: undefined,
          chave_min: undefined, chave_max: undefined, sql: "SELECT * FROM ...", ...semPublicado,
        });
    };
    const amostra = (
      <label>
        Amostra:
        <input
          data-testid="fonte-amostra"
          type="number"
          value={cfg.amostra ?? 10}
          onChange={(e) => ctx.patchConfig(id, { amostra: Number(e.target.value) })}
        />
      </label>
    );
    return (
      <div data-testid="form-fonte" className="no-form">
        <label>
          Origem:
          <select
            data-testid="fonte-modo"
            value={modo}
            onChange={(e) => trocarModo(e.target.value as ModoFonte)}
          >
            <option value="conexao">Conexão</option>
            <option value="pipeline">Outra pipeline</option>
            <option value="lake">Lake (bronze)</option>
            <option value="publicado">Dataset publicado (silver/gold)</option>
          </select>
        </label>
        {modo === "conexao" ? (
          <>
            <label>
              Conexão:
              <select
                data-testid="fonte-conexao"
                value={cfg.conexao_id ?? ""}
                onChange={(e) => ctx.patchConfig(id, { conexao_id: e.target.value })}
              >
                <option value="">— escolha —</option>
                {ctx.conexoes.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.nome} ({c.tipo})
                  </option>
                ))}
              </select>
            </label>
            {amostra}
            {ehBanco ? (
              <CodeArea
                label="Consulta na fonte"
                titulo={`Consulta na fonte · ${id}`}
                value={cfg.sql ?? "SELECT * FROM ..."}
                tema={ctx.tema}
                extensions={extFonte}
                onChange={(v) => ctx.patchConfig(id, { sql: v })}
              />
            ) : (
              <small>
                Fonte <b>{tipoConex}</b>: a leitura usa a configuração da conexão (sem consulta).
              </small>
            )}
          </>
        ) : modo === "publicado" ? (
          <>
            <label>
              Camada:
              <select
                data-testid="publicado-camada"
                value={cfg.camada === "silver" ? "silver" : "gold"}
                onChange={(e) => ctx.patchConfig(id, { camada: e.target.value })}
              >
                <option value="gold">gold — produto conformado</option>
                <option value="silver">silver — tabela da origem, tipada</option>
              </select>
            </label>
            <label>
              Dataset:
              <input
                data-testid="publicado-dataset"
                value={(cfg.dataset as string) ?? ""}
                placeholder="ex.: vendas"
                onChange={(e) => ctx.patchConfig(id, { dataset: e.target.value.trim() })}
              />
            </label>
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <span className="no-form-lbl">Escopo lido (partição)</span>
              <EditorMapa
                testid="publicado-particao"
                valor={cfg.particao as Record<string, unknown> | undefined}
                onChange={(v) => ctx.patchConfig(id, { particao: v })}
                phChave="chave (ex.: entidade_id)"
                phValor="valor ou :parametro"
                rotuloAdd="+ Adicionar chave"
                listaValor={`params-${id}`}
              />
              <DatalistParametros id={`params-${id}`} />
              <small>
                Sem escopo, lê todos os escopos correntes do dataset. Num dataset publicado por
                operação, declare a partição com <code>:parametro</code> — senão a corrida de uma
                operação lê o dado das outras.
              </small>
            </div>
            {amostra}
            <small>
              Lê o que um nó <b>Saída</b> publicou, pelo ponteiro do escopo — sem rodar de novo o
              pipeline que publicou. Dentro de uma cadeia, lê a publicação desta mesma corrida.
            </small>
            <CodeArea
              label="Consulta sobre o publicado (opcional)"
              titulo={`Consulta sobre o publicado · ${id}`}
              value={cfg.sql ?? ""}
              tema={ctx.tema}
              extensions={extFonte}
              onChange={(v) => ctx.patchConfig(id, { sql: v })}
            />
            <small>
              Em branco = o escopo inteiro. Com consulta, use <code>{alias}</code> como tabela.
            </small>
          </>
        ) : modo === "lake" ? (
          <>
            <label>
              Dataset do bronze:
              <select
                data-testid="fonte-lake"
                value={cfg.fonte_id ?? ""}
                onChange={(e) => ctx.patchConfig(id, { fonte_id: e.target.value })}
              >
                <option value="">— escolha —</option>
                {ctx.fontesLake.map((f) => (
                  <option key={f.id} value={f.id}>
                    {f.dataset}
                  </option>
                ))}
              </select>
            </label>
            {/* O corte por fatia é o que separa datasets que se sobrepõem (o COTAHIST anual
                cobre os mesmos pregões do diário) e o que evita ler dez anos sem precisar. */}
            <label>
              Da fatia:
              <input
                data-testid="lake-chave-min"
                placeholder="ex.: 2024 (vazio = desde a primeira)"
                value={(cfg.chave_min as string) ?? ""}
                onChange={(e) => ctx.patchConfig(id, { chave_min: e.target.value || undefined })}
              />
            </label>
            <label>
              Até a fatia:
              <input
                data-testid="lake-chave-max"
                placeholder="ex.: 2025 (vazio = até a última)"
                value={(cfg.chave_max as string) ?? ""}
                onChange={(e) => ctx.patchConfig(id, { chave_max: e.target.value || undefined })}
              />
            </label>
            {amostra}
            <small>
              A prévia lê só a fatia mais recente; a execução lê todas as do recorte. Cada fatia
              entra na versão mais nova que chegou ao bronze.
            </small>
            <CodeArea
              label="Consulta sobre o bronze"
              titulo={`Consulta sobre o bronze · ${id}`}
              value={cfg.sql ?? ""}
              tema={ctx.tema}
              extensions={extFonte}
              onChange={(v) => ctx.patchConfig(id, { sql: v })}
            />
            <small>
              Sem consulta, repassa o dataset inteiro. Com consulta, use <code>{alias}</code> como
              tabela.
            </small>
          </>
        ) : (
          <>
            <label>
              Pipeline:
              <select
                data-testid="fonte-pipeline"
                value={cfg.pipeline_id ?? ""}
                onChange={(e) => ctx.patchConfig(id, { pipeline_id: e.target.value })}
              >
                <option value="">— escolha —</option>
                {ctx.pipelines.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.nome}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Nó de saída:
              <select
                data-testid="fonte-no"
                value={cfg.no_id ?? ""}
                disabled={!cfg.pipeline_id}
                onChange={(e) => ctx.patchConfig(id, { no_id: e.target.value })}
              >
                <option value="">{cfg.pipeline_id ? "— escolha —" : "escolha um pipeline primeiro"}</option>
                {nosPipelineFonte.map((n) => (
                  <option key={n.id} value={n.id}>
                    {n.rotulo ? `${n.rotulo} (${n.id})` : n.id}
                  </option>
                ))}
                {/* Referência salva que não veio na lista atual (nó renomeado/removido, ou ainda carregando) — preserva pra não apagar em silêncio. */}
                {cfg.no_id && !nosPipelineFonte.some((n) => n.id === cfg.no_id) && (
                  <option value={cfg.no_id}>{cfg.no_id} (não encontrado)</option>
                )}
              </select>
            </label>
            {amostra}
            <CodeArea
              label="Consulta na fonte (opcional)"
              titulo={`Consulta na fonte · ${id}`}
              value={cfg.sql ?? ""}
              tema={ctx.tema}
              extensions={extFonte}
              onChange={(v) => ctx.patchConfig(id, { sql: v })}
            />
            <small>
              Em branco = lê o nó inteiro. Para filtrar/projetar, consulte <code>{alias}</code> (ex.:{" "}
              <code>SELECT * FROM {alias}</code>).
            </small>
          </>
        )}
      </div>
    );
  }
  if (cat === "escrita_sql") {
    const cfg = data.config ?? {};
    const modo: "tabela" | "sql" = cfg.modo === "sql" ? "sql" : "tabela";
    const estrategia = (cfg.estrategia as string) ?? "append";
    const mapeamento: { de: string; para: string }[] = Array.isArray(cfg.mapeamento) ? cfg.mapeamento : [];
    return (
      <div data-testid="form-escrita-sql" className="no-form">
        <small>
          Grava linhas em Postgres/SQL Server. <b>Sempre executa</b> (fora do reuso incremental) e
          exige conexão com escrita habilitada. Conecte <b>1 nó</b>.
        </small>
        <SelectConexaoEscrita
          testid="escrita-conexao"
          valor={cfg.conexao_id ?? ""}
          tipos={["postgres", "sqlserver"]}
          onChange={(v) => ctx.patchConfig(id, { conexao_id: v })}
        />
        <label>
          Modo:
          <select data-testid="escrita-modo" value={modo} onChange={(e) => ctx.patchConfig(id, { modo: e.target.value })}>
            <option value="tabela">Tabela</option>
            <option value="sql">SQL (DML livre)</option>
          </select>
        </label>
        {modo === "tabela" ? (
          <>
            <label>
              Schema:
              <input
                data-testid="escrita-schema"
                value={cfg.schema ?? ""}
                placeholder="ex.: public"
                onChange={(e) => ctx.patchConfig(id, { schema: e.target.value })}
              />
            </label>
            <label>
              Tabela:
              <input
                data-testid="escrita-tabela"
                value={cfg.tabela ?? ""}
                placeholder="ex.: resultado_vd"
                onChange={(e) => ctx.patchConfig(id, { tabela: e.target.value })}
              />
            </label>
            <label>
              Estratégia:
              <select
                data-testid="escrita-estrategia"
                value={estrategia}
                onChange={(e) => ctx.patchConfig(id, { estrategia: e.target.value })}
              >
                <option value="append">Anexar (append)</option>
                <option value="upsert">Upsert (por chave)</option>
                <option value="truncate_insert">Truncar e inserir</option>
              </select>
            </label>
            {estrategia === "upsert" && (
              <label>
                Chave (vírgula):
                <input
                  data-testid="escrita-chave"
                  value={(cfg.chave ?? []).join(",")}
                  placeholder="cod_emissor  ou  emissor_id,serie_id"
                  onChange={(e) => ctx.patchConfig(id, { chave: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) })}
                />
              </label>
            )}
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <span className="no-form-lbl">Mapeamento de colunas (opcional)</span>
              <EditorPares
                testid="escrita-mapeamento"
                valor={mapeamento}
                phDe="coluna do nó"
                phPara="coluna no destino"
                onChange={(v) => ctx.patchConfig(id, { mapeamento: v.length ? v : undefined })}
              />
              <small>Em branco = grava nas colunas de mesmo nome.</small>
            </div>
            {/* `limpar_escopo`: apaga e regrava o escopo da corrida na MESMA transação.
                Com "Truncar e inserir" o campo não vale — e esconder o campo deixaria o valor na
                definição travando o publicar, sem caminho de volta pela tela. Por isso o aviso com
                o botão de remover, e não o sumiço. */}
            {estrategia === "truncate_insert" ? (
              cfg.limpar_escopo ? (
                <div className="no-preview-erro" data-testid="escrita-escopo-conflito" style={{ margin: 0 }}>
                  O escopo configurado não vale com <b>Truncar e inserir</b> (a tabela inteira já é
                  apagada), e publicar recusa a combinação.{" "}
                  <button
                    type="button"
                    className="no-code-abrir"
                    data-testid="escrita-escopo-remover"
                    onClick={() => ctx.patchConfig(id, { limpar_escopo: undefined })}
                  >
                    Remover o escopo
                  </button>
                </div>
              ) : null
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <span className="no-form-lbl">Limpar o escopo da corrida (opcional)</span>
                <EditorMapa
                  testid="escrita-escopo"
                  valor={cfg.limpar_escopo as Record<string, unknown> | undefined}
                  onChange={(v) => ctx.patchConfig(id, { limpar_escopo: v })}
                  phChave="coluna no destino"
                  phValor=":parametro"
                  rotuloAdd="+ Adicionar coluna do escopo"
                  listaValor={`params-${id}`}
                />
                <DatalistParametros id={`params-${id}`} />
                <small>
                  Na mesma transação da gravação, apaga no destino as linhas cuja coluna tem o valor do
                  parâmetro — o escopo desta corrida — e grava as novas. O parâmetro tem de ser
                  obrigatório; a coluna, gravada pelo mapeamento.
                </small>
              </div>
            )}
            <label className="no-check">
              <input
                type="checkbox"
                data-testid="escrita-permitir-vazio"
                checked={!!cfg.permitir_vazio}
                onChange={(e) => ctx.patchConfig(id, { permitir_vazio: e.target.checked })}
              />
              Permitir vazio (0 linhas não aborta)
            </label>
            <label>
              Tamanho do lote:
              <input
                type="number"
                data-testid="escrita-lote"
                value={cfg.lote_tamanho ?? 1000}
                onChange={(e) => ctx.patchConfig(id, { lote_tamanho: Number(e.target.value) })}
              />
            </label>
          </>
        ) : (
          <>
            {cfg.limpar_escopo ? (
              <div className="no-preview-erro" data-testid="escrita-escopo-conflito" style={{ margin: 0 }}>
                O escopo configurado só vale no modo <b>Tabela</b>; no modo SQL, publicar recusa.{" "}
                <button
                  type="button"
                  className="no-code-abrir"
                  data-testid="escrita-escopo-remover"
                  onClick={() => ctx.patchConfig(id, { limpar_escopo: undefined })}
                >
                  Remover o escopo
                </button>
              </div>
            ) : null}
            <CodeArea
              label="Comando SQL (DML)"
              titulo={`Escrita SQL · ${id}`}
              value={cfg.sql ?? ""}
              tema={ctx.tema}
              extensions={ctx.extensoesSql as unknown[]}
              onChange={(v) => ctx.patchConfig(id, { sql: v })}
            />
            <small>
              Um único comando DML. Use <code>_entrada</code> para as linhas do nó anterior e{" "}
              <code>:parametro</code> para parâmetros do pipeline.
            </small>
          </>
        )}
        <CampoGrupoSerial id={id} cfg={cfg} />
      </div>
    );
  }
  if (cat === "http") {
    const cfg = data.config ?? {};
    const modo: "unico" | "por_linha" | "lote" =
      cfg.modo === "por_linha" ? "por_linha" : cfg.modo === "lote" ? "lote" : "unico";
    const corpoTipo: "colunas" | "template" | "coluna_json" =
      cfg.corpo_tipo === "template" ? "template" : cfg.corpo_tipo === "coluna_json" ? "coluna_json" : "colunas";
    const corpoCampos: { de: string; para: string }[] = Array.isArray(cfg.corpo_campos) ? cfg.corpo_campos : [];
    // Headers dinâmicos: guardados como lista de {nome, valor}; editados no EditorPares (de→para).
    const headersExtra: { nome: string; valor: string }[] = Array.isArray(cfg.headers_extra) ? cfg.headers_extra : [];
    const headersPares = headersExtra.map((h) => ({ de: h.nome ?? "", para: h.valor ?? "" }));
    // `concorrencia` ausente = 1 (default do backend) → o toggle "uma por vez" começa ligado.
    const sequencial = (cfg.concorrencia ?? 1) <= 1;
    const respostaCampos: { de: string; para: string }[] = Array.isArray(cfg.resposta_campos) ? cfg.resposta_campos : [];
    return (
      <div data-testid="form-http" className="no-form">
        <small>
          Chama uma API; a resposta vira o dado deste nó. <b>Sempre executa</b> (fora do reuso
          incremental) e exige conexão REST com escrita habilitada. Conecte <b>1 nó</b>.
        </small>
        <SelectConexaoEscrita
          testid="http-conexao"
          valor={cfg.conexao_id ?? ""}
          tipos={["api_rest"]}
          onChange={(v) => ctx.patchConfig(id, { conexao_id: v })}
        />
        <label>
          Método:
          <select data-testid="http-metodo" value={cfg.metodo ?? "POST"} onChange={(e) => ctx.patchConfig(id, { metodo: e.target.value })}>
            <option value="GET">GET</option>
            <option value="POST">POST</option>
            <option value="PUT">PUT</option>
            <option value="PATCH">PATCH</option>
            <option value="DELETE">DELETE</option>
          </select>
        </label>
        <label>
          Caminho:
          <input
            data-testid="http-caminho"
            value={cfg.caminho ?? ""}
            placeholder="/v1/registros"
            onChange={(e) => ctx.patchConfig(id, { caminho: e.target.value })}
          />
        </label>
        <small>
          Caminho relativo — começa com <code>/</code> (nunca <code>//</code>). A URL final é a base
          da conexão + este caminho.
        </small>
        <label>
          Modo:
          <select data-testid="http-modo" value={modo} onChange={(e) => ctx.patchConfig(id, { modo: e.target.value })}>
            <option value="unico">Uma chamada só (não usa as linhas)</option>
            <option value="por_linha">Uma requisição por linha (individual)</option>
            <option value="lote">Lote (várias linhas por requisição)</option>
          </select>
        </label>
        <label>
          Corpo:
          <select data-testid="http-corpo-tipo" value={corpoTipo} onChange={(e) => ctx.patchConfig(id, { corpo_tipo: e.target.value })}>
            <option value="colunas">Colunas (objeto gerado)</option>
            <option value="template">Template (JSON com placeholders)</option>
            <option value="coluna_json">Coluna JSON crua (envia uma coluna verbatim)</option>
          </select>
        </label>
        {corpoTipo === "colunas" && (
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <span className="no-form-lbl">Campos do corpo (de→para, opcional)</span>
            <EditorPares
              testid="http-corpo-campos"
              valor={corpoCampos}
              phDe="coluna do nó"
              phPara="campo no corpo"
              onChange={(v) => ctx.patchConfig(id, { corpo_campos: v.length ? v : undefined })}
            />
            <small>Em branco = envia todas as colunas da linha.</small>
          </div>
        )}
        {corpoTipo === "template" && (
          <>
            <CodeArea
              label="Template do corpo"
              titulo={`Corpo HTTP · ${id}`}
              value={cfg.corpo ?? ""}
              tema={ctx.tema}
              extensions={[]}
              validarSintaxe={() => true}
              onChange={(v) => ctx.patchConfig(id, { corpo: v })}
            />
            <small>
              JSON com placeholders: <code>{"{{coluna}}"}</code> (valor da linha, JSON-encoded —
              escreva SEM aspas ao redor) e <code>:parametro</code>. Ex.:{" "}
              <code>{'{"nome": {{nome}}}'}</code>.
            </small>
          </>
        )}
        {corpoTipo === "coluna_json" && (
          <>
            <label>
              Coluna com o JSON:
              <input
                data-testid="http-corpo-coluna"
                value={cfg.corpo_coluna ?? ""}
                placeholder="ex.: body"
                onChange={(e) => ctx.patchConfig(id, { corpo_coluna: e.target.value.trim() || undefined })}
              />
            </label>
            <small>
              O valor dessa coluna é enviado como o corpo <b>verbatim</b>. Texto é interpretado como
              JSON; objeto/array (ex.: coluna jsonb) vai como está.
            </small>
          </>
        )}
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <span className="no-form-lbl">Headers adicionais (opcional)</span>
          <EditorPares
            testid="http-headers-extra"
            valor={headersPares}
            phDe="nome do header"
            phPara="valor (aceita placeholders)"
            rotuloAdd="+ Adicionar header"
            onChange={(v) =>
              ctx.patchConfig(id, {
                headers_extra: v.length ? v.map((p) => ({ nome: p.de, valor: p.para })) : undefined,
              })
            }
          />
          <small>
            Mesclados por cima dos headers da conexão, resolvidos por requisição. O valor aceita{" "}
            <code>{"{{coluna}}"}</code> (valor da linha) e <code>:parametro</code>. Para segredos
            (token/Bearer) prefira <code>:parametro</code> — <code>{"{{coluna}}"}</code> faria o token
            aparecer como coluna no dado de entrada.
          </small>
        </div>
        {/* "Uma por vez" é a pergunta que o especialista realmente faz (muitas APIs externas não aceitam
            paralelo); "Concorrência (1–16)" era um número escondido que respondia isso sem dizer.
            O toggle é a MESMA config (`concorrencia: 1`) — desmarcar revela o número. */}
        <label className="no-check">
          <input
            type="checkbox"
            data-testid="http-sequencial"
            checked={sequencial}
            onChange={(e) => ctx.patchConfig(id, { concorrencia: e.target.checked ? 1 : 4 })}
          />
          Uma requisição por vez (sequencial, na ordem das linhas)
        </label>
        {/* Campos numéricos curtos em duas colunas: um por linha empilhava 4 linhas de 300px de
            largura para guardar números de 3 dígitos. */}
        <div className="no-form-dupla">
          {!sequencial && (
            <label>
              Requisições em paralelo (2–16):
              <input
                type="number"
                min={2}
                max={16}
                data-testid="http-concorrencia"
                value={cfg.concorrencia ?? 4}
                onChange={(e) => ctx.patchConfig(id, { concorrencia: Number(e.target.value) })}
              />
            </label>
          )}
          <label>
            Timeout (s, 1–600):
            <input
              type="number"
              min={1}
              max={600}
              data-testid="http-timeout"
              value={cfg.timeout_segundos ?? 30}
              onChange={(e) => ctx.patchConfig(id, { timeout_segundos: Number(e.target.value) })}
            />
          </label>
          <label>
            Taxa por segundo (opcional):
            <input
              type="number"
              min={0}
              step="0.1"
              data-testid="http-taxa"
              value={cfg.taxa_por_segundo ?? ""}
              placeholder="sem limite"
              onChange={(e) => {
                const t = e.target.value.trim();
                ctx.patchConfig(id, { taxa_por_segundo: t === "" ? undefined : Number(t) });
              }}
            />
          </label>
          {modo === "lote" && (
            <label>
              Tamanho do lote:
              <input
                type="number"
                data-testid="http-lote"
                value={cfg.lote_tamanho ?? 500}
                onChange={(e) => ctx.patchConfig(id, { lote_tamanho: Number(e.target.value) })}
              />
            </label>
          )}
        </div>
        <label>
          Falha de item:
          <select data-testid="http-tolerancia" value={cfg.tolerancia_falha ?? "abortar"} onChange={(e) => ctx.patchConfig(id, { tolerancia_falha: e.target.value })}>
            <option value="abortar">Abortar tudo</option>
            <option value="continuar">Continuar (registra o erro por linha)</option>
          </select>
        </label>
        {modo === "unico" ? (
          <label>
            Caminho JSON da resposta (opcional):
            <input
              data-testid="http-resposta-json-path"
              value={cfg.resposta_json_path ?? ""}
              placeholder="ex.: data"
              onChange={(e) => ctx.patchConfig(id, { resposta_json_path: e.target.value.trim() || undefined })}
            />
          </label>
        ) : (
          // por_linha/lote: a resposta vira COLUNA. Sem isto, usar um valor do retorno (nº do
          // boleto, mensagem de erro) obrigava a parsear `_resposta` num nó SQL depois.
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <span className="no-form-lbl">Valores da resposta (opcional)</span>
            <EditorPares
              testid="http-resposta-campos"
              valor={respostaCampos}
              phDe="caminho no JSON (ex.: dados.0.numero)"
              phPara="nome da coluna"
              rotuloAdd="+ Extrair valor da resposta"
              onChange={(v) => ctx.patchConfig(id, { resposta_campos: v.length ? v : undefined })}
            />
            <small>
              Cada valor extraído vira uma <b>coluna</b> deste nó, ao lado das colunas que entraram:
              o nó seguinte consolida tudo numa consulta só (1000 linhas + retorno), manda um e-mail
              por linha (<code>{"{{coluna}}"}</code>) ou grava num nó de escrita. Caminho não
              encontrado fica vazio (não derruba a linha).
            </small>
            <label className="no-check">
              <input
                type="checkbox"
                data-testid="http-incluir-resposta"
                checked={cfg.incluir_resposta_completa !== false}
                onChange={(e) => ctx.patchConfig(id, { incluir_resposta_completa: e.target.checked })}
              />
              Manter também a resposta completa (coluna <code>_resposta</code>)
            </label>
          </div>
        )}
        <label>
          Resposta de exemplo (JSON):
          <textarea
            data-testid="http-resposta-exemplo"
            className="no-descricao"
            rows={3}
            value={cfg.resposta_exemplo ?? ""}
            placeholder={'{"id": 123}'}
            onChange={(e) => ctx.patchConfig(id, { resposta_exemplo: e.target.value })}
          />
        </label>
        <small>Necessário para o preview dos nós seguintes enxergarem o formato da resposta.</small>
        <small>
          Para gravar em paralelo às análises, use um ramo independente do grafo ou um pipeline
          irmão (fonte tipo pipeline).
        </small>
        <CampoGrupoSerial id={id} cfg={cfg} />
      </div>
    );
  }
  if (cat === "ingestao") {
    // Nó de ingestão: traz dado público externo para o lake, camada bronze.
    //
    // O formulário é curto DE PROPÓSITO. URL, formato, encoding, schema esperado e cadência vivem
    // no contrato declarativo da fonte (`ingestao_fontes.contrato`), mantido pela operação por
    // API/script — repetir esses campos aqui criaria duas verdades sobre a mesma origem, e a do
    // grafo venceria em silêncio. O que o especialista escolhe é QUAL fonte e, no máximo, QUE
    // janela; o backend recusa qualquer campo de contrato dentro do nó.
    const cfg = data.config ?? {};
    const fonte = ctx.fontesLake.find((f) => f.id === cfg.fonte_id) ?? null;
    const janelaAberta = !!(cfg.inicio || cfg.fim);
    return (
      <div data-testid="form-ingestao" className="no-form">
        <small>
          Baixa da origem pública e grava no lake (<b>landing → bronze</b>). <b>Sempre executa</b>{" "}
          (fora do reuso incremental) e <b>não tem entrada</b>: o dado vem de fora, não do nó
          anterior. A saída deste nó é o <b>log por fatia</b> — o dado ingerido é lido depois por um
          nó <b>Fonte</b> de origem <i>lake</i>.
        </small>
        <label>
          Fonte de ingestão:
          <select
            data-testid="ing-fonte"
            value={cfg.fonte_id ?? ""}
            onChange={(e) => ctx.patchConfig(id, { fonte_id: e.target.value })}
          >
            <option value="">— escolha a fonte —</option>
            {ctx.fontesLake.map((f) => (
              <option key={f.id} value={f.id}>
                {f.nome} ({f.dataset})
              </option>
            ))}
          </select>
        </label>
        {/* Espelho do contrato, só leitura: é o que responde "esta é a fonte certa?" sem sair da
            tela — e deixa evidente que estes campos NÃO se editam aqui. */}
        {fonte && (
          <small data-testid="ing-meta">
            dataset <b>{fonte.dataset}</b> · acesso <b>{fonte.classe}</b> · recarga{" "}
            <b>{fonte.cadencia ?? "não informada"}</b>
            {fonte.watermark ? (
              <>
                {" "}
                · ingerido até <b>{fonte.watermark}</b>
              </>
            ) : (
              <> · nunca ingerido</>
            )}
            {fonte.ativo === false && <> · fonte desativada (só disparo manual)</>}
          </small>
        )}
        {fonte && (
          <small>
            Para mudar URL, formato ou cadência, edite a <b>fonte</b> (admin, fora do Studio) — o
            contrato é dela, não deste nó.
          </small>
        )}
        <label>
          <input
            data-testid="ing-janela-fixa"
            type="checkbox"
            checked={janelaAberta}
            onChange={(e) =>
              ctx.patchConfig(id, e.target.checked ? { inicio: "", fim: "" } : { inicio: null, fim: null })
            }
          />{" "}
          Janela fixa (em vez de incremental)
        </label>
        {!janelaAberta && (
          <small>
            Incremental: parte de onde a última execução parou e vai até hoje. É o modo da recarga
            cadenciada.
          </small>
        )}
        {janelaAberta && (
          <>
            <label>
              De:
              <input
                data-testid="ing-inicio"
                type="date"
                value={cfg.inicio ?? ""}
                onChange={(e) => ctx.patchConfig(id, { inicio: e.target.value })}
              />
            </label>
            <label>
              Até:
              <input
                data-testid="ing-fim"
                type="date"
                value={cfg.fim ?? ""}
                onChange={(e) => ctx.patchConfig(id, { fim: e.target.value })}
              />
            </label>
            {/* Sem `:param` aqui, e o aviso diz por quê: a janela decide QUANTAS requisições vão
                para a origem pública, e "em branco = tudo" viraria backfill de dez anos sem querer. */}
            <small>
              Uma requisição por fatia da janela. Janela longa é <b>backfill</b> — prefira a rota de
              admin, que existe justamente para isso.
            </small>
          </>
        )}
        <CampoGrupoSerial id={id} cfg={cfg} />
      </div>
    );
  }
  if (cat === "email") {
    const cfg = data.config ?? {};
    const modo: "unico" | "por_linha" = cfg.modo === "por_linha" ? "por_linha" : "unico";
    const corpoTipo: "html" | "texto" = cfg.corpo_tipo === "texto" ? "texto" : "html";
    const anexos: AnexoEmail[] = Array.isArray(cfg.anexos) ? cfg.anexos : [];
    return (
      <div data-testid="form-email" className="no-form">
        <small>
          Envia e-mail pelo servidor SMTP da conexão. <b>Sempre executa</b> (fora do reuso
          incremental) e exige conexão SMTP com escrita habilitada. Conecte <b>1 nó</b> (ou nenhum,
          para um aviso sem dado).
        </small>
        <SelectConexaoEscrita
          testid="email-conexao"
          valor={cfg.conexao_id ?? ""}
          tipos={["smtp"]}
          onChange={(v) => ctx.patchConfig(id, { conexao_id: v })}
        />
        <label>
          Modo:
          <select data-testid="email-modo" value={modo} onChange={(e) => ctx.patchConfig(id, { modo: e.target.value })}>
            <option value="unico">Único (um e-mail com todas as linhas)</option>
            <option value="por_linha">Por linha (um e-mail por linha)</option>
          </select>
        </label>
        <label>
          Para:
          <input
            data-testid="email-para"
            value={cfg.para ?? ""}
            placeholder={modo === "por_linha" ? "{{email}} ou fixo@example.com" : "alguem@example.com; outro@example.com"}
            onChange={(e) => ctx.patchConfig(id, { para: e.target.value })}
          />
        </label>
        <small>
          Separe por <code>;</code> ou <code>,</code>. Aceita <code>{"{{coluna}}"}</code> (modo por
          linha) e <code>{"{{param.nome}}"}</code>.
        </small>
        <div className="no-form-dupla">
          <label>
            Cc (opcional):
            <input
              data-testid="email-cc"
              value={cfg.cc ?? ""}
              onChange={(e) => ctx.patchConfig(id, { cc: e.target.value || undefined })}
            />
          </label>
          <label>
            Cco (opcional):
            <input
              data-testid="email-cco"
              value={cfg.cco ?? ""}
              onChange={(e) => ctx.patchConfig(id, { cco: e.target.value || undefined })}
            />
          </label>
          <label>
            Responder para (opcional):
            <input
              data-testid="email-responder-para"
              value={cfg.responder_para ?? ""}
              onChange={(e) => ctx.patchConfig(id, { responder_para: e.target.value || undefined })}
            />
          </label>
          <label>
            Nome do remetente (opcional):
            <input
              data-testid="email-remetente-nome"
              value={cfg.remetente_nome ?? ""}
              placeholder="em branco = o da conexão"
              onChange={(e) => ctx.patchConfig(id, { remetente_nome: e.target.value || undefined })}
            />
          </label>
        </div>
        <label>
          Assunto:
          <input
            data-testid="email-assunto"
            value={cfg.assunto ?? ""}
            placeholder="Relatório de {{param.competencia}} — {{contagem}} linhas"
            onChange={(e) => ctx.patchConfig(id, { assunto: e.target.value })}
          />
        </label>
        <label>
          Corpo:
          <select
            data-testid="email-corpo-tipo"
            value={corpoTipo}
            onChange={(e) => ctx.patchConfig(id, { corpo_tipo: e.target.value })}
          >
            <option value="html">HTML (você escreve o template)</option>
            <option value="texto">Texto puro</option>
          </select>
        </label>
        <CodeArea
          label={corpoTipo === "html" ? "Template do e-mail (HTML)" : "Corpo do e-mail (texto)"}
          titulo={`Template do e-mail · ${id}`}
          value={cfg.corpo ?? ""}
          tema={ctx.tema}
          extensions={[]}
          validarSintaxe={() => true}
          onChange={(v) => ctx.patchConfig(id, { corpo: v })}
        />
        <small>
          Placeholders: <code>{"{{tabela}}"}</code> (as linhas do nó anterior),{" "}
          <code>{"{{contagem}}"}</code>, <code>{"{{data_hora}}"}</code>,{" "}
          <code>{"{{param.nome}}"}</code> e <code>{"{{coluna}}"}</code> (modo por linha). Valor de
          dado vai escapado; o que não resolver aparece como <b>não resolvido</b> na prévia.
        </small>
        <label>
          Linhas em {"{{tabela}}"} (1–5000):
          <input
            type="number"
            min={1}
            max={5000}
            data-testid="email-tabela-limite"
            value={cfg.tabela_limite ?? 100}
            onChange={(e) => ctx.patchConfig(id, { tabela_limite: Number(e.target.value) })}
          />
        </label>
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <span className="no-form-lbl">Anexos (opcional)</span>
          <EditorAnexos
            testid="email-anexos"
            valor={anexos}
            onChange={(v) => ctx.patchConfig(id, { anexos: v })}
          />
        </div>
        <label className="no-check">
          <input
            type="checkbox"
            data-testid="email-permitir-vazio"
            checked={!!cfg.permitir_vazio}
            onChange={(e) => ctx.patchConfig(id, { permitir_vazio: e.target.checked })}
          />
          Enviar mesmo com 0 linhas de entrada
        </label>
        <div className="no-form-dupla">
          <label>
            Timeout (s, 1–600):
            <input
              type="number"
              min={1}
              max={600}
              data-testid="email-timeout"
              value={cfg.timeout_segundos ?? ""}
              placeholder="o da conexão"
              onChange={(e) => {
                const t = e.target.value.trim();
                ctx.patchConfig(id, { timeout_segundos: t === "" ? undefined : Number(t) });
              }}
            />
          </label>
          <label>
            Tentativas (1–10):
            <input
              type="number"
              min={1}
              max={10}
              data-testid="email-tentativas"
              value={cfg.tentativas ?? ""}
              placeholder="o da conexão"
              onChange={(e) => {
                const t = e.target.value.trim();
                ctx.patchConfig(id, { tentativas: t === "" ? undefined : Number(t) });
              }}
            />
          </label>
        </div>
        {modo === "por_linha" && (
          <label>
            Falha de item:
            <select
              data-testid="email-tolerancia"
              value={cfg.tolerancia_falha ?? "abortar"}
              onChange={(e) => ctx.patchConfig(id, { tolerancia_falha: e.target.value })}
            >
              <option value="abortar">Abortar tudo</option>
              <option value="continuar">Continuar (registra o erro por linha)</option>
            </select>
          </label>
        )}
        <small>
          A prévia deste nó é <b>dry-run</b>: monta o e-mail e os anexos, mas não envia nada.
        </small>
        <CampoGrupoSerial id={id} cfg={cfg} />
      </div>
    );
  }
  if (cat === "juntar" || cat === "filtrar" || cat === "agrupar") {
    const op = data.config.op;
    // Juntar tem dois modos (mesma op, JSON `op.modo`): junção por chave (JOIN, default,
    // exige condição) OU união (UNION [ALL] BY NAME — empilha 2+ entradas de mesmas colunas).
    const ehUniao = op.tipo === "juntar" && op.modo === "uniao";
    const modoJuntar = ehUniao ? (op.distinto ? "uniao_distinta" : "uniao") : "juncao";
    return (
      <div data-testid="form-guiada" className="no-form">
        <small>
          Operação <b>{op.tipo}</b>. Conecte os nós de entrada (
          {op.tipo === "juntar" ? (ehUniao ? "2 ou mais nós" : "2 nós") : "1 nó"}).
        </small>
        {op.tipo === "juntar" && (
          <label>
            Tipo:
            <select
              data-testid="op-modo"
              value={modoJuntar}
              onChange={(e) => {
                const v = e.target.value;
                ctx.patchOp(
                  id,
                  v === "juncao"
                    ? { modo: "juncao", distinto: false }
                    : { modo: "uniao", distinto: v === "uniao_distinta" },
                );
              }}
            >
              <option value="juncao">Junção por chave (JOIN)</option>
              <option value="uniao">União — empilhar linhas (UNION ALL)</option>
              <option value="uniao_distinta">União sem duplicatas (UNION)</option>
            </select>
          </label>
        )}
        {ehUniao && (
          <small>As entradas precisam ter as mesmas colunas (alinhadas pelo nome).</small>
        )}
        {((op.tipo === "juntar" && !ehUniao) || op.tipo === "filtrar") && (
          <label>
            Condição:
            <input
              data-testid="op-condicao"
              value={op.condicao ?? ""}
              placeholder={op.tipo === "juntar" ? "n1.id = n2.id" : "coluna > 0"}
              onChange={(e) => ctx.patchOp(id, { condicao: e.target.value })}
            />
          </label>
        )}
        {op.tipo === "agrupar" && (
          <>
            <label>
              Chaves (vírgula):
              <input
                data-testid="op-chaves"
                value={(op.chaves ?? []).join(",")}
                onChange={(e) => ctx.patchOp(id, { chaves: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) })}
              />
            </label>
            <label>
              Agregações (vírgula):
              <input
                data-testid="op-agregacoes"
                value={(op.agregacoes ?? []).join(",")}
                onChange={(e) => ctx.patchOp(id, { agregacoes: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) })}
              />
            </label>
          </>
        )}
      </div>
    );
  }
  if (cat === "python") {
    const linguagem = (data.config?.linguagem as string) ?? "python";
    const ehCsharp = linguagem === "csharp";
    return (
      <div data-testid="form-python" className="no-form">
        <label>
          Linguagem
          <select
            data-testid="codigo-linguagem"
            value={linguagem}
            onChange={(e) => ctx.patchConfig(id, { linguagem: e.target.value })}
          >
            <option value="python">Python</option>
            <option value="csharp">C#</option>
          </select>
        </label>
        {ehCsharp ? (
          <small>
            Cálculo .NET real em sandbox (equivalência bit-a-bit). Defina{" "}
            <code>public static class Regra</code> com{" "}
            <code>public static IEnumerable&lt;IDictionary&lt;string, object&gt;&gt; Executar(entradas, parametros)</code>.
            Leia um nó anterior com <code>Cflow.LerParquet(entradas["id_do_no"])</code> e retorne as
            linhas (dicionário coluna→valor).
          </small>
        ) : (
          <small>
            Procedural em sandbox. Globais: <code>con</code> (DuckDB), <code>entradas</code>,{" "}
            <code>parametros</code>. Os nós anteriores entram como tabela pelo <b>id</b> (ex.:{" "}
            <code>con.sql("SELECT * FROM no_anterior")</code>). Defina <b>resultado</b> (relação DuckDB
            ou lista de dicts).
          </small>
        )}
        <CodeArea
          label={ehCsharp ? "Código C#" : "Código Python"}
          titulo={`${ehCsharp ? "Código C#" : "Código Python"} · ${id}`}
          value={data.config?.codigo ?? ""}
          tema={ctx.tema}
          extensions={ehCsharp ? [] : [python()]}
          validarSintaxe={() => true}
          onChange={(v) => ctx.patchConfig(id, { codigo: v })}
        />
      </div>
    );
  }
  if (cat === "comparacao") {
    const cfg = data.config ?? {};
    return (
      <div data-testid="form-comparacao" className="no-form">
        <small>
          Diff por chave entre 2 entradas. Saída: a chave + <code>_status</code> (so_esquerda /
          so_direita / divergente). Conecte <b>2 nós</b>.
        </small>
        <label>
          Chave (vírgula):
          <input
            data-testid="cmp-chave"
            value={(cfg.chave ?? []).join(",")}
            placeholder="id  ou  emissor_id,serie_id"
            onChange={(e) => ctx.patchConfig(id, { chave: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) })}
          />
        </label>
        <label>
          Esquerda (opcional):
          <input
            data-testid="cmp-esquerda"
            value={cfg.esquerda ?? ""}
            placeholder="padrão: 1ª entrada"
            onChange={(e) => ctx.patchConfig(id, { esquerda: e.target.value.trim() || undefined })}
          />
        </label>
        <label>
          Direita (opcional):
          <input
            data-testid="cmp-direita"
            value={cfg.direita ?? ""}
            placeholder="padrão: 2ª entrada"
            onChange={(e) => ctx.patchConfig(id, { direita: e.target.value.trim() || undefined })}
          />
        </label>
        <label className="no-check">
          <input
            type="checkbox"
            data-testid="cmp-incluir-iguais"
            checked={!!cfg.incluir_iguais}
            onChange={(e) => ctx.patchConfig(id, { incluir_iguais: e.target.checked })}
          />
          Incluir linhas iguais
        </label>
      </div>
    );
  }
  if (cat === "saida") {
    const cfg = data.config ?? {};
    const destino: "arquivo" | "lake" = cfg.destino === "lake" ? "lake" : "arquivo";
    // Trocar de destino limpa os campos do outro: config meio de um e meio de outro é recusada na
    // validação, e o erro só apareceria no publicar.
    // `camada` e `particao` fora do lake são RECUSADAS — trocar para arquivo as tira.
    const trocarDestino = (d: "arquivo" | "lake") =>
      d === "lake"
        ? ctx.patchConfig(id, {
            destino: "lake", nome: undefined, caminho: undefined, formato: undefined,
          })
        : ctx.patchConfig(id, {
            destino: undefined, dataset: undefined, camada: undefined, particao: undefined,
          });
    return (
      <div data-testid="form-saida" className="no-form">
        <small>
          Publica o resultado da entrada num destino estável (consumo externo). Conecte <b>1 nó</b>.
        </small>
        <label>
          Destino:
          <select
            data-testid="saida-destino"
            value={destino}
            onChange={(e) => trocarDestino(e.target.value as "arquivo" | "lake")}
          >
            <option value="arquivo">Arquivo no servidor</option>
            <option value="lake">Lake (silver ou gold)</option>
          </select>
        </label>
        {destino === "lake" ? (
          <>
            <label>
              Dataset:
              <input
                data-testid="saida-dataset"
                value={cfg.dataset ?? ""}
                placeholder="ex.: cri_geral (letras, números, _ ou -)"
                onChange={(e) => ctx.patchConfig(id, { dataset: e.target.value })}
              />
            </label>
            {/* Camada e partição só se configuravam por JSON. A tela escreve os MESMOS campos que a validação lê. */}
            <label>
              Camada:
              <select
                data-testid="saida-camada"
                value={cfg.camada === "gold" ? "gold" : "silver"}
                onChange={(e) => ctx.patchConfig(id, { camada: e.target.value })}
              >
                <option value="silver">silver — a tabela da origem, tipada</option>
                <option value="gold">gold — produto conformado</option>
              </select>
            </label>
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <span className="no-form-lbl">Partição (opcional, na ordem do caminho)</span>
              <EditorParticaoSaida
                valor={Array.isArray(cfg.particao) ? (cfg.particao as ItemParticao[]) : undefined}
                onChange={(v) => ctx.patchConfig(id, { particao: v })}
                listaParametros={`nomes-params-${id}`}
              />
              <datalist id={`nomes-params-${id}`}>
                {ctx.parametros
                  .filter((p) => !p.segredo && p.nome)
                  .map((p) => (
                    <option key={p.nome} value={p.nome} />
                  ))}
              </datalist>
              <small>
                Parâmetro: o valor da corrida vira o escopo (ex.: <code>entidade_id</code>). Coluna:
                uma publicação por valor distinto — e a coluna continua no Parquet. Cada escopo tem o
                seu ponteiro <code>_atual.json</code>.
              </small>
            </div>
            <small>
              Cada execução grava uma versão nova e move o ponteiro <code>_atual.json</code>. A
              versão anterior continua no bucket — nada é sobrescrito. Sempre Parquet.
            </small>
          </>
        ) : (
          <>
        <label>
          Nome (slug):
          <input
            data-testid="saida-nome"
            value={cfg.nome ?? ""}
            placeholder="ex.: vendas (letras, números, _ ou -)"
            onChange={(e) => ctx.patchConfig(id, { nome: e.target.value })}
          />
        </label>
        <label>
          Formato:
          <select
            data-testid="saida-formato"
            value={cfg.formato ?? "parquet"}
            onChange={(e) => ctx.patchConfig(id, { formato: e.target.value })}
          >
            <option value="parquet">Parquet</option>
            <option value="csv">CSV</option>
          </select>
        </label>
        <label>
          Caminho (opcional):
          <input
            data-testid="saida-caminho"
            value={cfg.caminho ?? ""}
            placeholder="padrão: export do servidor"
            onChange={(e) => ctx.patchConfig(id, { caminho: e.target.value.trim() || undefined })}
          />
        </label>
          </>
        )}
        <label>
          Mín. linhas:
          <input
            type="number"
            data-testid="saida-min-linhas"
            value={cfg.min_linhas ?? 1}
            onChange={(e) => ctx.patchConfig(id, { min_linhas: Number(e.target.value) })}
          />
        </label>
        <label className="no-check">
          <input
            type="checkbox"
            data-testid="saida-permitir-vazio"
            checked={!!cfg.permitir_vazio}
            onChange={(e) => ctx.patchConfig(id, { permitir_vazio: e.target.checked })}
          />
          Permitir vazio (não aborta se abaixo do mínimo)
        </label>
        {/* Só a saída fora do lake é serial (`paralelo.serial`); na `lake` a validação recusa o campo. */}
        {destino !== "lake" && <CampoGrupoSerial id={id} cfg={cfg} />}
      </div>
    );
  }
  // nó SQL
  const cfgSql = data.config ?? {};
  return (
    <div className="no-form">
      <small>
        Nós anteriores entram como tabela pelo <b>id</b> (ex.: <code>n7</code>) ou pelo <b>rótulo</b>{" "}
        (se único; rótulo com espaço vai entre aspas duplas).
      </small>
      <CodeArea label="Código SQL" titulo={`Código SQL · ${id}`} value={data.sql ?? ""} tema={ctx.tema} extensions={ctx.extensoesSql as unknown[]} onChange={(v) => ctx.patchSql(id, v)} />
      <label>
        Amostra:
        <input
          data-testid="sql-amostra"
          type="number"
          value={cfgSql.amostra ?? 10}
          onChange={(e) => ctx.patchConfig(id, { amostra: Number(e.target.value) })}
        />
      </label>
    </div>
  );
}

// Parâmetros que ESTE nó usa (detectados pelos `:nome` no seu SQL/condição). O valor de preview é
// editável aqui mesmo. Em branco = traz tudo (sem filtro) no preview.
function ParamsNo({ data }: { data: any }) {
  const ctx = useEditorNo();
  const refs = refsDoNo(data);
  const [aberto, setAberto] = useState(false); // recolhido por padrão; o usuário expande/recolhe
  if (!refs.length) return null;
  return (
    <div className="no-bloco" data-testid="no-parametros">
      <button type="button" className="no-bloco-cab" data-testid="params-toggle" onClick={() => setAberto((a) => !a)}>
        <span className="no-bloco-tit">Parâmetros usados ({refs.length})</span>
        <span className={`no-bloco-chev${aberto ? " aberto" : ""}`}>
          <Icon name="chevron" />
        </span>
      </button>
      {aberto &&
        refs.map((nome) => {
        const p: any = ctx.parametros.find((x) => x.nome === nome);
        if (!p) {
          return (
            <div key={nome} className="no-param no-param--alerta">
              <code>:{nome}</code> <span>não declarado — declare na aba Parâmetros</span>
            </div>
          );
        }
        return (
          <label key={nome} className="no-param">
            <code>:{nome}</code>
            <input
              data-testid={`no-param-${nome}`}
              value={p.valor_preview != null ? String(p.valor_preview) : ""}
              placeholder="em branco = tudo"
              onChange={(e) => ctx.setValorPreview(nome, e.target.value)}
            />
          </label>
        );
      })}
    </div>
  );
}

// Funções (bibliotecas de MACROs) do nó — escala p/ muitas (busca + lista rolável). As que o nó USA
// (MACRO chamada no SQL) já vêm ATIVAS automaticamente (badge "auto"); o resto é opcional por nó.
function FuncoesNo({ id, data }: { id: string; data: any }) {
  const ctx = useEditorNo();
  const [aberto, setAberto] = useState(false);
  const [busca, setBusca] = useState("");
  if (!ctx.bibliotecas.length) return null;
  const selecionadas: string[] = (data?.config?.bibliotecas ?? []).map(String);
  const auto = new Set(bibsDetectadas(data, ctx.macroParaBib)); // detectadas por uso (só p/ rótulo "auto")
  const termo = busca.trim().toLowerCase();
  const filt = ctx.bibliotecas.filter((b) => b.nome.toLowerCase().includes(termo));
  return (
    <div className="no-bloco" data-testid="no-funcoes">
      <span className="no-bloco-tit">Funções{selecionadas.length ? ` (${selecionadas.length})` : ""}</span>
      <div className="no-func-dd">
        <button type="button" className="no-func-trigger" data-testid="funcoes-trigger" onClick={() => setAberto((a) => !a)}>
          <span className="no-func-fx" aria-hidden="true">fx</span>
          <span className="no-func-trigger-txt">{selecionadas.length ? `${selecionadas.length} função(ões)` : "Adicionar funções"}</span>
          <Icon name="chevron" />
        </button>
        {aberto && (
          <div className="no-func-pop nowheel">
            {ctx.bibliotecas.length > 6 && (
              <input className="no-func-busca" placeholder="Buscar função" value={busca} onChange={(e) => setBusca(e.target.value)} autoFocus />
            )}
            <div className="no-func-lista">
              {filt.map((b) => {
                const on = selecionadas.includes(b.id);
                return (
                  <label key={b.id} className={`no-func-item${on ? " on" : ""}`} data-testid={`no-funcao-${b.id}`}>
                    {/* pré-marcada quando o nó usa a MACRO, mas SEM travar: dá p/ desmarcar */}
                    <input type="checkbox" checked={on} onChange={() => ctx.toggleBiblioteca(id, b.id)} />
                    <span className="no-func-nome">{b.nome}</span>
                    {auto.has(b.id) && <span className="no-func-auto">auto</span>}
                  </label>
                );
              })}
              {!filt.length && <div className="no-func-vazio">nenhuma função</div>}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// Preview do nó numa JANELA FLUTUANTE (fora do card, via portal — igual à code area). O card é
// estreito demais p/ ver a grade; aqui dá pra redimensionar/rolar à vontade. Disparada pelo play.
function PreviewJanela({ id, pos, onFechar }: { id: string; pos: { left: number; top: number }; onFechar: () => void }) {
  const ctx = useEditorNo();
  const res = ctx.previews[id] as api.PreviewResultado | undefined;
  const erro = ctx.errosPreview[id];
  const carregando = ctx.carregando[id];
  // Nó de efeito (008): o preview traz `efeito` e NÃO executa nada. Mostramos o SQL gerado (escrita)
  // ou os payloads (http, um por linha) — o snapshot simulado ainda aparece na grade abaixo.
  const efeito = res?.efeito;
  // `email`: mostramos o corpo COMO FONTE (não renderizado). Injetar HTML de dado dentro do Studio
  // seria abrir uma superfície de execução na nossa própria tela para agradar a prévia.
  const conteudoEfeito = efeito
    ? efeito.sql_gerado != null && efeito.sql_gerado !== ""
      ? efeito.sql_gerado
      : efeito.tipo === "email"
        ? (efeito.corpo ?? "")
        : Array.isArray(efeito.payloads)
          ? efeito.payloads.map((p) => (typeof p === "string" ? p : JSON.stringify(p, null, 2))).join("\n")
          : null
    : null;
  // `pos` só é setado UMA VEZ (pelo pai, antes de abrir) — semeia a geometria e ignora mudanças
  // posteriores da prop, igual ao CodeArea.
  const { geo, iniciarResize } = useJanelaGeometria({ left: pos.left, top: pos.top, largura: 560, altura: 340 });
  const selo = ctx.previewSelo[id];
  const desatualizado = !!res && !!selo && selo.assinatura !== ctx.assinaturaAtual(id);
  const perfis: Record<string, PerfilColuna> = useMemo(
    () => (res ? perfisDaAmostra(res.colunas, res.linhas as Record<string, unknown>[]) : {}),
    [res],
  );
  const colsNum = useMemo(
    // Coluna numérica alinha à direita com dígitos tabulares; identificador (id/ano/cpf) NÃO é
    // número de grandeza e fica alinhado como texto.
    () => new Set(Object.entries(perfis).filter(([, pf]) => pf.tipo === "numero").map(([c]) => c)),
    [perfis],
  );
  return createPortal(
    <div
      className="code-janela prev-janela nodrag nowheel"
      style={{ left: geo.left, top: geo.top, width: geo.largura, height: geo.altura }}
      onClick={(e) => e.stopPropagation()}
      onPointerDown={(e) => e.stopPropagation()}
    >
      <AlcasRedimensionar iniciar={iniciarResize} />
      <div className="code-janela-cab">
        <span className="code-janela-tit">Preview · {id}</span>
        <button type="button" className="code-janela-x" data-testid="fechar-preview" title="Fechar" onClick={onFechar}>
          <svg viewBox="0 0 20 20" width="1em" height="1em" aria-hidden="true">
            <path d="M6 6l8 8M14 6l-8 8" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
          </svg>
        </button>
      </div>
      <div className="code-janela-corpo prev-corpo">
        {carregando && <p className="prev-estado">Pré-visualizando…</p>}
        {erro && (
          <p data-testid="preview-erro" className="no-preview-erro">
            {erro}
          </p>
        )}
        {efeito && (
          <div
            data-testid="preview-dryrun"
            style={{
              margin: 10,
              padding: "8px 10px",
              border: "1px solid var(--hair)",
              borderRadius: 7,
              background: "var(--soft)",
            }}
          >
            <div style={{ fontWeight: 700, fontSize: 12, marginBottom: efeito.destino || conteudoEfeito ? 6 : 0 }}>
              Dry-run — nenhum efeito foi executado
            </div>
            {efeito.destino && (
              <div style={{ fontSize: 11, marginBottom: conteudoEfeito ? 6 : 0, color: "var(--ink-2)" }}>
                Destino: <code>{efeito.destino}</code>
                {efeito.tipo === "email" && (efeito.contagem_alvo ?? 0) > 1 && (
                  <> · {efeito.contagem_alvo} e-mails</>
                )}
              </div>
            )}
            {efeito.tipo === "email" && (
              <div data-testid="preview-dryrun-email" style={{ fontSize: 11, marginBottom: 6, color: "var(--ink-2)" }}>
                {efeito.assunto != null && (
                  <div>
                    Assunto: <b>{efeito.assunto}</b>
                  </div>
                )}
                {!!efeito.anexos?.length && (
                  <div>
                    Anexos:{" "}
                    {efeito.anexos.map((a) => `${a.nome} (${fmtBytes(a.bytes)})`).join(", ")}
                  </div>
                )}
                {!!efeito.nao_resolvidos?.length && (
                  <div data-testid="preview-dryrun-nao-resolvidos" className="no-preview-erro" style={{ margin: "4px 0 0" }}>
                    Não resolvido no texto: {efeito.nao_resolvidos.map((n) => `{{${n}}}`).join(", ")} — sairia
                    em branco.
                  </div>
                )}
              </div>
            )}
            {conteudoEfeito != null && conteudoEfeito !== "" && (
              <pre
                data-testid="preview-dryrun-conteudo"
                style={{
                  margin: 0,
                  padding: 8,
                  background: "var(--surface)",
                  border: "1px solid var(--hair)",
                  borderRadius: 6,
                  fontSize: 11,
                  overflow: "auto",
                  maxHeight: 220,
                  whiteSpace: "pre-wrap",
                }}
              >
                {conteudoEfeito}
              </pre>
            )}
          </div>
        )}
        {res && (
          <>
            {desatualizado && (
              <p className="prev-desatual" role="status" data-testid="preview-desatualizado">
                Resultado de {selo ? fmtHora(new Date(selo.em).toISOString()) : "antes"} — a definição
                do nó mudou depois disso. Rode o preview de novo.
              </p>
            )}
            <div className={"no-preview-grade nowheel" + (desatualizado ? " esmaecida" : "")}>
              <table data-testid="preview-grade">
                <thead>
                  <tr>{res.colunas.map((c) => <th scope="col" key={c} className={colsNum.has(c) ? "num" : undefined}>{c}</th>)}</tr>
                </thead>
                <tbody>
                  {res.linhas.map((l, i) => (
                    <tr key={i}>
                      {res.colunas.map((c) => {
                        // `String(v)` transformava null em "null", perdia precisão de DECIMAL (que vem
                        // como texto) e punha separador de milhar em id/ano. `celula` é a mesma
                        // formatação honesta usada na tela de execução.
                        const cel = celula((l as Record<string, unknown>)[c], perfis[c]);
                        return (
                          <td key={c} className={(colsNum.has(c) ? "num" : "") + (cel.vazio ? " vazio" : "")}>
                            {cel.vazio ?? cel.texto}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
        {!carregando && !res && !erro && <p className="prev-estado">Sem resultado — rode o preview.</p>}
      </div>
      {res && (
        <div className="code-janela-rodape">
          {/* Provenance: a prévia roda sobre AMOSTRA das fontes, num DuckDB de sessão. Dizer só
              "N linha(s)" fazia a contagem parecer o total do dado real. */}
          {res.contagem} linha(s) na amostra · calculado sobre amostra das fontes
          {selo ? ` · ${fmtHora(new Date(selo.em).toISOString())}` : ""}
        </div>
      )}
    </div>,
    document.body,
  );
}

// Campo de descrição cresce com o conteúdo (teto de 6 linhas): com `rows={2}` fixo, uma descrição
// de três linhas ficava rolando dentro de um campo de 34px de altura.
function ajustarAltura(el: HTMLTextAreaElement) {
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 118) + "px";
}

// Nó custom do editor de pipeline — forma estilo Langflow em cores cFlow. Card sempre aberto:
// descrição + editor (code area colapsável) + parâmetros usados + funções + preview. Portas são
// pontos (cordas manuais). Preview disparado pelo ícone de play no header.
function NoPipelineBase({ id, data }: NodeProps) {
  const ctx = useEditorNo();
  const cat = categoriaDe(data);
  const m = META[cat];
  const nEntradas = m.entradas;
  const erroExec = data?._erroExec as string | undefined; // falha da última execução neste nó
  const erro = !!data?._desconectado || !!erroExec;
  const sel = ctx.selId === id;
  // Todos os tipos têm editor/preview próprios (saída inclusive: form de publicação; preview passa
  // a entrada adiante sem publicar — ver preview.py).
  const editavel = true;
  const carregando = ctx.carregando[id];
  const nome = data?.rotulo ?? `${m.rotulo} ${id}`;
  const [editandoNome, setEditandoNome] = useState(false);
  const [prevAberto, setPrevAberto] = useState(false);
  const [prevPos, setPrevPos] = useState<{ left: number; top: number }>({ left: 0, top: 0 });
  const HEAD = 46;

  const abrirPreview = (e: MouseEvent) => {
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const W = 560;
    const left = Math.min(Math.max(12, r.right + 16), window.innerWidth - W - 12);
    const top = Math.min(Math.max(12, r.top - 10), window.innerHeight - 360);
    setPrevPos({ left, top });
    setPrevAberto(true);
    ctx.preverNo(id);
  };

  return (
    <div className={`nopipe nopipe--${m.classe}${sel ? " is-sel" : ""}${erro ? " is-erro" : ""}`}>
      <div className="nopipe-head">
        <span className="nopipe-ic">
          <Icon name={m.ic} />
        </span>
        {editandoNome ? (
          <input
            data-testid="no-nome"
            aria-label="Nome do nó"
          className="nopipe-nome nodrag"
            value={nome}
            autoFocus
            onClick={(e) => e.stopPropagation()}
            onChange={(e) => ctx.patchRotulo(id, e.target.value)}
            onBlur={() => setEditandoNome(false)}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === "Escape") {
                e.preventDefault();
                // Não deixa o Escape subir: aqui ele encerra a RENOMEAÇÃO; no canvas ele recolhe o
                // card. Sem isto, um Escape fazia as duas coisas de uma vez.
                e.stopPropagation();
                (e.target as HTMLInputElement).blur();
              }
            }}
          />
        ) : (
          <span
            data-testid="no-nome"
            className="nopipe-nome nopipe-nome--ro"
            title="Dê dois cliques para renomear (arraste para mover)"
            onDoubleClick={() => setEditandoNome(true)}
          >
            {nome}
          </span>
        )}
        <div className="nopipe-acoes nodrag">
          {/* Sair do modo expandido precisa de uma saída ÓBVIA: antes só dava clicando em outro nó
              ou no vazio do canvas — nada na tela dizia isso. */}
          {ctx.compacto && sel && (
            <button
              data-testid={`no-recolher-${id}`}
              className="nopipe-btn"
              aria-label={`Recolher o nó ${data?.rotulo ?? id}`}
              title="Recolher (Esc)"
              onClick={(e) => {
                // `stopImmediatePropagation` no evento NATIVO: o React Flow escuta o clique no
                // próprio nó, e só `stopPropagation` do React deixava o clique alternar duas vezes
                // (recolhia e reabria no mesmo gesto).
                e.stopPropagation();
                e.nativeEvent.stopImmediatePropagation();
                ctx.recolher();
              }}
            >
              <svg viewBox="0 0 20 20" width="1em" height="1em" aria-hidden="true">
                <path
                  d="M6 12l4-4 4 4"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.8"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </button>
          )}
          {editavel && (
            <button
              data-testid="btn-preview"
              className="nopipe-btn nopipe-play"
              aria-label={`Pré-visualizar o nó ${data?.rotulo ?? id}`}
            title="Preview deste nó"
              disabled={carregando}
              onClick={(e) => {
                e.stopPropagation();
                abrirPreview(e);
              }}
            >
              <Icon name="run" />
            </button>
          )}
          <button
            data-testid="no-excluir"
            className="nopipe-btn nopipe-x"
            aria-label={`Excluir o nó ${data?.rotulo ?? id}`}
            title="Excluir nó"
            onClick={(e) => {
              e.stopPropagation();
              ctx.pedirExcluir(id);
            }}
          >
            <svg viewBox="0 0 20 20" width="1em" height="1em" aria-hidden="true">
              <path d="M6 6l8 8M14 6l-8 8" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
            </svg>
          </button>
        </div>
      </div>

      {erroExec && (
        <div className="nopipe-falha" data-testid="no-erro-exec" title={erroExec}>
          <span className="nopipe-falha-ic" aria-hidden="true">⚠</span>
          <span className="nopipe-falha-txt">{erroExec}</span>
        </div>
      )}

      {/* Card RECOLHIDO quando não é o nó em edição e o grafo é grande: 15 editores abertos ao
          mesmo tempo não cabem legíveis em tela nenhuma — era a raiz de o editor abrir num recorte
          que não informa. Recolhido, o card mostra o que identifica (nome, tipo, resumo);
          selecionar reabre. Em grafo pequeno nada muda. */}
      {ctx.compacto && !sel ? (
        <button
          type="button"
          className="nopipe-expandir"
          data-testid={`no-expandir-${id}`}
          onClick={(e) => {
            e.stopPropagation();
            ctx.selecionar(id);
          }}
        >
          {data?.descricao?.trim() ? data.descricao : "abrir para editar"}
        </button>
      ) : (
      <div className="nopipe-body nowheel">
        <div className="nodrag" onClick={(e) => e.stopPropagation()}>
          <textarea
            data-testid="no-descricao"
            className="no-descricao"
            value={data?.descricao ?? ""}
            placeholder="Descreva o que este nó faz…"
            rows={2}
            ref={(el) => {
              if (el) ajustarAltura(el);
            }}
            onChange={(e) => {
              ajustarAltura(e.currentTarget);
              ctx.patchDescricao(id, e.target.value);
            }}
          />
          <Editor id={id} data={data} cat={cat} />
          <ParamsNo data={data} />
          <FuncoesNo id={id} data={data} />
        </div>
      </div>
      )}

      {prevAberto && <PreviewJanela id={id} pos={prevPos} onFechar={() => setPrevAberto(false)} />}

      {/* Portas (cordas manuais): pontos na borda do card, sem rótulo. */}
      {Array.from({ length: nEntradas }).map((_, i) => (
        <Handle
          key={`in-${i}`}
          type="target"
          position={Position.Left}
          id={nEntradas > 1 ? `in-${i}` : undefined}
          className="nopipe-port nopipe-port--in"
          style={{ top: HEAD + 14 + i * 22 }}
        />
      ))}
      {m.saidas > 0 && (
        <Handle type="source" position={Position.Right} className="nopipe-port nopipe-port--out" style={{ top: HEAD + 14 }} />
      )}
    </div>
  );
}

export default memo(NoPipelineBase);
