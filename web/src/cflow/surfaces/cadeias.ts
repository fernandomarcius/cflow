// Acordeão de CORRIDA na lista de execuções — lógica PURA, sem React.
//
// Um pipeline com fonte tipo `pipeline` dispara a cadeia inteira: a execução pedida mais
// uma por dependência. Na lista, isso aparecia como N linhas soltas, sem nada dizendo que vieram do
// mesmo clique nem qual delas foi a pedida. O servidor agora agrupa (`GET /execucoes` devolve
// corridas, não execuções) e aqui fica o que a TELA decide a partir do grupo.
//
// Está num módulo próprio para ser testado sem navegador e sem banco — ver
// `scripts/teste-cadeias.mjs`, mesmo arranjo de `camadas.ts`/`teste-camadas.mjs`.
import type { ExecucaoItem, GrupoExecucoes } from "../../api/client";

// Um grupo é "só uma execução" quando não veio de disparo encadeado. A tela desenha essas linhas
// exatamente como antes: sem seta, sem acordeão, sem resumo. Só a cadeia ganha o controle novo.
export function ehCorridaUnica(g: GrupoExecucoes): boolean {
  return g.filhas.length === 0;
}

// Quais linhas o acordeão mostra ao abrir. Com filtro ativo, só as que CASAM — mostrar as outras
// faria a tela contradizer o filtro que a pessoa acabou de aplicar. Sem filtro, todas casam e a
// distinção some.
export function filhasVisiveis(g: GrupoExecucoes): ExecucaoItem[] {
  return g.filhas.filter((f) => f.casa_filtro);
}

// A linha-mãe é SEMPRE o cabeçalho, mesmo quando ela própria não casa o filtro (filtrar "falha"
// com a raiz concluída). Ela entra como contexto — é o que diz de que corrida a falha veio —, e a
// tela a marca como tal em vez de deixá-la passar por resultado do filtro.
export function raizEhContexto(g: GrupoExecucoes): boolean {
  return !g.raiz.casa_filtro;
}

// Texto do resumo da linha-mãe fechada. Diz o TAMANHO da corrida e, quando há filtro, quantas das
// linhas correspondem — "1 de 7" é o que evita abrir sete acordeões procurando a falha.
export function resumoCorrida(g: GrupoExecucoes): string {
  const deps = g.filhas.length;
  const plural = deps === 1 ? "pipeline" : "pipelines";
  const base = `+${deps} ${plural}`;
  // `casam_filtro` conta o grupo INTEIRO (mãe inclusive). Igual ao total = nenhum filtro recorta
  // este grupo, e anunciar "7 de 7" seria ruído em toda linha da tela sem filtro.
  if (g.casam_filtro >= g.total_no_grupo) return base;
  return `${base} · ${g.casam_filtro} de ${g.total_no_grupo} no filtro`;
}

// Quantas execuções esta página mostra ao todo (mãe + filhas visíveis de cada grupo). A paginação
// conta CORRIDAS; este número é o que o rodapé usa para não deixar os dois se passarem um pelo
// outro ("25 corridas" e "38 execuções" são coisas diferentes, e as duas importam).
export function totalDeExecucoesNaPagina(grupos: GrupoExecucoes[]): number {
  return grupos.reduce((n, g) => n + 1 + filhasVisiveis(g).length, 0);
}

// Chaves de grupo abertas, lidas da querystring (`?ab=a,b,c`). Estado de acordeão vive na URL como
// todo filtro desta tela: link compartilhado abre no mesmo lugar, e o voltar do navegador funciona.
export function abertasDaUrl(v: string | null | undefined): Set<string> {
  if (!v) return new Set();
  return new Set(
    v
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean),
  );
}

// De volta para a querystring. Devolve `null` quando nada está aberto, para o parâmetro sair da URL
// em vez de virar `?ab=` — ruído no link que se copia (mesma regra da aba padrão em `camadas.ts`).
export function abertasParaUrl(abertas: Set<string>): string | null {
  if (abertas.size === 0) return null;
  return [...abertas].join(",");
}

export function alternar(abertas: Set<string>, chave: string): Set<string> {
  const nova = new Set(abertas);
  if (nova.has(chave)) nova.delete(chave);
  else nova.add(chave);
  return nova;
}

// Uma corrida em que ALGUMA linha falhou. Serve para a mãe fechada poder avisar que há falha
// escondida no acordeão — sem repintar a pill dela: verde é "esta terminou bem", e herdar o
// vermelho da filha mentiria sobre o desfecho da própria execução (mesma regra da tela de execução).
export function corridaTemFalha(g: GrupoExecucoes): boolean {
  return g.raiz.status === "falha" || g.filhas.some((f) => f.status === "falha");
}

// Como as DEPENDÊNCIAS da corrida terminaram — a barra colorida ao lado do resumo. Conta TODAS as
// filhas, não só as que casam o filtro: a barra descreve a corrida, e o recorte do filtro já é dito
// em texto pelo `resumoCorrida` ("1 de 7 no filtro"). A mãe fica fora de propósito: o desfecho
// dela é a pill da própria linha, e somá-la aqui contaria a mesma execução duas vezes na tela.
export type ComposicaoCorrida = { concluida: number; executando: number; falha: number };

export function composicaoCorrida(g: GrupoExecucoes): ComposicaoCorrida {
  const c: ComposicaoCorrida = { concluida: 0, executando: 0, falha: 0 };
  for (const f of g.filhas) {
    if (f.status === "concluida" || f.status === "executando" || f.status === "falha") c[f.status]++;
  }
  return c;
}

// Texto da barra (aria-label + title): a cor sozinha não pode ser a única forma de ler a
// composição. Falha primeiro — é o que se vem procurar —, e parte zerada não é anunciada.
export function rotuloComposicao(c: ComposicaoCorrida): string {
  const partes: string[] = [];
  if (c.falha) partes.push(`${c.falha} com falha`);
  if (c.executando) partes.push(`${c.executando} em execução`);
  if (c.concluida) partes.push(`${c.concluida} concluída${c.concluida === 1 ? "" : "s"}`);
  return partes.join(" · ");
}

// ─── Tempo na corrida ───────────────────────────────────────────────────────────────────────────
// O disparo encadeado PRÉ-CRIA todas as execuções da corrida no mesmo instante (é o que dá o mapa
// de frescor antes de qualquer nó rodar). `iniciada_em` é esse
// instante para TODAS, e a dependência que roda por último fica "executando" enquanto espera as
// outras. Medido no dev (2026-09-10): `ref_08_resumo` aparecia com 78 s e os nós dela
// rodaram 2 s — o resto era fila. `concluida_em − iniciada_em` somava a espera ao trabalho.
//
// Por isso a linha tem DUAS medidas, e cada uma responde uma pergunta:
//  - a MÃE mostra a corrida inteira: do disparo ao fim da última execução do grupo;
//  - a FILHA mostra só o que ela rodou: do primeiro nó que começou (`comecou_em`, calculado no
//    servidor) ao fim dela. A espera vai para o `title` — separada, não escondida.

const ms = (iso: string) => new Date(iso).getTime();

export function janelaCorrida(g: GrupoExecucoes): { inicio: string | null; fim: string | null } {
  const todas = [g.raiz, ...g.filhas];
  const inicios = todas.map((e) => e.iniciada_em).filter((v): v is string => !!v);
  const inicio = inicios.length ? inicios.reduce((a, b) => (ms(b) < ms(a) ? b : a)) : null;
  // Qualquer execução ainda rodando = corrida em curso: um "fim" parcial seria um número que muda a
  // cada atualização da tela e parece a duração final.
  if (todas.some((e) => e.status === "executando")) return { inicio, fim: null };
  const fins = todas.map((e) => e.concluida_em).filter((v): v is string => !!v);
  const fim = fins.length ? fins.reduce((a, b) => (ms(b) > ms(a) ? b : a)) : null;
  return { inicio, fim };
}

export type TempoNaCorrida =
  // `fim` nulo = ainda rodando. `esperaMs` = quanto ficou na fila, do disparo ao primeiro nó.
  | { tipo: "rodou"; inicio: string; fim: string | null; esperaMs: number | null }
  // Em execução, e nenhum nó começou: está esperando as dependências.
  | { tipo: "aguardando" }
  // Terminou sem nenhum nó ter começado — a corrida parou antes de chegar nela.
  | { tipo: "sem_nos" };

export function tempoNaCorrida(e: ExecucaoItem): TempoNaCorrida {
  if (e.comecou_em) {
    return {
      tipo: "rodou",
      inicio: e.comecou_em,
      fim: e.concluida_em,
      esperaMs: e.iniciada_em ? Math.max(0, ms(e.comecou_em) - ms(e.iniciada_em)) : null,
    };
  }
  return e.status === "executando" ? { tipo: "aguardando" } : { tipo: "sem_nos" };
}

// TODAS as execuções da página (mãe + filhas de cada corrida), em ordem cronológica decrescente.
//
// É o que consumidores que pensam em EXECUÇÃO — e não em corrida — precisam depois de `GET
// /execucoes` passar a devolver grupos: a última execução de cada pipeline (listagem do Studio) ou
// a última concluída de um pipeline (origem do snapshot no Cubo).
//
// Reordena de propósito: os grupos vêm por `max(iniciada_em)` e as execuções DENTRO de cada um por
// `iniciada_em`, mas duas corridas podem se sobrepor no tempo — achatar sem reordenar deixaria a
// dependência de uma corrida recente na frente da raiz de outra mais antiga, e quem lê "a primeira
// deste pipeline" escolheria a execução errada.
//
// E filtrar por `pipeline_id` continua sendo do CHAMADOR: com `?pipeline_id=`, o servidor devolve
// as corridas que CONTÊM aquele pipeline — e a raiz da corrida pode ser de outro (é justamente o
// caso do DAG: quem chama é o consumidor). Pegar `itens[0].raiz` traria o pipeline errado.
export function execucoesDaPagina(grupos: GrupoExecucoes[]): ExecucaoItem[] {
  return grupos
    .flatMap((g) => [g.raiz, ...g.filhas])
    .sort((a, b) => (b.iniciada_em ?? "").localeCompare(a.iniciada_em ?? ""));
}
