// Abas de FILTRO por tipo na listagem de conexões — lógica PURA, sem React.
//
// Mesmo arranjo de `camadas.ts`: vive fora do componente para rodar sem navegador e sem banco
// (`pnpm run test:tipos-conexao`). Errar aqui não estoura — só mostra a lista errada, que é a
// classe de defeito mais difícil de perceber olhando a tela.
//
// **Duas listas de tipo, de propósito.** `TIPOS` (em `Conexoes.tsx`) é o que se pode CRIAR pelo
// Studio; este módulo descreve o que pode EXISTIR. Não são a mesma coisa: a conexão `objeto` (o
// destino do lake) é criada por `scripts/configurar_lake_gcs.py`, porque a credencial da conta de
// serviço não passa por formulário nem por argumento de linha de comando. Ela precisa aparecer na
// listagem e no filtro — e não pode aparecer no formulário de "Nova conexão".

export interface ConexaoTipada {
  tipo: string;
}

// Rótulo pt-BR por tipo. Tipo desconhecido devolve o valor CRU em vez de "outro": quando o backend
// ganhar um tipo novo, a tela mostra o nome dele em vez de esconder a conexão atrás de um genérico.
const ROTULO: Record<string, string> = {
  sqlserver: "SQL Server",
  postgres: "PostgreSQL",
  api_rest: "API REST",
  api_query: "API de consulta",
  arquivo: "Arquivo",
  smtp: "E-mail (SMTP)",
  objeto: "Objeto (lake)",
};

// Ordem das abas: bancos primeiro (é o grosso do cadastro), depois APIs, arquivo, e por último os
// dois de infraestrutura (SMTP e o bucket do lake), que quase ninguém abre. Tipo fora desta lista
// entra no fim, na ordem em que aparece — a aba existe mesmo para o tipo que este módulo não
// conhece, senão a conexão ficaria só na aba "Todas" sem jeito de isolá-la.
const ORDEM = ["postgres", "sqlserver", "api_rest", "api_query", "arquivo", "smtp", "objeto"];

export const ABA_TODAS = "";

export function rotuloTipo(tipo: string | null | undefined): string {
  const t = (tipo ?? "").trim();
  if (!t) return "sem tipo";
  return ROTULO[t] ?? t;
}

/**
 * Abas a exibir: "Todas" + uma por tipo PRESENTE no cadastro.
 *
 * Só os tipos presentes, e não os sete conhecidos, porque é a regra que esta tela já seguia no
 * seletor que as abas substituem ("o filtro não oferece tipo que não existe na lista"). Sete abas
 * com quatro vazias seria ruído — e diferente da listagem de pipelines, onde as cinco camadas são
 * fixas porque a classificação é um vocabulário fechado que a pessoa precisa enxergar inteiro.
 */
export function abasDeTipo(
  conexoes: ConexaoTipada[],
): { chave: string; rotulo: string; contagem: number }[] {
  const contagem = new Map<string, number>();
  for (const c of conexoes) {
    const t = (c.tipo ?? "").trim();
    contagem.set(t, (contagem.get(t) ?? 0) + 1);
  }
  const conhecidos = ORDEM.filter((t) => contagem.has(t));
  const desconhecidos = [...contagem.keys()].filter((t) => !ORDEM.includes(t)).sort();
  return [
    { chave: ABA_TODAS, rotulo: "Todas", contagem: conexoes.length },
    ...[...conhecidos, ...desconhecidos].map((t) => ({
      chave: t,
      rotulo: rotuloTipo(t),
      contagem: contagem.get(t) ?? 0,
    })),
  ];
}

/**
 * Predicado da aba. É o MESMO que alimenta a contagem exibida — duas contas separadas divergem no
 * dia em que o contrato de uma aba mudar, e uma aba que diz "(2)" e abre vazia destrói a confiança
 * em todas as outras.
 *
 * Aba desconhecida (link antigo, `?t=xyz`) cai em "Todas", não numa lista vazia sem explicação.
 */
export function daAba(c: ConexaoTipada, aba: string, abasValidas: string[]): boolean {
  if (!aba || !abasValidas.includes(aba)) return true;
  return (c.tipo ?? "").trim() === aba;
}

/** Normaliza a aba vinda da querystring: valor estranho vira "Todas". */
export function abaValida(aba: string | null | undefined, abasValidas: string[]): string {
  const a = (aba ?? "").trim();
  return abasValidas.includes(a) ? a : ABA_TODAS;
}
