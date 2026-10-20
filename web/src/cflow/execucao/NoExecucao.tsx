import { memo } from "react";
import { Handle, Position, type NodeProps } from "reactflow";

import { fmtDuracaoMs, type EstadoNo, linhasTxt } from "./formato";

// Nó do grafo na tela de EXECUÇÃO — read-only e leve, de propósito.
//
// Não reusa o `NoPipeline` do editor: aquele carrega CodeMirror, formulários das operações
// guiadas e o contexto de edição — nada disso existe aqui, e o peso atrapalharia um grafo de
// 50 nós que só precisa mostrar "rodou? quantas linhas? quanto tempo?".
//
// As dimensões (200 × ~64) são compactas para o `fitView` conseguir enquadrar o grafo inteiro:
// o card do editor tem ~240 × 600, e enquadrar AQUELE tamanho é o que fazia o board abrir
// ilegível. O layout em `ExecGrafo` assume esta largura.

export interface DadosNoExec {
  nome: string;
  tipo: string;
  estado: EstadoNo;
  linhas?: number | null;
  duracao_ms?: number | null;
  reaproveitado?: boolean | null;
  // Há quanto tempo o nó está rodando (só no estado `executando`). Calculado por quem monta o
  // grafo, a cada poll — o card não tem cronômetro próprio.
  decorrido_ms?: number | null;
  selecionado?: boolean;
  orfao?: boolean; // rodou, mas não está na definição desta versão (grafo mudou)
  // Texto no lugar das linhas, para nó que não é nó de pipeline (o mapa da corrida desenha
  // PIPELINES: "— linhas" ali seria um número que não existe). Ausente = o comportamento de sempre.
  metrica?: string | null;
  // Fora do caminho destacado (mapa da corrida com um pipeline selecionado): o card esmaece.
  apagado?: boolean;
}

function NoExecucaoBase({ data }: NodeProps<DadosNoExec>) {
  const { estado } = data;
  const metrica =
    data.metrica ??
    (estado === "falha"
      ? "erro"
      : estado === "skip"
        ? "não executado"
        // "em execução" e "aguardando" eram a MESMA palavra: quem abria a tela para saber em que
        // passo a execução está não encontrava a resposta em lugar nenhum.
        : estado === "executando"
          ? "em execução"
          : estado === "pendente"
            ? "aguardando"
            // Zero linha é achado, não rotina: no card ele se anuncia em vez de virar "0 linhas"
            // no meio de uma coluna de números.
            : (data.linhas ?? null) === 0
              ? "sem linha"
              : linhasTxt(data.linhas));
  // `title` com as métricas: em zoom baixo o CSS esconde tipo/linhas/duração, e o hover passa a ser
  // a forma de conferir sem aproximar.
  const resumo = `${data.tipo}${data.linhas != null ? ` · ${linhasTxt(data.linhas)}` : ""}${
    data.duracao_ms != null ? ` · ${fmtDuracaoMs(data.duracao_ms)}` : ""
  }`;
  return (
    <div
      title={`${data.nome} — ${resumo}`}
      className={
        "exec-node exec-node--" +
        estado +
        (data.selecionado ? " sel" : "") +
        (data.orfao ? " orfao" : "") +
        (data.apagado ? " apagado" : "")
      }
    >
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <div className="exec-node-bar" />
      <div className="exec-node-corpo">
        <div className="exec-node-nome" title={data.nome}>
          {data.nome}
        </div>
        <div className="exec-node-tipo">
          {data.tipo}
          {data.orfao && <span className="exec-node-flag">fora da versão</span>}
          {data.reaproveitado && <span className="exec-node-flag reap">reaproveitado</span>}
        </div>
        <div className="exec-node-metricas">
          <span>{metrica}</span>
          {/* No nó em andamento a duração ainda não existe; o que informa é HÁ QUANTO TEMPO ele
              está rodando — é assim que se distingue "está trabalhando" de "travou". */}
          {estado === "executando" ? (
            data.decorrido_ms != null && <span className="num">há {fmtDuracaoMs(data.decorrido_ms)}</span>
          ) : (
            estado !== "skip" &&
            estado !== "pendente" && <span className="num">{fmtDuracaoMs(data.duracao_ms)}</span>
          )}
        </div>
      </div>
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </div>
  );
}

export default memo(NoExecucaoBase);
