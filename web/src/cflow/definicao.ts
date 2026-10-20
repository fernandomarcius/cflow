// Ida e volta da definição do pipeline pelo Studio (o JSON versionado é a fonte da verdade).
//
// O Studio edita um SUBCONJUNTO da definição — nós (id, tipo, rótulo, descrição, SQL, config e
// posição), arestas (de → para) e parâmetros — e o `PUT /pipelines/{id}/rascunho` grava a definição
// como ela chega: SUBSTITUI, não mescla. Enquanto o canvas montava `{nos, arestas, parametros}` do
// zero, toda chave que ele não conhecia sumia no primeiro salvar, sem aviso. A que custa caro é
// `montante_publica: false` (`pipelines/definicao.montante_publica`, no core): apagada, a cadeia
// volta a mover os `_atual.json` que a raiz declarou congelados, e o fechamento sai com número
// plausível e errado — lendo o histórico de outra corrida.
//
// A regra daqui: o que o Studio não edita, ele devolve VERBATIM. Vale no topo da definição, no nó, na
// aresta e no parâmetro — inclusive dentro de `derivacao` e `opcoes_fonte`, que a aba Parâmetros monta
// campo a campo. Lógica pura, testada sem navegador (`npm run test:definicao`).

import type { DerivacaoParametro, OpcoesFonte, ParametroPipeline } from "../api/client";

export type Objeto = Record<string, unknown>;

// Os campos que cada nível da definição tem EDITOR no Studio. Fora destas listas, o campo passa
// intacto: a lista diz o que o Studio controla, não o que a definição pode ter.
export const CHAVES_TOPO_EDITADAS: readonly string[] = ["nos", "arestas", "parametros"];
export const CHAVES_NO_EDITADAS: readonly string[] = [
  "id",
  "tipo",
  "rotulo",
  "descricao",
  "sql",
  "config",
  "posicao",
];
export const CHAVES_ARESTA_EDITADAS: readonly string[] = ["de", "para"];
// A aba Parâmetros monta estes campos um a um e OMITE alguns de propósito (o `default` de um
// parâmetro `segredo`, o metadado em branco, as `opcoes` de quem deixou de ser `opcoes`). Por isso a
// omissão tem de continuar valendo para eles — e só para eles.
export const CHAVES_PARAMETRO_EDITADAS: readonly string[] = [
  "nome",
  "tipo",
  "obrigatorio",
  "segredo",
  "default",
  "rotulo",
  "descricao",
  "ordem",
  "valor_preview",
  "opcoes",
  "opcoes_fonte",
  "derivacao",
];
export const CHAVES_DERIVACAO_EDITADAS: readonly string[] = [
  "de",
  "meses",
  "campo_id",
  "campo_valor",
  "origem",
  "conexao_id",
  "sql",
  "no_id",
  "mapa",
  "substituicoes",
  "caixa_alta",
  "juntar",
];
export const CHAVES_OPCOES_FONTE_EDITADAS: readonly string[] = [
  "origem",
  "conexao_id",
  "no_id",
  "coluna",
  "coluna_valor",
  "coluna_rotulo",
  "sql",
];

function ehObjeto(v: unknown): v is Objeto {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** Cópia de `obj` SEM as chaves que o Studio edita — o que sobra volta intacto no salvar. */
export function extras(obj: unknown, editadas: readonly string[]): Objeto {
  if (!ehObjeto(obj)) return {};
  const fora = new Set(editadas);
  return Object.fromEntries(Object.entries(obj).filter(([k]) => !fora.has(k)));
}

export const extrasDaDefinicao = (def: unknown): Objeto => extras(def, CHAVES_TOPO_EDITADAS);
export const extrasDoNo = (no: unknown): Objeto => extras(no, CHAVES_NO_EDITADAS);
export const extrasDaAresta = (aresta: unknown): Objeto => extras(aresta, CHAVES_ARESTA_EDITADAS);

export interface NoDaDefinicao {
  extras?: Objeto;
  id: string;
  tipo: string;
  rotulo?: string;
  descricao?: string;
  sql?: string;
  config?: any;
  posicao?: { x: number; y: number };
}

export interface ArestaDaDefinicao {
  extras?: Objeto;
  de: string;
  para: string;
}

export interface DefinicaoLida {
  extras: Objeto;
  nos: NoDaDefinicao[];
  arestas: ArestaDaDefinicao[];
  parametros: ParametroPipeline[];
}

export type DefinicaoMontada = Objeto & {
  nos: Objeto[];
  arestas: Objeto[];
  parametros: unknown[];
};

/** A definição como o canvas a consome: os campos editáveis separados do que só passa por ele. */
export function lerDefinicao(def: unknown): DefinicaoLida {
  const d = ehObjeto(def) ? def : {};
  const nos = Array.isArray(d.nos) ? d.nos.filter(ehObjeto) : [];
  const arestas = Array.isArray(d.arestas) ? d.arestas.filter(ehObjeto) : [];
  return {
    extras: extrasDaDefinicao(d),
    nos: nos.map((n) => ({
      extras: extrasDoNo(n),
      id: String(n.id),
      tipo: String(n.tipo),
      rotulo: n.rotulo as string | undefined,
      descricao: n.descricao as string | undefined,
      sql: n.sql as string | undefined,
      config: n.config,
      posicao: n.posicao as { x: number; y: number } | undefined,
    })),
    arestas: arestas.map((a) => ({ extras: extrasDaAresta(a), de: String(a.de), para: String(a.para) })),
    parametros: (Array.isArray(d.parametros) ? d.parametros : []) as ParametroPipeline[],
  };
}

/**
 * A definição que o Studio persiste: o que ele edita POR CIMA do que ele carregou.
 *
 * O campo editado vence o extra homônimo. Por construção eles não colidem (o extra é justamente o que
 * não é campo editado), mas o spread na ordem inversa deixaria um JSON antigo sobrescrever a edição
 * em silêncio no dia em que alguém tirar uma chave das listas acima.
 */
export function montarDefinicao(partes: {
  extras?: Objeto;
  nos: NoDaDefinicao[];
  arestas: ArestaDaDefinicao[];
  parametros: unknown[];
}): DefinicaoMontada {
  return {
    ...(partes.extras ?? {}),
    nos: partes.nos.map(({ extras: ex, ...editado }) => ({ ...(ex ?? {}), ...editado })),
    arestas: partes.arestas.map(({ extras: ex, ...editada }) => ({ ...(ex ?? {}), ...editada })),
    parametros: partes.parametros,
  };
}

/**
 * A definição do servidor com OUTRA lista de parâmetros e nada mais mudado — o salvar da aba
 * Parâmetros, que não edita nós nem arestas.
 */
export function definicaoComParametros(def: unknown, parametros: unknown[]): DefinicaoMontada {
  const base = ehObjeto(def) ? def : {};
  return {
    ...base,
    nos: Array.isArray(base.nos) ? (base.nos as Objeto[]) : [],
    arestas: Array.isArray(base.arestas) ? (base.arestas as Objeto[]) : [],
    parametros,
  };
}

/**
 * O parâmetro como a aba Parâmetros o persiste: campos em branco omitidos, `default`/`valor_preview`
 * de credencial nunca gravados, `default` de `inteiro` como número — e o que a aba não edita, intacto.
 */
export function parametroParaSalvar(p: ParametroPipeline): ParametroPipeline {
  const base: ParametroPipeline = {
    ...(extras(p, CHAVES_PARAMETRO_EDITADAS) as Partial<ParametroPipeline>),
    nome: p.nome.trim(),
    tipo: p.tipo,
    obrigatorio: !!p.obrigatorio,
  };
  // Credencial: o backend redige o valor ao persistir/auditar (e recusa nome de segredo sem esta
  // marca). Só persiste quando marcado, para não poluir a definição.
  if (p.segredo) base.segredo = true;
  // Parâmetro `segredo` NÃO leva valor na definição. `default`/`valor_preview` são persistidos no
  // JSON versionado e entram no diff da trilha — que é append-only e permanente, ou seja,
  // irreversível. A redação existente cobre `execucoes.parametros`, não a definição.
  // Tipo `inteiro` exige NÚMERO no JSON: o backend recusa a definição inteira (422 "Default de 'x'
  // deve ser inteiro") quando o padrão chega como texto — e o campo do painel sempre devolve texto.
  // Se não for inteiro, o texto segue como está e o lint/backend acusam com mensagem própria.
  if (p.default !== undefined && String(p.default) !== "" && !p.segredo)
    base.default =
      p.tipo === "inteiro" && /^-?\d+$/.test(String(p.default).trim())
        ? Number(String(p.default).trim())
        : p.default;
  // Metadados humanos (persistem só quando preenchidos).
  if (p.rotulo && p.rotulo.trim() !== "") base.rotulo = p.rotulo.trim();
  if (p.descricao && p.descricao.trim() !== "") base.descricao = p.descricao.trim();
  if (p.ordem !== undefined && p.ordem !== null && !Number.isNaN(p.ordem)) base.ordem = p.ordem;
  if (p.valor_preview !== undefined && String(p.valor_preview) !== "" && !p.segredo)
    base.valor_preview = p.valor_preview;
  // Fonte de opções (dropdown dinâmico) — QUALQUER tipo de parâmetro pode ter (ex.: entidade_id
  // `inteiro` com dropdown id+apelido). Persiste SEMPRE que houver fonte válida; a lista estática
  // `opcoes` fica só para tipo "opcoes" sem fonte.
  // opcoes_fonte: origem "conexao" {conexao_id, coluna|coluna_valor, coluna_rotulo?, sql?}
  // OU origem "no" {origem:"no", no_id, coluna_valor, coluna_rotulo?}.
  const f = p.opcoes_fonte;
  const colOk = (f?.coluna ?? "").trim() || (f?.coluna_valor ?? "").trim();
  const temFonte = !!(
    f &&
    (f.origem === "no"
      ? (f.no_id ?? "").trim() && colOk
      : f.conexao_id && (colOk || (f.sql ?? "").trim()))
  );
  if (f && temFonte && f.origem === "no") {
    base.opcoes_fonte = {
      ...(extras(f, CHAVES_OPCOES_FONTE_EDITADAS) as Partial<OpcoesFonte>),
      origem: "no",
      conexao_id: "",
      no_id: f.no_id!.trim(),
      ...((f.coluna_valor ?? f.coluna ?? "").trim()
        ? { coluna_valor: (f.coluna_valor ?? f.coluna)!.trim() }
        : {}),
      ...((f.coluna_rotulo ?? "").trim() ? { coluna_rotulo: f.coluna_rotulo!.trim() } : {}),
    };
  } else if (f && temFonte) {
    base.opcoes_fonte = {
      ...(extras(f, CHAVES_OPCOES_FONTE_EDITADAS) as Partial<OpcoesFonte>),
      conexao_id: f.conexao_id,
      ...((f.coluna ?? "").trim() ? { coluna: f.coluna!.trim() } : {}),
      ...((f.coluna_valor ?? "").trim() ? { coluna_valor: f.coluna_valor!.trim() } : {}),
      ...((f.coluna_rotulo ?? "").trim() ? { coluna_rotulo: f.coluna_rotulo!.trim() } : {}),
      ...((f.sql ?? "").trim() ? { sql: f.sql!.trim() } : {}),
    };
  } else if (p.tipo === "opcoes") {
    base.opcoes = (p.opcoes ?? []).filter((o) => o.trim() !== "");
  }
  // Parâmetro derivado: persiste só com os campos preenchidos (omite vazios). O campo do MAPA é
  // específico por origem (só um vai; "arquivo" é o default retrocompatível e omite `origem`).
  // Derivado de calendário não tem mapa: vão `de` e `meses`, e os campos de mapa não —
  // ficariam na definição aceitos em silêncio e sem efeito. Sem `meses` inteiro, nada é inventado:
  // a validação recusa alto.
  if (p.derivacao && p.derivacao.origem === "competencia") {
    const d = p.derivacao;
    const meses = Number(d.meses);
    base.derivacao = {
      ...(extras(d, CHAVES_DERIVACAO_EDITADAS) as Partial<DerivacaoParametro>),
      origem: "competencia",
      de: (d.de ?? "").trim(),
      ...(d.meses !== undefined && Number.isInteger(meses) ? { meses } : {}),
    };
  } else if (p.derivacao) {
    const d = p.derivacao;
    const origem = d.origem ?? "arquivo";
    const der: DerivacaoParametro = {
      // `normalizar: "sistema"` é aceito pelo backend e não tem campo nesta aba: sem o resto
      // preservado ele sumia no primeiro salvar, pelo mesmo caminho do `montante_publica`.
      ...(extras(d, CHAVES_DERIVACAO_EDITADAS) as Partial<DerivacaoParametro>),
      de: (d.de ?? "").trim(),
      campo_id: (d.campo_id ?? "").trim(),
      campo_valor: (d.campo_valor ?? "").trim(),
    };
    if (origem === "fonte") {
      der.origem = "fonte";
      der.conexao_id = (d.conexao_id ?? "").trim();
      if ((d.sql ?? "").trim()) der.sql = d.sql!.trim();
    } else if (origem === "no") {
      der.origem = "no";
      der.no_id = (d.no_id ?? "").trim();
    } else {
      der.mapa = (d.mapa ?? "").trim();
    }
    const subs = (d.substituicoes ?? []).filter((s) => s.de.trim() && s.para.trim());
    if (subs.length) der.substituicoes = subs.map((s) => ({ de: s.de.trim(), para: s.para.trim() }));
    if (d.caixa_alta) der.caixa_alta = true;
    if (d.juntar !== undefined && d.juntar !== "") der.juntar = d.juntar;
    base.derivacao = der;
  }
  return base;
}
