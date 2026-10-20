// Pivot client-side (linhas × colunas × valores) sobre o resultado da consulta. React puro, sem
// dependência WASM. (Perspective/FINOS é o renderer alternativo de produção; integração à parte.)
type Resultado = { colunas: string[]; linhas: any[]; layout?: { linhas?: string[]; colunas?: string[]; valores?: string[] } };

function uniq(xs: string[]): string[] {
  return Array.from(new Set(xs));
}

export function PivotTable({ resultado }: { resultado: Resultado }) {
  const { colunas, linhas, layout } = resultado;
  const rowDims = layout?.linhas ?? [];
  const colDims = layout?.colunas ?? [];
  const valores = layout?.valores ?? colunas.filter((c) => !rowDims.includes(c) && !colDims.includes(c));

  // Sem dimensão de coluna → tabela simples (dimensões de linha + medidas).
  if (!colDims.length) {
    const cols = rowDims.length ? [...rowDims, ...valores] : colunas;
    return (
      <table data-testid="pivot" border={1} cellPadding={4} style={{ borderCollapse: "collapse" }}>
        <thead><tr>{cols.map((c) => <th key={c}>{c}</th>)}</tr></thead>
        <tbody>
          {linhas.map((ln, i) => <tr key={i}>{cols.map((c) => <td key={c}>{String(ln[c] ?? "")}</td>)}</tr>)}
        </tbody>
      </table>
    );
  }

  // Pivot: linhas distintas × (colunas distintas × medidas).
  const chaveR = (r: any) => rowDims.map((d) => r[d]).join(" / ");
  const chaveC = (r: any) => colDims.map((d) => r[d]).join(" / ");
  const rowKeys = uniq(linhas.map(chaveR));
  const colKeys = uniq(linhas.map(chaveC));
  const idx: Record<string, any> = {};
  linhas.forEach((r) => { idx[`${chaveR(r)}§${chaveC(r)}`] = r; });

  return (
    <table data-testid="pivot" border={1} cellPadding={4} style={{ borderCollapse: "collapse" }}>
      <thead>
        <tr>
          <th>{rowDims.join(" / ")}</th>
          {colKeys.map((ck) => valores.map((v) => <th key={ck + v}>{ck} · {v}</th>))}
        </tr>
      </thead>
      <tbody>
        {rowKeys.map((rk) => (
          <tr key={rk}>
            <td>{rk}</td>
            {colKeys.map((ck) => valores.map((v) => {
              const cel = idx[`${rk}§${ck}`];
              return <td key={ck + v}>{cel ? String(cel[v] ?? "") : ""}</td>;
            }))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
