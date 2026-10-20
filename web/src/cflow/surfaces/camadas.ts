// Filter TABS of the pipeline list and of the Executions screen: PURE logic, no React.
//
// The tabs are filters over the SAME response of `GET /pipelines` (the server already returns
// `camada` and `sistema` for each item), so switching tabs does not call the API.
//
// `camada`, `grupo` and `sistema` are ORTHOGONAL: `camada` CLASSIFIES (bronze/silver/gold),
// `sistema` says WHO sees the item. That is why the filters OVERLAP on purpose: a pipeline flagged
// as `sistema` and classified as `silver` shows up in System AND in Silver. The only tab that
// excludes is "Geral" (General), which is exactly "none of that": no layer, no group, no system flag.
//
// Lives in its own module (not inside `Pipeline.tsx`) so it can be tested without a browser.
import type { Camada, Grupo } from "../../api/client";

export type AbaCamada = "geral" | "sistema" | "bronze" | "silver" | "gold";

// EXACT order in which the tabs appear on screen.
export const ABAS_CAMADA: { chave: AbaCamada; rotulo: string }[] = [
  { chave: "geral", rotulo: "Geral" },
  { chave: "sistema", rotulo: "Sistema" },
  { chave: "bronze", rotulo: "Bronze" },
  { chave: "silver", rotulo: "Silver" },
  { chave: "gold", rotulo: "Gold" },
];

export const ABA_PADRAO: AbaCamada = "geral";

// The minimum the filter needs to know about a pipeline. Structural instead of `api.PipelineResumo`
// so the module does not depend on the API client just to check three fields.
export interface ItemFiltravel {
  camada?: Camada | null;
  grupo?: Grupo | null;
  sistema?: boolean;
}

// A strange value in the query string (old link, typo) falls back to the default tab, not to an
// unexplained empty list, which is what `?aba=bronzee` would produce if the filter trusted the URL.
export function abaValida(v: string | null | undefined): AbaCamada {
  return ABAS_CAMADA.some((a) => a.chave === v) ? (v as AbaCamada) : ABA_PADRAO;
}

// The contract, literally:
//   Geral     = camada IS NULL AND grupo IS NULL AND sistema = false
//   Sistema   = sistema = true
//   rest      = camada = tab
//
// `grupo` is part of Geral because Geral literally means "none of that".
export function naAba(p: ItemFiltravel, aba: AbaCamada): boolean {
  if (aba === "geral") return p.camada == null && p.grupo == null && p.sistema !== true;
  if (aba === "sistema") return p.sistema === true;
  return p.camada === aba;
}

// Count per tab, always over the WHOLE list. Uses the SAME predicate that filters: two separate
// computations would diverge the day the contract of a tab changed.
export function contarPorAba<T extends ItemFiltravel>(itens: T[]): Record<AbaCamada, number> {
  const m = {} as Record<AbaCamada, number>;
  for (const a of ABAS_CAMADA) m[a.chave] = itens.filter((p) => naAba(p, a.chave)).length;
  return m;
}

// The key of `GET /execucoes?aba=` for the tab.
export function abaDaApi(aba: AbaCamada | ""): string | null {
  return aba || null;
}
