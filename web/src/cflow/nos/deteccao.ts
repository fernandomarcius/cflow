import type { ParametroPipeline } from "../../api/client";

// Detecção (não hardcoded) a partir do SQL/condição de um nó.

// Textos onde o usuário escreve SQL/condição num nó (fonte.config.sql, sql, guiada.op.condicao).
export function textosDoNo(data: any): string[] {
  return [data?.sql, data?.config?.sql, data?.config?.op?.condicao].filter((s): s is string => typeof s === "string");
}

// Parâmetros `:nome` referenciados pelo nó (mesma regra do backend: ignora `::cast`).
export function refsDoNo(data: any): string[] {
  const re = /(?<!:):([a-zA-Z_][a-zA-Z0-9_]*)/g;
  const out: string[] = [];
  for (const t of textosDoNo(data)) {
    let m: RegExpExecArray | null;
    while ((m = re.exec(t)) !== null) if (!out.includes(m[1])) out.push(m[1]);
  }
  return out;
}

// Extrai os tokens `:nome` de um SQL. O lookbehind `(?<!:)` impede casar quando o
// `:` é precedido por outro `:` (cast do PostgreSQL, ex.: `valor::numeric`).
// COMENTÁRIOS são removidos antes — `-- ...`, `/* ... */` e strings com `HH:MM:SS`
// não são parâmetros (evita falsos `:MM`/`:SS`/`:dr` vindos de comentário/exemplo).
export function extrairRefs(sql: string): string[] {
  const semComentarios = sql
    .replace(/\/\*[\s\S]*?\*\//g, " ") // bloco /* ... */
    .replace(/--[^\n]*/g, " "); // linha -- ...
  const re = /(?<!:):([a-zA-Z_][a-zA-Z0-9_]*)/g;
  const achados: string[] = [];
  let m: RegExpExecArray | null;
  while ((m = re.exec(semComentarios)) !== null) achados.push(m[1]);
  return achados;
}

// Casa uma referência `:ref` com algum param declarado, tolerando a convenção de
// competência (`X_inicio`/`X_fim` cobertos pelo param `X`).
export function refCobertaPor(ref: string, nomesDeclarados: Set<string>): boolean {
  if (nomesDeclarados.has(ref)) return true;
  const m = /^(.+)_(inicio|fim)$/.exec(ref);
  return m != null && nomesDeclarados.has(m[1]);
}

// Varre o SQL/condição de todos os nós do grafo e autodeclara qualquer `:ref` sem
// parâmetro correspondente — stub mínimo (`tipo: "texto"`), que o especialista completa
// depois na aba Parâmetros. `criados` fica vazio quando nada precisa ser adicionado.
export function parametrosAutomaticos(
  nos: { sql?: unknown; config?: any }[],
  atuais: ParametroPipeline[],
): { criados: ParametroPipeline[]; todos: ParametroPipeline[] } {
  const nomesDeclarados = new Set(
    (atuais ?? [])
      .map((p) => (typeof p?.nome === "string" ? p.nome.trim() : ""))
      .filter((n) => n !== ""),
  );
  const refs = new Set<string>();
  for (const no of nos ?? []) {
    for (const ref of extrairRefs(textosDoNo(no).join("\n"))) refs.add(ref);
  }
  const criados: ParametroPipeline[] = [...refs]
    .filter((ref) => !refCobertaPor(ref, nomesDeclarados))
    .sort()
    .map((nome) => ({ nome, tipo: "texto", obrigatorio: false }));
  return { criados, todos: [...(atuais ?? []), ...criados] };
}

// Identificadores "chamados" no SQL do nó (palavras seguidas de `(`), p/ casar com nomes de MACRO.
function chamadasDoNo(data: any): Set<string> {
  const re = /([a-zA-Z_][a-zA-Z0-9_]*)\s*\(/g;
  const out = new Set<string>();
  for (const t of textosDoNo(data)) {
    let m: RegExpExecArray | null;
    while ((m = re.exec(t)) !== null) out.add(m[1].toLowerCase());
  }
  return out;
}

// Bibliotecas cujas MACROs o nó CHAMA — auto-ativação por uso real (não hardcoded). `macroParaBib`
// mapeia nome-de-macro (minúsculo) → id da biblioteca que a define.
export function bibsDetectadas(data: any, macroParaBib: Record<string, string>): string[] {
  const chamadas = chamadasDoNo(data);
  const out: string[] = [];
  for (const nome of chamadas) {
    const bib = macroParaBib[nome];
    if (bib && !out.includes(bib)) out.push(bib);
  }
  return out;
}

// União das bibliotecas EFETIVAS do nó: escolhidas à mão (config.bibliotecas) + detectadas pelo uso.
export function bibsEfetivas(data: any, macroParaBib: Record<string, string>): string[] {
  const manuais: string[] = (data?.config?.bibliotecas ?? []).map(String);
  const auto = bibsDetectadas(data, macroParaBib);
  const out = [...manuais];
  for (const b of auto) if (!out.includes(b)) out.push(b);
  return out;
}
