import { useEffect } from "react";

// Poll que respeita a aba: com a aba OCULTA (outra aba em primeiro plano, janela minimizada) não
// pede nada ao servidor; ao voltar a ficar visível, atualiza NA HORA e retoma o intervalo. Ninguém
// está olhando uma aba oculta, e cada pedido custa uma ida e volta pela rede —
// deixar três telas de execução abertas em abas esquecidas era tráfego contínuo à toa.
//
// A parte pura (`iniciarPollVisivel`) recebe o ambiente por parâmetro para ser testada sem
// navegador (scripts/teste-poll.mjs); `usePollVisivel` é o que as telas usam.

/** O que o poll precisa do navegador: saber se a aba está visível, ouvir a troca e um relógio. */
export interface AmbientePoll {
  visivel(): boolean;
  /** Registra o ouvinte de troca de visibilidade; devolve a função que o remove. */
  aoMudarVisibilidade(cb: () => void): () => void;
  agendar(cb: () => void, ms: number): unknown;
  cancelar(t: unknown): void;
}

type DocVisibilidade = Pick<Document, "visibilityState" | "addEventListener" | "removeEventListener">;

export function ambienteNavegador(doc: DocVisibilidade = document): AmbientePoll {
  return {
    visivel: () => doc.visibilityState !== "hidden",
    aoMudarVisibilidade(cb) {
      doc.addEventListener("visibilitychange", cb);
      return () => doc.removeEventListener("visibilitychange", cb);
    },
    agendar: (cb, ms) => setInterval(cb, ms),
    cancelar: (t) => clearInterval(t as ReturnType<typeof setInterval>),
  };
}

/**
 * Chama `tique` a cada `intervaloMs` enquanto a aba está visível. Aba oculta → para o relógio.
 * Voltou a ficar visível → um `tique` imediato (o que está na tela pode ter envelhecido) e o
 * relógio recomeça. Devolve a função que desliga tudo (relógio e ouvinte).
 */
export function iniciarPollVisivel(
  tique: () => void,
  intervaloMs: number,
  amb: AmbientePoll = ambienteNavegador(),
): () => void {
  let relogio: unknown = null;
  const ligar = () => {
    if (relogio === null) relogio = amb.agendar(tique, intervaloMs);
  };
  const desligar = () => {
    if (relogio !== null) {
      amb.cancelar(relogio);
      relogio = null;
    }
  };
  const aoMudar = () => {
    if (!amb.visivel()) {
      desligar();
      return;
    }
    // Evento repetido com a aba já visível não pode disparar um pedido extra.
    if (relogio !== null) return;
    tique();
    ligar();
  };
  if (amb.visivel()) ligar();
  const soltar = amb.aoMudarVisibilidade(aoMudar);
  return () => {
    soltar();
    desligar();
  };
}

/**
 * Poll de tela: liga enquanto `ativo` (ex.: há execução em andamento) e pausa com a aba oculta.
 * `tique` precisa ser estável (useCallback): trocar o `tique` reinicia o relógio, como trocar a
 * função de um `setInterval` dentro de um efeito fazia antes.
 */
export function usePollVisivel(ativo: boolean, tique: () => void, intervaloMs: number): void {
  useEffect(() => {
    if (!ativo) return;
    return iniciarPollVisivel(tique, intervaloMs);
  }, [ativo, tique, intervaloMs]);
}
