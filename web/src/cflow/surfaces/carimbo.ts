// Lógica pura do carimbo de obsoleto na tela: que datasets um pipeline publica no lake,
// como o escopo digitado vira a partição que a API recebe, e o que dizer de cada estado do
// ponteiro. Sem React e sem rede — testada por `npm run test:carimbo`.
//
// A decisão (pode ou não carimbar, e por quê) é do SERVIDOR (`cflow_core.ingestao.carimbo`): a tela
// só mostra o plano que ele devolve. Validar aqui o que o servidor valida criaria a segunda regra, e
// as duas divergiriam no primeiro ajuste de uma delas.

export interface SaidaLake {
  noId: string;
  rotulo: string;
  dataset: string;
  camada: "silver" | "gold";
  // Chaves do caminho NA ORDEM declarada — o layout que este pipeline publica hoje.
  chaves: string[];
}

// As saídas `lake` da definição, sem repetir (dataset, camada). A camada ausente é `silver`, o
// default do destino; a chave de cada item de partição é a `chave` declarada ou o `nome`
// em minúsculas — a mesma regra do engine, para a tela e o caminho nomearem o mesmo segmento.
export function saidasLake(definicao: unknown): SaidaLake[] {
  const nos = (definicao as { nos?: unknown })?.nos;
  if (!Array.isArray(nos)) return [];
  const saida: SaidaLake[] = [];
  const vistos = new Set<string>();
  for (const no of nos as any[]) {
    const cfg = no?.config ?? {};
    if (no?.tipo !== "saida" || String(cfg.destino ?? "arquivo").toLowerCase() !== "lake") continue;
    if (!cfg.dataset) continue;
    const camada = cfg.camada === "gold" ? "gold" : "silver";
    const chave = `${camada}/${cfg.dataset}`;
    if (vistos.has(chave)) continue;
    vistos.add(chave);
    const itens = Array.isArray(cfg.particao) ? cfg.particao : [];
    saida.push({
      noId: String(no.id),
      rotulo: String(no.rotulo ?? no.id),
      dataset: String(cfg.dataset),
      camada,
      chaves: itens
        .filter((p: unknown) => p && typeof p === "object")
        .map((p: any) => String(p.chave || String(p.nome ?? "").toLowerCase())),
    });
  }
  return saida;
}

// O escopo digitado: `chave=valor`, uma por linha (vírgula também separa, para quem cola
// `entidade_id=11, data_registro=2026-08-06`), NA ORDEM do caminho — o endereço do ponteiro é
// posicional. Só recusa o que nem chegaria a ser um pedido; o resto é o servidor que julga.
export function lerEscopo(texto: string): { itens: string[]; erro: string | null } {
  const partes = texto
    .split(/[\n,]/)
    .map((s) => s.trim())
    .filter(Boolean);
  if (!partes.length)
    return { itens: [], erro: "Informe o escopo: chave=valor, uma por linha (ex.: entidade_id=11)." };
  const itens: string[] = [];
  const vistas = new Set<string>();
  for (const parte of partes) {
    const i = parte.indexOf("=");
    const chave = i > 0 ? parte.slice(0, i).trim() : "";
    const valor = i > 0 ? parte.slice(i + 1).trim() : "";
    if (!chave || !valor) return { itens: [], erro: `"${parte}" não está no formato chave=valor.` };
    if (vistas.has(chave)) return { itens: [], erro: `A chave "${chave}" aparece duas vezes.` };
    vistas.add(chave);
    itens.push(`${chave}=${valor}`);
  }
  return { itens, erro: null };
}

export type EstadoEscopo = "vigente" | "obsoleto" | "ausente";

// Rótulo do estado do PONTEIRO. Pill cinza neutra nos três: estado de ponteiro não é desfecho, e
// verde significa "terminou bem" nas telas de execução (mesma régua da pill de camada).
export function rotuloEstado(estado: EstadoEscopo): string {
  if (estado === "obsoleto") return "carimbado como obsoleto — a leitura é recusada";
  if (estado === "ausente") return "sem ponteiro neste escopo";
  return "vigente — lido pelas fontes que pedem este escopo";
}

// O escopo como o campo da tela o escreve: um `chave=valor` por linha, na ordem do caminho.
export function escopoComoTexto(particao: string[]): string {
  return particao.join("\n");
}

function contar(n: number, um: string, varios: string): string {
  return `${n} ${n === 1 ? um : varios}`;
}

// O que a descoberta de escopos do layout anterior devolveu: uma frase, os escopos que o SERVIDOR
// liberou para carimbar (`pode_carimbar` de cada um — a tela não decide) e os avisos de cobertura.
// Aviso dito é o que separa "não há escopo" de "não deu para ver todos".
export function resumoDescoberta(d: {
  escopos: { particao: string[]; estado: EstadoEscopo; pode_carimbar: boolean }[];
  ausentes: number;
  truncado: boolean;
  layouts_anteriores: { chaves: string[]; execucoes: number; sem_indice: number }[];
}): { carimbaveis: string[][]; texto: string; avisos: string[] } {
  const carimbaveis = d.escopos.filter((e) => e.pode_carimbar).map((e) => e.particao);
  const obsoletos = d.escopos.filter((e) => e.estado === "obsoleto").length;
  const bloqueados = d.escopos.length - carimbaveis.length - obsoletos;
  const execucoes = d.layouts_anteriores.reduce((s, l) => s + l.execucoes, 0);
  let texto: string;
  if (!d.layouts_anteriores.length) {
    texto = "Nenhuma execução gravou este dataset em outro layout: não há escopo de layout anterior.";
  } else if (!d.escopos.length) {
    texto = `Nenhum ponteiro de layout anterior no lake (${contar(execucoes, "execução consultada", "execuções consultadas")}).`;
  } else {
    const partes = [`${carimbaveis.length} a carimbar`];
    if (obsoletos) partes.push(`${obsoletos} já carimbado${obsoletos === 1 ? "" : "s"}`);
    if (bloqueados) partes.push(`${bloqueados} bloqueado${bloqueados === 1 ? "" : "s"}`);
    texto =
      `${contar(d.escopos.length, "escopo", "escopos")} de layout anterior: ${partes.join(", ")} ` +
      `(${contar(execucoes, "execução consultada", "execuções consultadas")}).`;
  }
  const avisos: string[] = [];
  const semIndice = d.layouts_anteriores.reduce((s, l) => s + l.sem_indice, 0);
  if (semIndice)
    avisos.push(
      `${contar(semIndice, "execução concluída", "execuções concluídas")} sem índice no lake: os escopos de coluna ${semIndice === 1 ? "dela" : "delas"} não estão na lista.`,
    );
  if (d.truncado)
    avisos.push("A lista parou no teto de execuções consultadas: pode haver mais escopos.");
  if (d.ausentes)
    avisos.push(
      `${contar(d.ausentes, "escopo", "escopos")} sem ponteiro no lake — nada a carimbar ${d.ausentes === 1 ? "nele" : "neles"}.`,
    );
  return { carimbaveis, texto, avisos };
}

// Que ações o plano libera. A tela não decide: repete `pode_carimbar`/`pode_descarimbar` do
// servidor, e a razão de não poder é a `mensagem` do bloqueio que ele devolveu.
export function acoesDoPlano(plano: {
  estado: EstadoEscopo;
  pode_carimbar: boolean;
  pode_descarimbar: boolean;
  bloqueio: { codigo: string; mensagem: string } | null;
}): { carimbar: boolean; descarimbar: boolean; motivo: string | null } {
  return {
    carimbar: plano.pode_carimbar,
    descarimbar: plano.pode_descarimbar,
    motivo:
      plano.pode_carimbar || plano.pode_descarimbar
        ? null
        : (plano.bloqueio?.mensagem ?? rotuloEstado(plano.estado)),
  };
}
