// Espelho de core/src/cflow_core/periodos.py — resolve um token de período na janela meio-aberta
// [data_inicio, data_fim) (data_fim EXCLUSIVO), relativo a "hoje" em America/Sao_Paulo.
// Mantenha em sincronia com o backend (há testes de paridade). Semana = domingo→sábado (BR).
// A aritmética de datas usa Date em UTC-meia-noite para não sofrer drift de fuso do browser.

// Tokens relativos + rótulos pt-BR para o seletor do operador (ordem do menu).
export const PERIODOS_RELATIVOS: { token: string; rotulo: string }[] = [
  { token: "mes_atual", rotulo: "Mês atual" },
  { token: "mes_anterior", rotulo: "Mês anterior" },
  { token: "semana_atual", rotulo: "Esta semana" },
  { token: "semana_anterior", rotulo: "Semana passada" },
  { token: "hoje", rotulo: "Hoje" },
  { token: "ontem", rotulo: "Ontem" },
  { token: "ultimos_7_dias", rotulo: "Últimos 7 dias" },
  { token: "ultimos_30_dias", rotulo: "Últimos 30 dias" },
];

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

function isoUTC(d: Date): string {
  return `${d.getUTCFullYear()}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())}`;
}

// "Hoje" em America/Sao_Paulo como Date em UTC-meia-noite (independe do fuso do browser).
export function hojeSaoPaulo(): Date {
  const s = new Date().toLocaleDateString("en-CA", { timeZone: "America/Sao_Paulo" }); // YYYY-MM-DD
  const [a, m, d] = s.split("-").map(Number);
  return new Date(Date.UTC(a, m - 1, d));
}

function addDias(d: Date, n: number): Date {
  return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate() + n));
}
function primeiroDoMes(d: Date): Date {
  return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1));
}
function proximoMes(d: Date): Date {
  return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 1));
}
// Domingo que inicia a semana (domingo→sábado). getUTCDay(): dom=0..sáb=6.
function domingoDaSemana(d: Date): Date {
  return addDias(d, -d.getUTCDay());
}

// Resolve um token de período na janela meio-aberta [data_inicio, data_fim). Aceita tokens
// relativos (+ aliases de mês 'atual'/'anterior'), "YYYY-MM" e "YYYY-MM-DD..YYYY-MM-DD"
// (intervalo personalizado, pontas inclusivas → fim+1 dia). Lança se o token não for reconhecido.
export function resolverJanela(
  token: string,
  hoje?: Date,
): { data_inicio: string; data_fim: string } {
  const t = (token ?? "").trim();
  const h = hoje ?? hojeSaoPaulo();
  const j = (ini: Date, fim: Date) => ({ data_inicio: isoUTC(ini), data_fim: isoUTC(fim) });

  switch (t) {
    case "mes_atual":
    case "atual": {
      const ini = primeiroDoMes(h);
      return j(ini, proximoMes(ini));
    }
    case "mes_anterior":
    case "anterior": {
      const prim = primeiroDoMes(h);
      return j(new Date(Date.UTC(prim.getUTCFullYear(), prim.getUTCMonth() - 1, 1)), prim);
    }
    case "semana_atual": {
      const ini = domingoDaSemana(h);
      return j(ini, addDias(ini, 7));
    }
    case "semana_anterior": {
      const fim = domingoDaSemana(h);
      return j(addDias(fim, -7), fim);
    }
    case "hoje":
      return j(h, addDias(h, 1));
    case "ontem":
      return j(addDias(h, -1), h);
    case "ultimos_7_dias":
      return j(addDias(h, -6), addDias(h, 1));
    case "ultimos_30_dias":
      return j(addDias(h, -29), addDias(h, 1));
  }
  // "YYYY-MM" — mês específico.
  const ym = /^(\d{4})-(0[1-9]|1[0-2])$/.exec(t);
  if (ym) {
    const ini = new Date(Date.UTC(Number(ym[1]), Number(ym[2]) - 1, 1));
    return j(ini, proximoMes(ini));
  }
  // "YYYY-MM-DD..YYYY-MM-DD" — intervalo personalizado (pontas inclusivas → fim+1 dia).
  const iv = /^(\d{4})-(\d{2})-(\d{2})\.\.(\d{4})-(\d{2})-(\d{2})$/.exec(t);
  if (iv) {
    const ini = new Date(Date.UTC(Number(iv[1]), Number(iv[2]) - 1, Number(iv[3])));
    const fimIncl = new Date(Date.UTC(Number(iv[4]), Number(iv[5]) - 1, Number(iv[6])));
    return j(ini, addDias(fimIncl, 1));
  }
  throw new Error(`Período não reconhecido: ${token}`);
}

// Resolve um valor de corte AS-OF (system time) num timestamp INCLUSIVO ISO
// "YYYY-MM-DD HH:MM:SS[.ffffff]". Espelho de core/.../periodos.py::resolver_corte (testes de paridade).
// Aceita timestamp literal (→ ele mesmo), data "YYYY-MM-DD" (→ fim do dia) ou token de período/
// "YYYY-MM" (→ último instante inclusivo = janela.data_fim − 1µs). O nó fonte filtra DATA_REGISTRO <= corte.
export function resolverCorte(valor: string, hoje?: Date): string {
  const v = (valor ?? "").trim();
  if (/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?/.test(v)) return v.replace("T", " ");
  if (/^\d{4}-\d{2}-\d{2}$/.test(v)) return `${v} 23:59:59.999999`;
  const { data_fim } = resolverJanela(v, hoje); // ISO date EXCLUSIVO
  const [a, m, d] = data_fim.split("-").map(Number);
  const ultimo = new Date(Date.UTC(a, m - 1, d) - 1000); // −1s (front exibe; backend usa −1µs)
  const p = (n: number) => String(n).padStart(2, "0");
  return (
    `${ultimo.getUTCFullYear()}-${p(ultimo.getUTCMonth() + 1)}-${p(ultimo.getUTCDate())} ` +
    `${p(ultimo.getUTCHours())}:${p(ultimo.getUTCMinutes())}:${p(ultimo.getUTCSeconds())}`
  );
}

// Papel de um parâmetro de data pelo NOME (heurística única, compartilhada por Portal e
// Agendamentos): "inicio" | "fim" | null. Centraliza a convenção para não duplicar a regex.
export function papelData(nome: string): "inicio" | "fim" | null {
  if (/fim|final|ate|hasta/i.test(nome)) return "fim";
  if (/inicio|inicial|de\b|desde/i.test(nome)) return "inicio";
  return null;
}

// Dentre uma lista de nomes de parâmetros `data`, qual é início e qual é fim. Espera exatamente
// dois; usa `papelData` e cai para a ordem da lista quando o nome não denuncia o papel.
export function paresDeData(nomes: string[]): { inicio: string; fim: string } | null {
  if (nomes.length !== 2) return null;
  const fim = nomes.find((n) => papelData(n) === "fim") ?? nomes[1];
  const inicio = nomes.find((n) => n !== fim) ?? nomes[0];
  return { inicio, fim };
}

// Token → rótulo pt-BR para exibição (relativo, "YYYY-MM" → "mai/2026", ou intervalo).
const MESES_ABREV = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"];
export function rotuloPeriodo(token: string): string {
  const rel = PERIODOS_RELATIVOS.find((p) => p.token === token);
  if (rel) return rel.rotulo;
  const ym = /^(\d{4})-(\d{2})$/.exec(token);
  if (ym) return `${MESES_ABREV[Number(ym[2]) - 1] ?? ym[2]}/${ym[1]}`;
  const iv = /^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$/.exec(token);
  if (iv) return `${iv[1]} a ${iv[2]}`;
  return token;
}
