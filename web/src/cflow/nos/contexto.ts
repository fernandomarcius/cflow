import { createContext, useContext } from "react";

import type {
  Conexao,
  FonteLake,
  ParametroPipeline,
  PipelineResumo,
  PreviewResultado,
} from "../../api/client";

// Contexto do editor de nós: handlers e estado por-nó que o card (NoPipeline) consome para editar
// SQL/config e mostrar o preview DENTRO do próprio nó (estilo Langflow), sem inspetor lateral nem
// seção de preview genérica. Provido pelo StudioGraph, que detém nodes/edges/preview.
export interface EditorNoCtx {
  selId: string | null; // nó "aberto" (expande o card); setado ao adicionar e ao clicar
  conexoes: Conexao[];
  // Pipelines disponíveis como FONTE (fonte tipo pipeline): a saída de outro pipeline
  // vira entrada deste. Exclui o próprio pipeline (evita auto-referência).
  pipelines: PipelineResumo[];
  // Datasets do lake (bronze) disponíveis como fonte — o caminho bronze→silver.
  fontesLake: FonteLake[];
  tema: "light" | "dark";
  extensoesSql: unknown[]; // extensões CodeMirror (sql + autocomplete das etapas anteriores)
  patchConfig: (id: string, patch: Record<string, unknown>) => void; // fonte: conexao/amostra/sql
  patchSql: (id: string, valor: string) => void; // nó SQL
  patchOp: (id: string, patch: Record<string, unknown>) => void; // guiada: condição/chaves/agregações
  patchRotulo: (id: string, valor: string) => void; // nome do nó (editável no header, autosave no rascunho)
  patchDescricao: (id: string, valor: string) => void; // descrição livre do nó (o que acontece ali)
  pedirExcluir: (id: string) => void; // abre o modal de confirmação de exclusão
  // Grafo grande: cards não selecionados abrem recolhidos (15 editores abertos não cabem legíveis).
  compacto: boolean;
  selecionar: (id: string) => void;
  recolher: () => void; // sai do modo expandido (não alterna)
  preverNo: (id: string) => void;
  previews: Record<string, PreviewResultado>;
  // Assinatura (SQL/config) e instante de cada preview: sem isso o resultado antigo continuava
  // na tela depois de editar o SQL, com cara de resultado do que está escrito agora.
  previewSelo: Record<string, { assinatura: string; em: number }>;
  assinaturaAtual: (id: string) => string; // assinatura do nó AGORA (compara com a do selo)
  errosPreview: Record<string, string>;
  carregando: Record<string, boolean>;
  // Parâmetros do pipeline (declarados) — mostrados no nó conforme os `:nome` que ele referencia.
  parametros: ParametroPipeline[];
  setValorPreview: (nome: string, valor: string) => void; // edita o "valor de preview" de um parâmetro
  // Bibliotecas de funções disponíveis + seleção POR NÓ (no.config.bibliotecas).
  bibliotecas: PipelineResumo[];
  toggleBiblioteca: (id: string, bibId: string) => void;
  // Mapa nome-de-macro (minúsculo) → id da biblioteca, p/ auto-ativar a função no nó que a usa.
  macroParaBib: Record<string, string>;
  // Persiste o grafo (nós/arestas/parâmetros) no rascunho — mesma ação do "Salvar" do toolbar,
  // chamável de dentro do code area. Autodeclara `:param` sem declaração antes de persistir;
  // retorna os nomes recém-criados (vazio quando nada precisou ser adicionado).
  salvarGrafo: () => Promise<string[]>;
}

export const EditorNoContext = createContext<EditorNoCtx | null>(null);

export function useEditorNo(): EditorNoCtx {
  const c = useContext(EditorNoContext);
  if (!c) throw new Error("EditorNoContext ausente (NoPipeline fora do StudioGraph).");
  return c;
}
