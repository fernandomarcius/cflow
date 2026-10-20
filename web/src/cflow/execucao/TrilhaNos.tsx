import { useEffect, useRef } from "react";

import type { NoDoGrafo } from "./ExecGrafo";
import { decorridoNo, fmtDuracaoMs, pillNo, linhasTxt } from "./formato";

// Visão em LISTA dos nós da execução — alternativa ao grafo, não substituta.
//
// O grafo mostra a topologia; num pipeline longo (dezenas de nós) ele exige zoom e pan para
// responder "qual nó falhou?" e "quantas linhas saíram de cada etapa?". A lista responde isso de
// relance, na ordem em que o layout do grafo enfileira os nós (profundidade no DAG).
//
// Usa `.noderow`/`.cdot`/`.reap`, que já existiam no CSS do produto para exatamente esta forma.

export default function TrilhaNos({
  nos,
  selecionado,
  onSelecionar,
  focoPedido = 0,
}: {
  nos: NoDoGrafo[];
  selecionado: string | null;
  onSelecionar: (id: string) => void;
  // Mesmo contrato do grafo: só uma ESCOLHA deliberada revela a linha. Sem isso, a lista abria
  // rolada até o último nó do fluxo (a auto-seleção pega a ponta) — escondendo as fontes, que é
  // por onde um pipeline se lê — e, pior, rolava de volta a cada render: com o cronômetro de 1s e
  // o poll de 2s, acompanhar uma execução ficava impossível porque a lista não parava quieta.
  focoPedido?: number;
}) {
  const refs = useRef(new Map<string, HTMLButtonElement>());
  useEffect(() => {
    if (!focoPedido || !selecionado) return;
    refs.current.get(selecionado)?.scrollIntoView({ block: "nearest" });
  }, [focoPedido, selecionado]);

  return (
    <div className="exec-trilha" data-testid="exec-trilha">
      {nos.map((n) => {
        const pill = pillNo(n.estado);
        return (
          <button
            key={n.id}
            type="button"
            className={"noderow exec-noderow " + pill.cls + (n.id === selecionado ? " sel" : "")}
            aria-current={n.id === selecionado ? "true" : undefined}
            data-testid={`exec-trilha-${n.id}`}
            ref={(el) => {
              if (el) refs.current.set(n.id, el);
              else refs.current.delete(n.id);
            }}
            onClick={() => onSelecionar(n.id)}
          >
            <span className={"cdot " + pill.cls} />
            <span>
              <span className="nn">{n.nome}</span>
              <span className="nt">
                {n.tipo}
                {n.orfao && <span className="reap">fora da versão</span>}
              </span>
            </span>
            <span className="res">
              {n.estado === "falha" ? (
                <span className="err">{n.erro ?? "falhou"}</span>
              ) : n.estado === "skip" ? (
                "não executado"
              ) : n.estado === "executando" ? (
                <b>em execução</b>
              ) : n.estado === "pendente" ? (
                "aguardando"
              ) : (
                <>
                  <b>{linhasTxt(n.linhas)}</b>
                  {n.reaproveitado && <span className="reap">reaproveitado</span>}
                </>
              )}
            </span>
            {/* Nó rodando não tem duração final: a coluna passa a contar o tempo decorrido, que
                é o número que responde "está andando ou empacou?". */}
            <span className="rdur">
              {n.estado === "executando"
                ? decorridoNo(n) != null
                  ? "há " + fmtDuracaoMs(decorridoNo(n))
                  : "…"
                : n.executou
                  ? fmtDuracaoMs(n.duracao_ms)
                  : "—"}
            </span>
          </button>
        );
      })}
    </div>
  );
}
