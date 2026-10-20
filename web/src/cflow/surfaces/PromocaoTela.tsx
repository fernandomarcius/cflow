import { useEffect, useMemo, useState } from "react";

import * as api from "../../api/client";
import { ConfirmarAcao } from "../ui";
import { lerPacote, nomeArquivo, resumirPlano, ROTULO_ACAO, type PlanoPromocao } from "./promocao";

// Promover entre ambientes pela TELA. Antes isso era script
// (`scripts/promover_pipeline.py`): o `/admin/exportar` antigo levava rascunho, perdia a
// classificação e não reapontava a fonte tipo pipeline. Aqui:
//  - exportar leva a versão PUBLICADA do que se escolhe e de tudo o que ele lê (o fecho);
//  - importar SIMULA primeiro — o plano que aparece é o que o servidor vai executar, e o botão só
//    habilita quando há o que gravar; o que é do ambiente (credencial, ingestão, caminho, biblioteca)
//    aparece como pendência nomeada.
// Só admin: a API recusa os demais, e a tela mostra a recusa.

function mensagemDe(e: unknown, padrao: string): string {
  const x = e as { mensagem?: string; message?: string } | null;
  return x?.mensagem ?? x?.message ?? padrao;
}

function baixar(conteudo: string, nome: string) {
  const url = URL.createObjectURL(new Blob([conteudo], { type: "application/json" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = nome;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

const CELULA: React.CSSProperties = { padding: "6px 10px", borderBottom: "1px solid var(--hair)" };

export default function Promocao() {
  const [pipelines, setPipelines] = useState<api.PipelineResumo[]>([]);
  const [busca, setBusca] = useState("");
  const [escolhidos, setEscolhidos] = useState<Set<string>>(new Set());
  const [exportando, setExportando] = useState(false);
  const [aviso, setAviso] = useState<string | null>(null);
  const [erro, setErro] = useState<string | null>(null);

  const [pacote, setPacote] = useState<Record<string, unknown> | null>(null);
  const [arquivo, setArquivo] = useState("");
  const [atualizar, setAtualizar] = useState(false);
  const [plano, setPlano] = useState<PlanoPromocao | null>(null);
  const [simulando, setSimulando] = useState(false);
  const [confirmando, setConfirmando] = useState(false);
  const [resultado, setResultado] = useState<PlanoPromocao | null>(null);

  useEffect(() => {
    api
      .listarPipelines()
      .then(setPipelines)
      .catch(() => setPipelines([]));
  }, []);

  const visiveis = useMemo(() => {
    const q = busca.trim().toLowerCase();
    return q ? pipelines.filter((p) => p.nome.toLowerCase().includes(q)) : pipelines;
  }, [pipelines, busca]);
  const resumo = plano ? resumirPlano(plano) : null;

  async function exportar() {
    setErro(null);
    setAviso(null);
    setExportando(true);
    try {
      const pac = await api.exportarPacote([...escolhidos]);
      baixar(JSON.stringify(pac, null, 1), nomeArquivo(new Date()));
      const pls = (pac.pipelines as unknown[]) ?? [];
      const cxs = (pac.conexoes as unknown[]) ?? [];
      const fora = (pac.sem_versao_publicada as string[]) ?? [];
      setAviso(
        `Pacote com ${pls.length} pipeline(s) e ${cxs.length} conexão(ões), sem segredo.` +
          (fora.length ? ` Sem versão publicada, ficaram de fora: ${fora.join(", ")}.` : ""),
      );
    } catch (e) {
      setErro(mensagemDe(e, "Falha ao exportar o pacote."));
    } finally {
      setExportando(false);
    }
  }

  function escolherArquivo(f: File | undefined) {
    setErro(null);
    setPlano(null);
    setResultado(null);
    setPacote(null);
    setArquivo(f?.name ?? "");
    if (!f) return;
    f.text().then((texto) => {
      const lido = lerPacote(texto);
      if (lido.erro) setErro(lido.erro);
      else setPacote(lido.pacote ?? null);
    });
  }

  async function simular() {
    if (!pacote) return;
    setErro(null);
    setResultado(null);
    setSimulando(true);
    try {
      setPlano(await api.importarPacote(pacote, { atualizar, simular: true }));
    } catch (e) {
      setPlano(null);
      setErro(mensagemDe(e, "Falha ao simular a importação."));
    } finally {
      setSimulando(false);
    }
  }

  async function importar() {
    if (!pacote) return;
    setErro(null);
    try {
      const r = await api.importarPacote(pacote, { atualizar, simular: false });
      setResultado(r);
      setPlano(null);
      api.listarPipelines().then(setPipelines).catch(() => undefined);
    } catch (e) {
      setErro(mensagemDe(e, "Falha ao importar o pacote."));
    }
  }

  return (
    <div className="page">
      {confirmando && plano && resumo && (
        <ConfirmarAcao
          titulo="Importar o pacote"
          corpo={
            <>
              Cria {resumo.criar} pipeline(s), publica versão nova em {resumo.atualizar} e cria{" "}
              {resumo.conexoesNovas} conexão(ões) <b>sem credencial</b>, numa transação só — tudo ou
              nada, com trilha de auditoria.
              {plano.pendencias.length > 0 && (
                <span className="exec-aviso" style={{ display: "block", marginTop: 10 }}>
                  {plano.pendencias.length} pendência(s) do ambiente continuam depois da importação.
                </span>
              )}
            </>
          }
          rotuloConfirmar="Importar"
          perigo
          onConfirmar={() => {
            setConfirmando(false);
            void importar();
          }}
          onCancelar={() => setConfirmando(false)}
        />
      )}

      <div className="page-head">
        <h1 className="display">Promover entre ambientes</h1>
        <p className="sub">
          Leva pipelines publicados e as conexões que eles usam de um ambiente para outro. A credencial
          não viaja: conexão nova chega sem ela.
        </p>
      </div>

      {erro && (
        <div className="exec-banner-falha" role="alert" data-testid="promo-erro" style={{ display: "block" }}>
          {erro}
        </div>
      )}

      <section className="card" style={{ padding: 16, marginBottom: 16 }} aria-labelledby="promo-exp">
        <h2 id="promo-exp" style={{ marginTop: 0 }}>
          1. Exportar deste ambiente
        </h2>
        <p style={{ marginTop: 0 }}>
          Escolha o que promover — o pacote leva junto tudo o que eles leem por fonte tipo pipeline. Sem
          escolher nada, leva todos os publicados.
        </p>
        <input
          className="nm"
          data-testid="promo-busca"
          placeholder="Filtrar por nome"
          value={busca}
          onChange={(e) => setBusca(e.target.value)}
          style={{ maxWidth: 360, marginBottom: 8 }}
        />
        <div style={{ maxHeight: 260, overflowY: "auto", border: "1px solid var(--hair)", borderRadius: 8 }}>
          {visiveis.map((p) => (
            <label key={p.id} style={{ display: "flex", gap: 8, padding: "4px 10px", alignItems: "center" }}>
              <input
                type="checkbox"
                data-testid={`promo-escolher-${p.id}`}
                checked={escolhidos.has(p.id)}
                onChange={(e) =>
                  setEscolhidos((s) => {
                    const n = new Set(s);
                    if (e.target.checked) n.add(p.id);
                    else n.delete(p.id);
                    return n;
                  })
                }
              />
              {p.nome}
            </label>
          ))}
          {visiveis.length === 0 && <div style={{ padding: 10 }}>Nenhum pipeline com esse nome.</div>}
        </div>
        <div style={{ display: "flex", gap: 12, alignItems: "center", marginTop: 10 }}>
          <button className="btn-stamp" data-testid="promo-exportar" disabled={exportando} onClick={() => void exportar()}>
            {exportando ? "Exportando…" : escolhidos.size ? `Baixar pacote (${escolhidos.size} + dependências)` : "Baixar pacote (todos)"}
          </button>
          {aviso && (
            <span role="status" data-testid="promo-aviso">
              {aviso}
            </span>
          )}
        </div>
      </section>

      <section className="card" style={{ padding: 16 }} aria-labelledby="promo-imp">
        <h2 id="promo-imp" style={{ marginTop: 0 }}>
          2. Importar neste ambiente
        </h2>
        <div style={{ display: "flex", gap: 14, flexWrap: "wrap", alignItems: "center" }}>
          <label>
            Pacote:{" "}
            <input
              type="file"
              accept="application/json,.json"
              data-testid="promo-arquivo"
              onChange={(e) => escolherArquivo(e.target.files?.[0])}
            />
          </label>
          <label style={{ display: "flex", gap: 6, alignItems: "center" }}>
            <input
              type="checkbox"
              data-testid="promo-atualizar"
              checked={atualizar}
              onChange={(e) => {
                setAtualizar(e.target.checked);
                setPlano(null); // o plano de antes não vale para a outra escolha
              }}
            />
            Atualizar os que já existem (publica versão nova quando a definição mudou)
          </label>
          <button className="btn-sec" data-testid="promo-simular" disabled={!pacote || simulando} onClick={() => void simular()}>
            {simulando ? "Simulando…" : "Simular"}
          </button>
          <button
            className="btn-stamp"
            data-testid="promo-importar"
            disabled={!plano || !resumo?.temEscrita}
            title={plano && !resumo?.temEscrita ? "Nada a gravar: tudo já está igual neste ambiente." : undefined}
            onClick={() => setConfirmando(true)}
          >
            Importar
          </button>
        </div>
        {arquivo && !plano && !resultado && pacote && (
          <p style={{ marginBottom: 0 }}>
            <b>{arquivo}</b> lido. Simule para ver o que muda aqui — nada é gravado.
          </p>
        )}

        {plano && resumo && (
          <div data-testid="promo-plano" style={{ marginTop: 14 }}>
            <p>
              <b>Plano:</b> criar {resumo.criar} · publicar versão nova {resumo.atualizar} · já iguais{" "}
              {resumo.manter} · diferentes sem “atualizar” {resumo.diferentes} · conexões novas{" "}
              {resumo.conexoesNovas} · reusadas {resumo.conexoesReusadas}
            </p>
            <div style={{ overflowX: "auto" }}>
              <table style={{ borderCollapse: "collapse", width: "100%" }}>
                <thead>
                  <tr>
                    {["Pipeline", "O que acontece", "Camada", "Grupo", "Sistema", "Diferenças"].map((c) => (
                      <th key={c} style={{ ...CELULA, textAlign: "left" }}>
                        {c}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {plano.pipelines.map((p) => (
                    <tr key={p.nome} data-testid={`promo-linha-${p.nome}`}>
                      <td style={CELULA}>{p.nome}</td>
                      <td style={CELULA}>{ROTULO_ACAO[p.acao]}</td>
                      <td style={CELULA}>{p.camada ?? "—"}</td>
                      <td style={CELULA}>{p.grupo ?? "—"}</td>
                      <td style={CELULA}>{p.sistema ? "sim" : "não"}</td>
                      <td style={CELULA}>{p.diferencas.join(", ") || "—"}</td>
                    </tr>
                  ))}
                  {plano.conexoes.map((c) => (
                    <tr key={`cx-${c.nome}`}>
                      <td style={CELULA}>
                        conexão <b>{c.nome}</b> ({c.tipo})
                      </td>
                      <td style={CELULA} colSpan={5}>
                        {c.acao === "reusar" ? "já existe aqui — reusada" : "criar sem credencial"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {(plano ?? resultado)?.pendencias.length ? (
          <div className="exec-aviso" data-testid="promo-pendencias" style={{ marginTop: 12 }}>
            <b>Pendências deste ambiente</b> — o pacote não tem como resolver:
            <ul style={{ margin: "6px 0 0 18px" }}>
              {(plano ?? resultado)!.pendencias.map((m) => (
                <li key={m}>{m}</li>
              ))}
            </ul>
          </div>
        ) : null}

        {resultado && (
          <p role="status" data-testid="promo-resultado" style={{ marginBottom: 0 }}>
            Importado: {resultado.pipelines_criados ?? 0} pipeline(s) criado(s),{" "}
            {resultado.pipelines_atualizados ?? 0} com versão nova e {resultado.conexoes_criadas ?? 0}{" "}
            conexão(ões) criada(s) sem credencial.
          </p>
        )}
      </section>
    </div>
  );
}
