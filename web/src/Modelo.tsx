import { useEffect, useState } from "react";

import * as api from "./api/client";
import { PerspectivePivot } from "./PerspectivePivot";

// Modelo (F4): cria/edita um cubo (fonte = saída de pipeline + dimensões + medidas nomeadas) e
// consulta. Render de pivot simples em tabela; Perspective/FINOS é o renderer de produção (E7).
export function Modelo() {
  const [cubos, setCubos] = useState<api.PipelineResumo[]>([]);
  const [pipelines, setPipelines] = useState<api.PipelineResumo[]>([]);
  const [cuboId, setCuboId] = useState("");
  const [base, setBase] = useState(0);
  const [fontePid, setFontePid] = useState("");
  const [fonteNo, setFonteNo] = useState("saida");
  const [dims, setDims] = useState("cidade:cidade");
  const [meds, setMeds] = useState("total:sum(valor)");
  const [resultado, setResultado] = useState<any>(null);
  const [erro, setErro] = useState("");
  const [msg, setMsg] = useState("");
  // criar view
  const [views, setViews] = useState<api.PipelineResumo[]>([]);
  const [vNome, setVNome] = useState("Nova view");
  const [vCubo, setVCubo] = useState("");
  const [vLinhas, setVLinhas] = useState("");
  const [vColunas, setVColunas] = useState("");
  const [vValores, setVValores] = useState("");
  const [vLib, setVLib] = useState("");
  // criar página
  const [pNome, setPNome] = useState("Nova página");
  const [pView, setPView] = useState("");
  const [pTitulo, setPTitulo] = useState("Bloco");

  const split = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);
  const recarregar = () => { api.listarCubos().then(setCubos); api.listarViews().then(setViews); };
  useEffect(() => { recarregar(); api.listarPipelines().then(setPipelines); }, []);

  const publicarView = async () => {
    try {
      await api.publicarView(vNome, { cubo_id: vCubo, linhas: split(vLinhas), colunas: split(vColunas), valores: split(vValores), filtros_liberados: split(vLib) });
      setMsg("View publicada."); setErro(""); recarregar();
    } catch (e: any) { setErro(e.message); }
  };
  const publicarPagina = async () => {
    try {
      await api.publicarPagina(pNome, { blocos: [{ tipo: "pivot", view_id: pView, titulo: pTitulo }] });
      setMsg("Página publicada — veja no Portal."); setErro("");
    } catch (e: any) { setErro(e.message); }
  };

  const parseDims = () => dims.split(",").map((s) => s.trim()).filter(Boolean).map((p) => {
    const [nome, coluna] = p.split(":"); return { nome, coluna: coluna || nome };
  });
  const parseMeds = () => meds.split(",").map((s) => s.trim()).filter(Boolean).map((p) => {
    const i = p.indexOf(":"); return { nome: p.slice(0, i), expressao: p.slice(i + 1) };
  });

  const novo = async () => { const c = await api.criarCubo("Cubo"); setCuboId(c.id); setBase(0); recarregar(); };
  const abrir = async (id: string) => {
    const d = await api.obterCubo(id); setCuboId(id); setBase(d.numero);
    const def = d.definicao || {};
    if (def.fonte) { setFontePid(def.fonte.pipeline_id); setFonteNo(def.fonte.no_id); }
    if (def.dimensoes) setDims(def.dimensoes.map((x: any) => `${x.nome}:${x.coluna}`).join(","));
    if (def.medidas) setMeds(def.medidas.map((x: any) => `${x.nome}:${x.expressao}`).join(","));
    setResultado(null);
  };
  const salvarPublicar = async () => {
    try {
      const def = { fonte: { pipeline_id: fontePid, no_id: fonteNo }, dimensoes: parseDims(), medidas: parseMeds() };
      const r = await api.salvarCubo(cuboId, def, base);
      const p = await api.publicarCubo(cuboId, r.numero);
      setBase(p.numero); setErro("");
    } catch (e: any) { setErro(e.message); }
  };
  const consultar = async () => {
    try {
      const r = await api.consultarCubo(cuboId, parseDims().map((d) => d.nome), parseMeds().map((m) => m.nome));
      setResultado(r); setErro("");
    } catch (e: any) { setErro(e.message); }
  };

  return (
    <div data-testid="modelo" style={{ padding: 16, maxWidth: 720, display: "flex", flexDirection: "column", gap: 8 }}>
      <h3>Modelo — cubo & medidas</h3>
      <div style={{ display: "flex", gap: 8 }}>
        <button data-testid="cubo-novo" onClick={novo}>Novo cubo</button>
        <select data-testid="cubo-abrir" value={cuboId} onChange={(e) => e.target.value && abrir(e.target.value)}>
          <option value="">— abrir cubo —</option>
          {cubos.map((c) => <option key={c.id} value={c.id}>{c.nome}</option>)}
        </select>
        {cuboId && <span style={{ fontSize: 12 }}>v{base} · {cuboId.slice(0, 8)}</span>}
      </div>
      {cuboId && (
        <>
          <label>Fonte — pipeline:
            <select data-testid="cubo-fonte-pipeline" value={fontePid} onChange={(e) => setFontePid(e.target.value)}>
              <option value="">—</option>
              {pipelines.map((p) => <option key={p.id} value={p.id}>{p.nome}</option>)}
            </select>
          </label>
          <label>Fonte — nó de saída: <input data-testid="cubo-fonte-no" value={fonteNo} onChange={(e) => setFonteNo(e.target.value)} /></label>
          <label>Dimensões (nome:coluna, vírgula): <input data-testid="cubo-dims" value={dims} onChange={(e) => setDims(e.target.value)} /></label>
          <label>Medidas (nome:expressão, vírgula): <input data-testid="cubo-meds" value={meds} onChange={(e) => setMeds(e.target.value)} /></label>
          <div style={{ display: "flex", gap: 8 }}>
            <button data-testid="cubo-publicar" onClick={salvarPublicar}>Salvar + publicar</button>
            <button data-testid="cubo-consultar" onClick={consultar}>Consultar</button>
          </div>
        </>
      )}
      {erro && <p style={{ color: "crimson" }}>{erro}</p>}
      {resultado && <div data-testid="cubo-resultado"><PerspectivePivot resultado={resultado} /></div>}

      <hr />
      <h3>Criar view (pivot de um cubo)</h3>
      <div data-testid="form-view" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <label>Nome: <input data-testid="view-nome" value={vNome} onChange={(e) => setVNome(e.target.value)} /></label>
        <label>Cubo:
          <select data-testid="view-cubo" value={vCubo} onChange={(e) => setVCubo(e.target.value)}>
            <option value="">—</option>
            {cubos.map((c) => <option key={c.id} value={c.id}>{c.nome}</option>)}
          </select>
        </label>
        <label>Linhas (dimensões, vírgula): <input data-testid="view-linhas" value={vLinhas} onChange={(e) => setVLinhas(e.target.value)} /></label>
        <label>Colunas (dimensões, vírgula): <input value={vColunas} onChange={(e) => setVColunas(e.target.value)} /></label>
        <label>Valores (medidas, vírgula): <input data-testid="view-valores" value={vValores} onChange={(e) => setVValores(e.target.value)} /></label>
        <label>Filtros liberados (dimensões, vírgula): <input value={vLib} onChange={(e) => setVLib(e.target.value)} /></label>
        <button data-testid="view-publicar" disabled={!vCubo} onClick={publicarView}>Publicar view</button>
      </div>

      <hr />
      <h3>Criar página</h3>
      <div data-testid="form-pagina" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <label>Nome: <input data-testid="pagina-nome" value={pNome} onChange={(e) => setPNome(e.target.value)} /></label>
        <label>View do bloco:
          <select data-testid="pagina-view" value={pView} onChange={(e) => setPView(e.target.value)}>
            <option value="">—</option>
            {views.map((v) => <option key={v.id} value={v.id}>{v.nome}</option>)}
          </select>
        </label>
        <label>Título do bloco: <input value={pTitulo} onChange={(e) => setPTitulo(e.target.value)} /></label>
        <button data-testid="pagina-publicar" disabled={!pView} onClick={publicarPagina}>Publicar página</button>
      </div>
      {msg && <p data-testid="modelo-msg" style={{ color: "green" }}>{msg}</p>}
    </div>
  );
}
