import type { IconName } from "../ui";

// Categoria visual do nó (deriva de data.tipo + op.tipo). É o discriminador de cor/ícone/rótulo
// do card; NÃO é persistida — o data model salvo continua {tipo, config} (ver StudioGraph.definicao).
export type CategoriaNo =
  | "fonte"
  | "sql"
  | "juntar"
  | "filtrar"
  | "agrupar"
  | "saida"
  | "comparacao"
  | "python"
  | "escrita_sql"
  | "http"
  | "email"
  | "ingestao";

// guiada → op.tipo (juntar/filtrar/agrupar); senão o próprio data.tipo (fonte/sql/...).
export function categoriaDe(data: any): CategoriaNo {
  if (data?.tipo === "guiada") return (data?.config?.op?.tipo ?? "juntar") as CategoriaNo;
  return (data?.tipo ?? "sql") as CategoriaNo;
}

export interface MetaCategoria {
  rotulo: string; // título curto pt-BR no header
  classe: string; // sufixo de classe CSS: .nopipe--<classe>
  ic: IconName; // ícone do header (conjunto de ui.tsx — sem lib externa)
  entradas: number; // nº de handles target (fonte=0; juntar/comparacao=2)
  saidas: number; // nº de handles source (saida=0)
}

// fonte/sql/juntar/filtrar/agrupar existem no editor hoje; saida/comparacao/python e os nós de
// efeito escrita_sql/http ficam reservados (cor/ícone definidos) para extensão futura da UI.
export const META: Record<CategoriaNo, MetaCategoria> = {
  fonte: { rotulo: "Fonte", classe: "fonte", ic: "portal", entradas: 0, saidas: 1 },
  sql: { rotulo: "SQL", classe: "sql", ic: "studio", entradas: 1, saidas: 1 },
  juntar: { rotulo: "Juntar", classe: "juntar", ic: "modelo", entradas: 2, saidas: 1 },
  filtrar: { rotulo: "Filtrar", classe: "filtrar", ic: "search", entradas: 1, saidas: 1 },
  agrupar: { rotulo: "Agrupar", classe: "agrupar", ic: "grid", entradas: 1, saidas: 1 },
  saida: { rotulo: "Saída", classe: "saida", ic: "download", entradas: 1, saidas: 0 },
  comparacao: { rotulo: "Comparação", classe: "comparacao", ic: "layout", entradas: 2, saidas: 1 },
  python: { rotulo: "Python", classe: "python", ic: "runs", entradas: 1, saidas: 1 },
  // Nós de efeito (008): sempre executam (fora do reuso incremental) e exigem conexão com
  // permitir_escrita=true. escrita_sql espelha o estilo do nó de saída (grava dado).
  escrita_sql: { rotulo: "Escrita SQL", classe: "escrita_sql", ic: "download", entradas: 1, saidas: 1 },
  http: { rotulo: "HTTP", classe: "http", ic: "arrow", entradas: 1, saidas: 1 },
  // `email` (009): 1 entrada OPCIONAL (aviso sem dado é caso legítimo) e 1 saída — o resumo do
  // envio é dado consultável (um nó de escrita_sql adiante registra o que saiu, e para quem).
  email: { rotulo: "E-mail", classe: "email", ic: "carta", entradas: 1, saidas: 1 },
  // `ingestao`: traz dado público externo para o lake (bronze). **Zero entradas** — ele
  // busca da ORIGEM, não do nó anterior; uma porta de entrada aqui convidaria a ligar uma aresta
  // cujo dado seria ignorado. A saída existe porque o nó devolve o LOG POR FATIA como dado
  // consultável (é o que a tela de execução exibe, e o que um nó SQL adiante pode filtrar).
  ingestao: { rotulo: "Ingestão", classe: "ingestao", ic: "download", entradas: 0, saidas: 1 },
};

// Tipo de dado que trafega pela porta → cor do chip. O grafo atual só move tabelas; mantido como
// função p/ quando houver portas de parâmetro/valor (color-coding ao estilo Langflow).
export type TipoPorta = "tabela" | "param" | "valor";
export function tipoPortaDaCategoria(_cat: CategoriaNo): TipoPorta {
  return "tabela";
}
