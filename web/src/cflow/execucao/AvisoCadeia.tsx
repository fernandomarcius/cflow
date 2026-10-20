import { useEffect, useState } from "react";

import * as api from "../../api/client";
import { rotuloEfeito, rotuloPublicacao } from "./formato";

// O que um disparo deste pipeline roda JUNTO, mostrado ANTES de agendar e de "executar
// agora". Agendar um consumidor roda os upstreams com todos os efeitos deles — e-mail, escrita em
// banco externo, publicação no lake —, e antes disto só se sabia lendo a trilha depois do disparo.
//
// A tela não decide: repete `GET /pipelines/{id}/cadeia`, que usa a MESMA função do disparo. Pipeline
// sem montante não mostra nada — o aviso não pode virar decoração em todo agendamento.
export function AvisoCadeia({ pipelineId }: { pipelineId: string | null | undefined }) {
  const [plano, setPlano] = useState<api.PlanoCadeia | null>(null);
  const [erro, setErro] = useState<string | null>(null);

  useEffect(() => {
    setPlano(null);
    setErro(null);
    if (!pipelineId) return;
    let vivo = true;
    api
      .planoCadeia(pipelineId)
      .then((p) => vivo && setPlano(p))
      .catch((e: any) => vivo && setErro(e?.message ?? "Falha ao montar a cadeia."));
    return () => {
      vivo = false;
    };
  }, [pipelineId]);

  if (!pipelineId) return null;
  if (erro)
    return (
      <span className="exec-erro" role="alert" style={{ display: "block" }} data-testid="cadeia-erro">
        A cadeia deste pipeline não resolve — o disparo falharia: {erro}
      </span>
    );
  if (!plano || plano.montantes.length === 0) return null;

  const comEfeito = plano.montantes.some((m) => m.efeitos.length > 0);
  return (
    <span
      data-testid="cadeia-aviso"
      style={{
        display: "block",
        border: "1px solid var(--hair)",
        borderRadius: 8,
        padding: "8px 10px",
        margin: "8px 0",
        fontSize: 13,
        background: "var(--soft)",
      }}
    >
      <b>Roda junto</b>, na versão publicada,{" "}
      {plano.montantes.length === 1
        ? "o pipeline de que este depende"
        : `os ${plano.montantes.length} pipelines de que este depende`}
      :
      <ul style={{ margin: "6px 0 0 18px" }}>
        {plano.montantes.map((m) => (
          <li key={m.pipeline_id} data-testid={`cadeia-montante-${m.pipeline_id}`}>
            <b>{m.nome}</b> (v{m.versao})
            {m.efeitos.length > 0 && (
              <> — {m.efeitos.map((e) => `${e.no_id}: ${rotuloEfeito(e.tipo)}`).join("; ")}</>
            )}
            {m.publicacoes.length > 0 && (
              <>
                {" "}
                —{" "}
                {plano.sem_publicar
                  ? "calcula sem publicar"
                  : `publica ${m.publicacoes.map(rotuloPublicacao).join(", ")}`}
              </>
            )}
          </li>
        ))}
      </ul>
      {comEfeito && !plano.recusa && (
        <span className="exec-erro" style={{ display: "block", marginTop: 6 }}>
          Os efeitos acima acontecem de novo a cada disparo — quem agenda este pipeline também os agenda.
        </span>
      )}
      {plano.sem_publicar && (
        <span style={{ display: "block", marginTop: 6 }}>
          Este pipeline declara que a montante <b>não publica</b>: ela calcula e grava o snapshot que
          ele lê, sem mover ponteiro no lake.
        </span>
      )}
      {plano.recusa && (
        <span className="exec-erro" style={{ display: "block", marginTop: 6 }} data-testid="cadeia-recusa">
          O disparo será recusado: {plano.recusa.mensagem}
        </span>
      )}
    </span>
  );
}
