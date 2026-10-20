import { PERIODOS_RELATIVOS, resolverJanela } from "./periodos";

// Seletor de período reutilizável (Portal + Agendamentos): menu com "mês específico" /
// "intervalo personalizado" / tokens relativos (mês/semana/dia/últimos N). Emite o TOKEN escolhido
// (que `periodos.ts` resolve em [data_inicio, data_fim)). Mostra a janela resolvida como dica.

// Mês atual ("YYYY-MM") e hoje ("YYYY-MM-DD") em America/Sao_Paulo (default dos seletores).
function mesAtualSP(): string {
  const s = new Date().toLocaleDateString("en-CA", { timeZone: "America/Sao_Paulo" }); // YYYY-MM-DD
  return s.slice(0, 7);
}
function hojeSP(): string {
  return new Date().toLocaleDateString("en-CA", { timeZone: "America/Sao_Paulo" });
}

export function SeletorPeriodo({
  periodo,
  onChange,
  testid,
  permitirTudo = false,
}: {
  periodo: string;
  onChange: (token: string) => void;
  testid?: string;
  // `permitirTudo` acrescenta "Todo o período" (token vazio) — para filtro OPCIONAL, como o
  // histórico de execuções. Onde o período é obrigatório (parâmetro do Portal), não passar.
  permitirTudo?: boolean;
}) {
  const ehMes = /^\d{4}-\d{2}$/.test(periodo);
  const mInt = /^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$/.exec(periodo);
  const selVal = ehMes ? "__mes__" : mInt ? "__intervalo__" : periodo;
  let janela = "";
  if (permitirTudo && !periodo) {
    janela = "sem limite de data";
  } else {
    try {
      const iv = resolverJanela(periodo);
      janela = `${iv.data_inicio} → ${iv.data_fim} (fim exclusivo)`;
    } catch {
      janela = "intervalo inválido";
    }
  }
  return (
    <div className="periodo">
      <select
        aria-label="Tipo de período"
        data-testid={testid ? `${testid}-tipo` : "param-periodo-tipo"}
        value={selVal}
        onChange={(e) => {
          const v = e.target.value;
          if (v === "__mes__") onChange(mesAtualSP());
          else if (v === "__intervalo__") {
            const hoje = hojeSP();
            onChange(`${hoje}..${hoje}`);
          } else onChange(v);
        }}
      >
        {permitirTudo && <option value="">Todo o período</option>}
        <option value="__mes__">Mês específico</option>
        <option value="__intervalo__">Intervalo personalizado</option>
        {PERIODOS_RELATIVOS.map((p) => (
          <option key={p.token} value={p.token}>
            {p.rotulo}
          </option>
        ))}
      </select>
      {ehMes && (
        <input
          type="month"
          aria-label="Mês de referência"
          data-testid={testid ? `${testid}-mes` : "param-mes-competencia"}
          value={periodo}
          max={mesAtualSP()}
          onChange={(e) => onChange(e.target.value || mesAtualSP())}
        />
      )}
      {mInt && (
        <span className="periodo-intervalo">
          <input
            type="date"
            aria-label="Data de início do intervalo"
            data-testid={testid ? `${testid}-de` : "param-data-inicio"}
            value={mInt[1]}
            onChange={(e) => onChange(`${e.target.value || mInt[1]}..${mInt[2]}`)}
          />
          <span className="periodo-ate">até</span>
          <input
            type="date"
            aria-label="Data de fim do intervalo"
            data-testid={testid ? `${testid}-ate` : "param-data-fim"}
            value={mInt[2]}
            onChange={(e) => onChange(`${mInt[1]}..${e.target.value || mInt[2]}`)}
          />
        </span>
      )}
      <span className="periodo-hint">{janela}</span>
    </div>
  );
}
