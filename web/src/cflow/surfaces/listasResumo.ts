// Estado das linhas das listas de View e de Cubo a partir do `resumo` que a API devolve NA PRÓPRIA
// listagem (`GET /views?resumo=true`, `GET /cubos?resumo=true` — uma consulta no servidor). Lógica
// pura, testada sem navegador em scripts/teste-listas-resumo.mjs.
//
// Antes cada linha custava `GET /{coleção}/{id}` + `GET /{coleção}/{id}/versoes`, e as colunas de
// dependência mais uma chamada por página (views) ou por view (cubos): medido: 303
// chamadas numa navegação. As regras de leitura são as
// mesmas que a tela aplicava sobre aquelas respostas — só mudou de onde o dado vem.
import type { CuboComResumo, ResumoCubo, ResumoView, ViewComResumo } from "../../api/client";

// Três fases distinguíveis: "lendo…" não pode virar "nenhuma medida", e falha de leitura não pode
// virar zero.
export type Fase<T> = { fase: "carregando" } | { fase: "ok"; dado: T } | { fase: "erro" };

export interface EstadoView {
  cuboId: string;
  agrupa: number; // zona "Agrupar por"
  divide: number; // zona "Dividir em colunas"
  medidas: number; // zona "Somar / medir"
  liberados: number; // `filtros_liberados` — o que o operador pode filtrar no Portal
  publicada: { numero: number; em: string | null } | null;
  rascunhoAdiante: number | null; // versão salva à frente da publicada (o Portal vê a outra)
}

export interface EstadoCubo {
  dims: number;
  meds: number;
  pipelineId: string;
  noId: string;
  publicada: { numero: number; em: string | null } | null;
  rascunhoAdiante: number | null; // nº da versão salva à frente da publicada (o que roda é a outra)
}

function publicadaDe(r: { publicada: { numero: number; criado_em: string | null } | null }) {
  return r.publicada ? { numero: r.publicada.numero, em: r.publicada.criado_em } : null;
}

// Rascunho à frente = a última versão salva passou a publicada (sem publicada, não há "à frente").
function adiante(r: { ultimo: number; publicada: { numero: number } | null }): number | null {
  return r.publicada && r.ultimo > r.publicada.numero ? r.ultimo : null;
}

export function estadoViewDoResumo(r: ResumoView): EstadoView {
  return {
    cuboId: r.cubo_id ?? "",
    agrupa: r.linhas,
    divide: r.colunas,
    medidas: r.valores,
    liberados: r.filtros_liberados,
    publicada: publicadaDe(r),
    rascunhoAdiante: adiante(r),
  };
}

export function estadoCuboDoResumo(r: ResumoCubo): EstadoCubo {
  return {
    dims: r.dimensoes,
    meds: r.medidas,
    pipelineId: r.pipeline_id ?? "",
    noId: r.no_id ?? "",
    publicada: publicadaDe(r),
    rascunhoAdiante: adiante(r),
  };
}

/**
 * Fase de cada linha. Item sem `resumo` (a API ainda não o devolve — front publicado antes dela)
 * vira "não lido" na linha, nunca um zero inventado.
 */
export function estadosDoResumo<R, E>(
  itens: { id: string; resumo?: R }[],
  converter: (r: R) => E,
): Record<string, Fase<E>> {
  return Object.fromEntries(
    itens.map((i) => [i.id, i.resumo ? { fase: "ok", dado: converter(i.resumo) } : { fase: "erro" }]),
  );
}

/**
 * "Usada em N páginas": tudo-ou-nada, como era — numa análise de impacto, contagem parcial faria
 * uma view usada em produção parecer livre para mexer. `ocultas` é o mesmo número em todo item
 * (páginas de sistema do catálogo que este perfil não lê).
 */
export function usoEmPaginas(itens: ViewComResumo[]): Fase<{ mapa: Record<string, number>; ocultas: number }> {
  if (itens.some((i) => !i.resumo)) return { fase: "erro" };
  const mapa: Record<string, number> = {};
  for (const i of itens) if (i.resumo!.paginas > 0) mapa[i.id] = i.resumo!.paginas;
  return { fase: "ok", dado: { mapa, ocultas: itens[0]?.resumo?.paginas_nao_verificadas ?? 0 } };
}

/** "Quem depende": views visíveis que leem cada cubo. Tudo-ou-nada, pelo mesmo motivo. */
export function dependentesDosCubos(itens: CuboComResumo[]): Fase<Record<string, number>> {
  if (itens.some((i) => !i.resumo)) return { fase: "erro" };
  const mapa: Record<string, number> = {};
  for (const i of itens) if (i.resumo!.views > 0) mapa[i.id] = i.resumo!.views;
  return { fase: "ok", dado: mapa };
}
