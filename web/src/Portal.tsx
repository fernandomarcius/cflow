import { useEffect, useState } from "react";

import * as api from "./api/client";
import { PerspectivePivot } from "./PerspectivePivot";

// Portal (F5): superfície do operador. Catálogo de páginas publicadas (read-only), consulta e
// geração de relatório carimbado. Os blocos só REFERENCIAM o modelo — a página não calcula.

export function Portal() {
  const [catalogo, setCatalogo] = useState<api.PipelineResumo[]>([]);
  const [paginaId, setPaginaId] = useState("");
  const [resultado, setResultado] = useState<any>(null);
  const [relatorio, setRelatorio] = useState<any>(null);
  const [erro, setErro] = useState("");

  useEffect(() => { api.catalogo().then(setCatalogo); }, []);

  const abrir = async (id: string) => {
    setPaginaId(id); setRelatorio(null);
    try { setResultado(await api.consultarPagina(id)); setErro(""); } catch (e: any) { setErro(e.message); setResultado(null); }
  };
  const gerar = async () => {
    try { setRelatorio(await api.gerarRelatorio(paginaId)); setErro(""); } catch (e: any) { setErro(e.message); }
  };

  return (
    <div data-testid="portal" style={{ padding: 16, maxWidth: 760, display: "flex", flexDirection: "column", gap: 8 }}>
      <h3>Portal — catálogo</h3>
      <select data-testid="catalogo-select" value={paginaId} onChange={(e) => e.target.value && abrir(e.target.value)}>
        <option value="">— escolher página —</option>
        {catalogo.map((p) => <option key={p.id} value={p.id}>{p.nome}</option>)}
      </select>
      {erro && <p style={{ color: "crimson" }}>{erro}</p>}
      {resultado && (
        <div data-testid="pagina-render">
          <h4>{resultado.pagina} <small>v{resultado.versao}</small></h4>
          {resultado.blocos.map((b: any, i: number) => (
            <div key={i}>
              <b>{b.titulo}</b>
              <PerspectivePivot resultado={b.resultado} />
            </div>
          ))}
          <button data-testid="btn-relatorio" onClick={gerar}>Gerar relatório</button>
        </div>
      )}
      {relatorio && (
        <div data-testid="relatorio" style={{ background: "#f6f6f6", padding: 8 }}>
          <b>Relatório carimbado</b> — id {relatorio.id.slice(0, 8)} · página v{relatorio.versao}
          <div style={{ fontSize: 11 }}>rastreio (execução): {relatorio.trace?.map((t: any) => t.origem?.execucao_id?.slice(0, 8)).join(", ")}</div>
        </div>
      )}
    </div>
  );
}
