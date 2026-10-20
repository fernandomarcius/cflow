// Danger zone de BACKFILL/REPLAY — molde: `ConfirmarReexecucao.tsx` (busca um "plano" assíncrono,
// nomeia o efeito, `perigo` condicional, campo de confirmação por texto quando o efeito é arriscado).
//
// A diferença de risco entre os dois É o ponto do componente (regra da 010: "um clique de backfill
// custa a reputação do nosso IP na origem"):
//  - backfill refaz a janela DA ORIGEM — cada fatia é uma requisição real. Exige digitar o dataset.
//  - replay reconstrói só da LANDING — sem rede. Mesmo guardrail admin-only, sem o campo de texto.
import { useEffect, useState } from "react";

import * as api from "../../api/client";
import { ConfirmarAcao } from "../ui";

interface ConfirmarBackfillReplayProps {
  fonte: api.FonteLake;
  tipo: "backfill" | "replay";
  onFeito: () => void;
  onCancelar: () => void;
}

export default function ConfirmarBackfillReplay({
  fonte,
  tipo,
  onFeito,
  onCancelar,
}: ConfirmarBackfillReplayProps) {
  const [inicio, setInicio] = useState(fonte.watermark ?? "");
  const [fim, setFim] = useState(() => new Date().toISOString().slice(0, 10));
  const [estimativa, setEstimativa] = useState<api.EstimativaIngestao | null>(null);
  const [carregandoEstimativa, setCarregandoEstimativa] = useState(false);
  const [erro, setErro] = useState<string | null>(null);
  const [enviando, setEnviando] = useState(false);

  // Re-busca a estimativa a cada mudança de janela — mesma função (`plano_dry_run`) que a execução
  // de verdade usa por baixo, então o número mostrado é o número que vai rodar.
  useEffect(() => {
    if (!inicio.trim() || !fim.trim()) {
      setEstimativa(null);
      return;
    }
    let vivo = true;
    setCarregandoEstimativa(true);
    api
      .estimarFatiasIngestao(fonte.id, inicio, fim)
      .then((e) => vivo && setEstimativa(e))
      .catch((e) => vivo && setErro(e instanceof Error ? e.message : "Falha ao estimar a janela."))
      .finally(() => vivo && setCarregandoEstimativa(false));
    return () => {
      vivo = false;
    };
  }, [fonte.id, inicio, fim]);

  async function confirmar(texto: string) {
    if (tipo === "backfill" && texto.trim() !== fonte.dataset) {
      setErro(`Digite "${fonte.dataset}" para confirmar.`);
      return;
    }
    setErro(null);
    setEnviando(true);
    try {
      if (tipo === "backfill") await api.backfillFonteIngestao(fonte.id, inicio, fim);
      else await api.replayFonteIngestao(fonte.id, inicio, fim);
      onFeito();
    } catch (e) {
      setErro(e instanceof Error ? e.message : "Falha ao disparar.");
      setEnviando(false);
    }
  }

  // `corpo` vai dentro de um `<p>` no ConfirmarAcao (`ui.tsx`) — por isso `<span>`, nunca `<div>`,
  // para o layout em grid (mesma técnica de `ConfirmarReexecucao`: `<span style={{display:"block"}}>`
  // é conteúdo de fluxo permitido dentro de `<p>`, `<div>` não é).
  const corpo = (
    <>
      <span style={{ display: "block", marginBottom: 10 }}>
        {tipo === "backfill" ? (
          <>
            Refaz a janela <b>da origem</b> — cada fatia é uma requisição real à fonte pública.
          </>
        ) : (
          <>
            Reconstrói o bronze só a partir da <b>landing</b> já gravada — sem tocar a rede. Use
            quando a regra do bronze mudou (encoding, layout, coluna esperada).
          </>
        )}
      </span>
      <span style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginBottom: 10 }}>
        <label style={{ display: "block" }}>
          <span className="fld">Início</span>
          <input
            className="nm"
            type="date"
            data-testid="reproc-inicio"
            value={inicio}
            onChange={(e) => setInicio(e.target.value)}
          />
        </label>
        <label style={{ display: "block" }}>
          <span className="fld">Fim</span>
          <input
            className="nm"
            type="date"
            data-testid="reproc-fim"
            value={fim}
            onChange={(e) => setFim(e.target.value)}
          />
        </label>
      </span>
      <span data-testid="reproc-estimativa" style={{ display: "block", fontSize: 13 }}>
        {carregandoEstimativa ? (
          "Calculando…"
        ) : estimativa ? (
          <>
            <b>{estimativa.fatias}</b> {estimativa.fatias === 1 ? "fatia" : "fatias"} na janela de{" "}
            {estimativa.janela.inicio} a {estimativa.janela.fim}
            {tipo === "backfill" ? (
              <> — {estimativa.fatias} {estimativa.fatias === 1 ? "requisição" : "requisições"} à origem pública.</>
            ) : (
              <> — sem tocar a rede.</>
            )}
          </>
        ) : (
          "Informe início e fim para estimar a janela."
        )}
      </span>
    </>
  );

  return (
    <ConfirmarAcao
      titulo={tipo === "backfill" ? `Backfill de ${fonte.nome}` : `Replay de ${fonte.nome}`}
      corpo={corpo}
      rotuloConfirmar={enviando ? "Disparando…" : tipo === "backfill" ? "Disparar backfill" : "Disparar replay"}
      perigo={tipo === "backfill"}
      valorInicial={tipo === "backfill" ? "" : undefined}
      rotuloCampo={tipo === "backfill" ? `Digite "${fonte.dataset}" para confirmar` : undefined}
      erro={erro}
      onConfirmar={(texto) => void confirmar(texto)}
      onCancelar={onCancelar}
    />
  );
}
