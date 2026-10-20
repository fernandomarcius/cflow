// Rótulos do usuário logado que a CASCA do app mostra na sidebar (avatar e perfil) e a tela de
// Conta repete. Moram aqui, e não em `surfaces/Conta.tsx`, porque a casca fica no bundle principal
// e as telas são carregadas sob demanda: importar dois helpers de Conta puxaria a tela inteira de
// volta para o carregamento inicial.

const PERFIL_ROTULO: Record<string, string> = {
  admin: "Administrador",
  especialista: "Especialista",
  operador: "Operador",
  auditor: "Auditor",
};

// Iniciais para o avatar (primeiro + último nome). Reusado na sidebar.
export function iniciais(nome: string): string {
  const p = (nome ?? "").trim().split(/\s+/).filter(Boolean);
  const a = p[0]?.[0] ?? "";
  const b = p.length > 1 ? p[p.length - 1][0] : "";
  return (a + b).toUpperCase() || "?";
}

export function rotuloPerfil(perfil: string): string {
  return PERFIL_ROTULO[perfil] ?? perfil;
}
