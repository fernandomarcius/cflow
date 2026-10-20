// Promoção entre ambientes (pacote v2): a lógica da tela, pura e testada sem navegador
// (`npm run test:promocao`). Quem decide o que criar, atualizar ou manter é o SERVIDOR — a tela pede
// `simular` e mostra o plano que ele devolveu; aqui só se lê o arquivo e se resume o plano.

export type AcaoPipeline = "criar" | "atualizar" | "manter" | "manter_diferente";

export interface PlanoPromocao {
  versao_pacote: number;
  atualizar: boolean;
  conexoes: { nome: string; tipo: string; acao: "reusar" | "criar" }[];
  pipelines: {
    nome: string;
    acao: AcaoPipeline;
    diferencas: string[];
    camada?: string | null;
    grupo?: string | null;
    sistema?: boolean;
    versao_origem?: number | null;
  }[];
  pendencias: string[];
  simulado?: boolean;
  conexoes_criadas?: number;
  pipelines_criados?: number;
  pipelines_atualizados?: number;
}

// Rótulo de cada ação como a pessoa lê — o código da API fica fora da tela.
export const ROTULO_ACAO: Record<AcaoPipeline, string> = {
  criar: "criar",
  atualizar: "publicar versão nova",
  manter: "já igual — nada muda",
  manter_diferente: "diferente — só com “atualizar”",
};

// Lê o arquivo escolhido. A validação de verdade é do servidor; aqui só se evita mandar para lá o
// que nem é pacote, com a frase que diz o que fazer.
export function lerPacote(texto: string): { pacote?: Record<string, unknown>; erro?: string } {
  let v: unknown;
  try {
    v = JSON.parse(texto);
  } catch {
    return { erro: "O arquivo não é JSON — escolha o pacote gerado por “Exportar”." };
  }
  if (!v || typeof v !== "object" || Array.isArray(v)) {
    return { erro: "O arquivo não é um pacote de promoção." };
  }
  const p = v as Record<string, unknown>;
  if (p.versao !== 1 && p.versao !== 2) {
    return { erro: `Versão de pacote não reconhecida (${String(p.versao)}): esperado 1 ou 2.` };
  }
  if (!Array.isArray(p.pipelines ?? []) || !Array.isArray(p.conexoes ?? [])) {
    return { erro: "O pacote precisa das listas “pipelines” e “conexoes”." };
  }
  return { pacote: p };
}

export function resumirPlano(p: PlanoPromocao) {
  const conta = (a: AcaoPipeline) => p.pipelines.filter((x) => x.acao === a).length;
  return {
    criar: conta("criar"),
    atualizar: conta("atualizar"),
    manter: conta("manter"),
    diferentes: conta("manter_diferente"),
    conexoesNovas: p.conexoes.filter((c) => c.acao === "criar").length,
    conexoesReusadas: p.conexoes.filter((c) => c.acao === "reusar").length,
    // Importar sem nada a gravar é clique à toa: a tela desabilita o botão.
    temEscrita: conta("criar") + conta("atualizar") + p.conexoes.filter((c) => c.acao === "criar").length > 0,
  };
}

// `cflow-promocao-2026-09-11-1542.json` — hora local de quem baixou, para ordenar na pasta.
export function nomeArquivo(agora: Date): string {
  const d = (n: number) => String(n).padStart(2, "0");
  return `cflow-promocao-${agora.getFullYear()}-${d(agora.getMonth() + 1)}-${d(agora.getDate())}-${d(agora.getHours())}${d(agora.getMinutes())}.json`;
}
