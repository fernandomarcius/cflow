// Formatação compartilhada pelas duas telas de execução (lista e detalhe): momentos em
// America/Sao_Paulo, durações, números pt-BR e o vocabulário de status exposto ao usuário.
//
// O status do banco é `executando | concluida | falha`; o que aparece na tela é
// "em execução | concluída | falha". A classe do pill vem daqui para as duas telas não
// divergirem (antes a lista pintava "executando" de VERDE, como se tivesse terminado bem).

import type { StatusExecucao } from "../../api/client";

const SP = "America/Sao_Paulo";

// Fuso EXPLÍCITO (regra do projeto): o carimbo é evidência, e "17:12" sem fuso não diz nada a
// quem lê o histórico de outra máquina.
export function fmtMomento(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("pt-BR", { timeZone: SP, timeZoneName: "shortOffset" });
  } catch {
    return iso;
  }
}

// Data/hora curta para célula de tabela. O fuso aparece UMA vez, no cabeçalho da coluna (repetir
// "GMT-3" em 25 linhas é ruído); a data completa com fuso fica no `title` da célula.
export function fmtMomentoCurto(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    // O ANO entra quando não é o corrente. Sem isso, um relatório carimbado em 2025 aparecia como
    // "22/06 10:41" ao lado de um de 2026 — num artefato de evidência, a data incompleta é a que
    // faz alguém comparar coisas de anos diferentes achando que são do mesmo período.
    const d = new Date(iso);
    const anoDele = Number(d.toLocaleString("en-CA", { timeZone: SP, year: "numeric" }));
    // O ano de AGORA também sai em America/Sao_Paulo. Com `getFullYear()` (ano local da máquina),
    // um navegador em fuso à frente de SP inverte a regra na virada do ano — 31/12 21:00 em SP já é
    // 01/01 em UTC — e passa a imprimir o ano nos registros do ano corrente, omitindo no do ano
    // anterior: exatamente o contrário do que o comentário acima promete.
    const anoAgora = Number(new Date().toLocaleString("en-CA", { timeZone: SP, year: "numeric" }));
    return d.toLocaleString("pt-BR", {
      timeZone: SP,
      day: "2-digit",
      month: "2-digit",
      ...(anoDele !== anoAgora ? { year: "numeric" as const } : {}),
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

/** Só a hora (HH:MM) — usada nas linhas do dia corrente, onde a data está no separador do dia. */
export function fmtHora(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleTimeString("pt-BR", {
      timeZone: SP,
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

/** Data por extenso em pt-BR a partir de "YYYY-MM-DD" ("1 de julho de 2026") ou de "YYYY-MM"
 *  ("julho de 2026").
 *
 *  Existe por causa do `<input type="date">`: ele renderiza no locale do NAVEGADOR, e o
 *  `lang="pt-BR"` do documento não manda nisso. Num navegador em en-US, 1º de julho aparece como
 *  "07/01/2026" e é lido como 7 de janeiro — na única tela em que errar o período gera relatório
 *  carimbado errado em silêncio. Mês escrito não tem como ser invertido.
 *
 *  Monta a data por COMPONENTES (`new Date(a, m-1, d)`, hora local): `new Date("2026-07-01")` é
 *  meia-noite UTC e voltaria 30/06 em São Paulo — a mesma armadilha documentada em `celula`.
 *  Componente fora da faixa (13/45) faria o Date "rolar" para outro dia: nesse caso devolve o
 *  texto cru em vez de inventar uma data. */
export function fmtDataExtenso(iso: string | null | undefined): string {
  const v = (iso ?? "").trim();
  const m = /^(\d{4})-(\d{2})(?:-(\d{2}))?$/.exec(v);
  if (!m) return v || "—";
  const [ano, mes, dia] = [Number(m[1]), Number(m[2]), m[3] ? Number(m[3]) : 1];
  const d = new Date(ano, mes - 1, dia);
  if (d.getFullYear() !== ano || d.getMonth() !== mes - 1 || d.getDate() !== dia) return v;
  return d.toLocaleDateString("pt-BR", {
    year: "numeric",
    month: "long",
    ...(m[3] ? { day: "numeric" } : {}),
  });
}

/** Dia em São Paulo no formato YYYY-MM-DD — chave para agrupar a lista por dia. */
export function diaSP(iso: string | null | undefined): string | null {
  if (!iso) return null;
  try {
    return new Date(iso).toLocaleDateString("en-CA", { timeZone: SP });
  } catch {
    return null;
  }
}

/** Rótulo do separador de dia: "hoje", "ontem" ou a data. */
export function rotuloDia(diaOuIso: string): string {
  // Aceita a chave de dia (YYYY-MM-DD) OU um ISO completo: quem chamava com o ISO cru via
  // "30T23:15:46.035706+00:00/07/2026" na tela, porque o split("-") abaixo partia o timestamp.
  const dia = diaOuIso.length > 10 ? (diaSP(diaOuIso) ?? diaOuIso.slice(0, 10)) : diaOuIso;
  const hoje = new Date().toLocaleDateString("en-CA", { timeZone: SP });
  const ontem = new Date(Date.now() - 86400000).toLocaleDateString("en-CA", { timeZone: SP });
  if (dia === hoje) return "hoje";
  if (dia === ontem) return "ontem";
  const [a, m, d] = dia.split("-");
  return `${d}/${m}/${a}`;
}

export function fmtDuracaoMs(ms: number | null | undefined): string {
  if (ms == null || !isFinite(ms) || ms < 0) return "—";
  if (ms < 1000) return `${ms} ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toLocaleString("pt-BR", { maximumFractionDigits: 1 })} s`;
  const min = Math.floor(s / 60);
  return `${min} min ${Math.round(s - min * 60)} s`;
}

// Duração entre dois momentos; sem fim (execução em andamento) → "—" ou o rótulo dado.
export function fmtDuracao(
  ini: string | null | undefined,
  fim: string | null | undefined,
  emAndamento = "—",
): string {
  if (!ini) return "—";
  if (!fim) return emAndamento;
  return fmtDuracaoMs(new Date(fim).getTime() - new Date(ini).getTime());
}

// Plural de "linha" — o produto imprimia "1 linhas" em três lugares da mesma tela.
export function linhasTxt(n: number | null | undefined): string {
  if (n == null) return "— linhas";
  return `${num(n)} ${n === 1 ? "linha" : "linhas"}`;
}

export function num(n: number | null | undefined): string {
  return n == null ? "—" : n.toLocaleString("pt-BR");
}

// ── Status da EXECUÇÃO ────────────────────────────────────────────────────────
// `exec` é uma classe própria de pill (azul, pulsando) — não reusa `ok`, que significa terminou bem.
export function pillExecucao(status: StatusExecucao | string): { cls: string; label: string } {
  if (status === "falha") return { cls: "falha", label: "falha" };
  if (status === "executando") return { cls: "exec", label: "em execução" };
  if (status === "concluida") return { cls: "ok", label: "concluída" };
  // Status DESCONHECIDO não nasce verde. O default era "concluída": um estado novo do backend
  // apareceria pintado como "terminou bem" — exatamente o erro que a regra de frontend registra
  // como já cometido uma vez nesta lista. Num produto auditável, o desfecho bom nunca é o palpite.
  return { cls: "skip", label: String(status || "estado desconhecido") };
}

// ── Estado de um NÓ dentro da execução ────────────────────────────────────────
// `executando` = está rodando AGORA; `pendente` = a execução ainda está rodando e este nó não
// chegou a rodar; `skip` = a execução terminou sem ele (o pipeline parou antes).
//
// `executando` e `pendente` eram um estado só, e os dois liam "aguardando": numa execução de 40
// minutos, a pergunta que a tela existe para responder — "em que passo está?" — não tinha
// resposta. São cores diferentes de propósito: azul pulsando é onde o trabalho está acontecendo,
// cinza é fila.
export type EstadoNo = "ok" | "reap" | "falha" | "skip" | "executando" | "pendente" | "indefinido";

export function pillNo(estado: EstadoNo): { cls: string; label: string } {
  switch (estado) {
    case "falha":
      return { cls: "falha", label: "falha" };
    case "skip":
      return { cls: "skip", label: "não executado" };
    case "executando":
      return { cls: "exec", label: "em execução" };
    case "pendente":
      return { cls: "off", label: "pendente" };
    case "reap":
      return { cls: "ok", label: "reaproveitado" };
    case "ok":
      return { cls: "ok", label: "concluído" };
    default:
      // Um nó que ficou marcado como "executando" numa execução já encerrada é um efeito externo
      // cujo desfecho NINGUÉM conhece (o worker pode ter morrido depois de disparar a escrita).
      // Pintar de verde e contar como ok é a tela afirmando um sucesso que não houve.
      return { cls: "skip", label: "desfecho desconhecido" };
  }
}

/** Há quanto tempo o nó está rodando (ms), ou `null` se ele não está em andamento.
 *
 * O relógio de referência é o do navegador contra o `iniciado_em` do servidor: com relógios
 * defasados a conta pode dar negativo, e "há -3 s" é pior do que não mostrar nada — por isso o
 * piso em zero em vez de exibir o número cru.
 */
export function decorridoNo(
  n: { estado: EstadoNo; iniciado_em?: string | null },
  agora: number = Date.now(),
): number | null {
  if (n.estado !== "executando" || !n.iniciado_em) return null;
  const t = new Date(n.iniciado_em).getTime();
  if (Number.isNaN(t)) return null;
  return Math.max(0, agora - t);
}

// ── Células da amostra do snapshot ────────────────────────────────────────────
// O dado vem tipado do Parquet, mas chega ao front como JSON. Imprimir com `String(v)` deixava
// "372260" onde o especialista espera "372.260" — quem confere valor financeiro conta casas com o
// olho, e sem separador nem alinhamento à direita a conferência fica insegura.

const ISO_DATA = /^\d{4}-\d{2}-\d{2}$/;
const ISO_DATA_HORA = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/;
// Identificador, ano e documento NÃO levam separador de milhar: 1785442380189 e 2026 não são
// quantidades, e "1.785.442.380.189" atrapalha a comparação com a fonte.
const COLUNA_ID = /(^|_)(id|ids|cod|codigo|ano|num|numero|versao|cnpj|cpf|conta)(_|$)/i;

export type TipoColuna = "numero" | "data" | "bool" | "texto";
export interface PerfilColuna {
  tipo: TipoColuna;
  decimais: number;
  agrupar: boolean;
}

// Como os números chegam (medido na resposta da API):
//   DECIMAL(38,10) → TEXTO ("1234.5600000000") — exato, e a formatação preserva TODOS os dígitos;
//   DOUBLE         → número JSON (0.1+0.2 vira 0.30000000000000004, 17 casas de resíduo binário);
//   inteiro        → número JSON.
// Daí a regra: texto de Decimal nunca é arredondado (é dado exato); float é limitado a `decimais`
// casas na exibição — senão UMA linha com resíduo binário padroniza a coluna inteira em 17 casas —
// e o valor bruto continua acessível no `title` da célula.
const NUMERO_TEXTO = /^-?\d+(\.\d+)?$/;

/** Casas decimais SIGNIFICATIVAS. Em float, o resíduo binário não conta: 0.1+0.2 vale 1 casa
 *  (0,3), não 17 — senão uma linha com resíduo padroniza a coluna inteira em casas inexistentes.
 *  Em texto (Decimal) todo dígito conta, porque ali é dado. */
function casasDecimais(v: unknown): number {
  const texto = typeof v === "number" ? String(Number(v.toFixed(6))) : String(v);
  return (texto.split(".")[1] ?? "").length;
}

/** Formata um número dado como TEXTO, preservando todos os dígitos originais.
 *  Espera notação decimal simples (sem expoente) — `celula` normaliza antes de chamar. */
function formatarNumeroTexto(bruto: string, decimais: number, agrupar: boolean): string {
  const negativo = bruto.startsWith("-");
  // `slice`, não `replace("-","")`: replace tirava o menos do EXPOENTE, e "1e-7" saía como "1e7"
  // — a tela mostrava dez milhões onde o dado era um décimo de milionésimo.
  const semSinal = negativo ? bruto.slice(1) : bruto;
  const [inteiraCrua, decimalCrua = ""] = semSinal.split(".");
  const inteira = agrupar
    ? inteiraCrua.replace(/\B(?=(\d{3})+(?!\d))/g, ".")
    : inteiraCrua;
  const decimal = decimalCrua.padEnd(decimais, "0").slice(0, Math.max(decimais, decimalCrua.length));
  return `${negativo ? "-" : ""}${inteira}${decimal ? "," + decimal : ""}`;
}

/** Perfila uma coluna pela amostra: tipo + nº de decimais a usar em TODA a coluna.
 *
 * Decimais uniformes na coluna são o ponto: com "372.260" numa linha e "372.260,50" na outra, a
 * vírgula não alinha e a comparação visual (que é como se confere valor aqui) se perde. */
export function perfilColuna(valores: unknown[], nome: string): PerfilColuna {
  const v = valores.filter((x) => x !== null && x !== undefined && x !== "");
  if (!v.length) return { tipo: "texto", decimais: 0, agrupar: false };
  const numerico = v.every(
    (x) =>
      (typeof x === "number" && Number.isFinite(x)) ||
      (typeof x === "string" && NUMERO_TEXTO.test(x)),
  );
  if (numerico) {
    // Teto de 6 casas só quando há FLOAT na coluna (acima disso é resíduo binário). Coluna toda em
    // texto é DECIMAL: as casas são a precisão declarada da coluna e devem aparecer inteiras.
    const temFloat = v.some((x) => typeof x === "number");
    const maxCasas = Math.max(...v.map(casasDecimais));
    const decimais = temFloat ? Math.min(6, maxCasas) : maxCasas;
    // Inteiro muito longo (documento, chave de NF) é identificador mesmo sem casar o nome:
    // "12.345.678.000.199" atrapalha a comparação com a fonte.
    const longo = decimais === 0 && v.some((x) => String(x).replace("-", "").length >= 12);
    return {
      tipo: "numero",
      decimais,
      agrupar: !(decimais === 0 && (COLUNA_ID.test(nome) || longo)),
    };
  }
  if (v.every((x) => typeof x === "boolean")) return { tipo: "bool", decimais: 0, agrupar: false };
  if (v.every((x) => typeof x === "string" && (ISO_DATA.test(x) || ISO_DATA_HORA.test(x))))
    return { tipo: "data", decimais: 0, agrupar: false };
  return { tipo: "texto", decimais: 0, agrupar: false };
}

export function perfisDaAmostra(
  colunas: string[],
  amostra: Record<string, unknown>[],
): Record<string, PerfilColuna> {
  const out: Record<string, PerfilColuna> = {};
  for (const c of colunas) out[c] = perfilColuna(amostra.map((l) => l[c]), c);
  return out;
}

/** Uma célula formatada. `vazio` distingue NULL de string vazia — na conferência de dado, "não
 *  veio nada" e "veio vazio" são achados diferentes (e a plataforma trata `""` como sem valor). */
export function celula(
  v: unknown,
  perfil?: PerfilColuna,
): { texto: string; vazio: "nulo" | "texto-vazio" | null; titulo?: string } {
  if (v === null || v === undefined) return { texto: "—", vazio: "nulo", titulo: "nulo" };
  if (v === "") return { texto: "(vazio)", vazio: "texto-vazio", titulo: "texto vazio" };
  // Float em notação exponencial (1e-7, 1e+21) precisa virar decimal ANTES de qualquer
  // manipulação de string — ver o bug do sinal do expoente em `formatarNumeroTexto`.
  if (typeof v === "number" && /e/i.test(String(v))) {
    return { texto: v.toLocaleString("pt-BR", { maximumSignificantDigits: 6 }), vazio: null };
  }
  if (perfil?.tipo === "numero" && (typeof v === "number" || typeof v === "string")) {
    // Float com mais casas que a coluna = resíduo binário: limita na exibição (o `title` da célula
    // mostra o bruto). Texto (Decimal) passa intacto — ali cada dígito é dado.
    // Compara as casas LITERAIS do float (não as significativas) com o perfil da coluna: é assim
    // que o resíduo binário é cortado, mantendo o valor bruto no `title`.
    const casasLiterais = typeof v === "number" ? (String(v).split(".")[1] ?? "").length : 0;
    const bruto =
      typeof v === "number" && casasLiterais > perfil.decimais
        ? v.toFixed(perfil.decimais)
        : String(v);
    return { texto: formatarNumeroTexto(bruto, perfil.decimais, perfil.agrupar), vazio: null };
  }
  if (typeof v === "number") {
    // Coluna de tipo MISTO (número no meio de texto): sem perfil não há como saber se é quantidade
    // ou identificador, então não agrupa — separador errado num id é pior que a falta dele.
    return { texto: formatarNumeroTexto(String(v), 0, false), vazio: null };
  }
  if (typeof v === "boolean") return { texto: v ? "sim" : "não", vazio: null };
  if (perfil?.tipo === "data" && typeof v === "string") {
    // Fatiar o texto, NUNCA `new Date(...)`: "2026-07-30" é meia-noite UTC e voltaria 29/07 em
    // São Paulo — uma data inteira deslocada por causa da formatação.
    const [d, h] = v.split(/[T ]/);
    const [ano, mes, dia] = d.split("-");
    return { texto: h ? `${dia}/${mes}/${ano} ${h.slice(0, 5)}` : `${dia}/${mes}/${ano}`, vazio: null };
  }
  if (typeof v === "object") return { texto: JSON.stringify(v), vazio: null };
  return { texto: String(v), vazio: null };
}

export function rotuloOrigem(
  origem: string | null | undefined,
  agendamentoNome?: string | null,
): string {
  if (origem === "agendado") return agendamentoNome ? `agendada · ${agendamentoNome}` : "agendada";
  return "manual";
}

// O que cada nó de EFEITO faz fora da plataforma. Um rótulo só para o aviso da reexecução e o da
// cadeia no agendamento: escrito duas vezes, os avisos divergiriam no primeiro tipo novo.
const ROTULO_EFEITO: Record<string, string> = {
  email: "envia e-mail",
  http: "chama uma API externa",
  escrita_sql: "grava em banco externo",
  ingestao: "baixa dado da origem pública",
};

export function rotuloEfeito(tipo: string): string {
  return ROTULO_EFEITO[tipo] ?? tipo;
}

// Onde uma saída publica: `gold/vendas`, `Iceberg cri_geral`, `arquivo relatorio`.
export function rotuloPublicacao(p: {
  destino: string;
  alvo: string | null;
  camada: string | null;
}): string {
  if (p.destino === "lake") return `${p.camada ?? "silver"}/${p.alvo ?? ""}`;
  if (p.destino === "iceberg") return `Iceberg ${p.alvo ?? ""}`;
  return `arquivo ${p.alvo ?? ""}`;
}
