import { useEffect, useState } from "react";

// Tema da UI (claro/escuro). Persiste em localStorage, aplica via `data-theme` no <html>,
// e avisa componentes de tema próprio (CodeMirror/React Flow) por um evento.
export type Tema = "light" | "dark";

const CHAVE = "cflow-tema";

export function temaInicial(): Tema {
  try {
    return localStorage.getItem(CHAVE) === "dark" ? "dark" : "light";
  } catch {
    return "light";
  }
}

// Aplica no <html> + persiste + dispara evento (chamar no boot, antes do render, evita flash).
export function aplicarTema(t: Tema): void {
  document.documentElement.dataset.theme = t;
  try {
    localStorage.setItem(CHAVE, t);
  } catch {
    /* storage indisponível — segue só em memória */
  }
  window.dispatchEvent(new CustomEvent("cflow-tema", { detail: t }));
}

// Hook do toggle: tema atual + função que alterna.
export function useTema(): [Tema, () => void] {
  const [t, setT] = useState<Tema>(
    () => (document.documentElement.dataset.theme as Tema) || temaInicial(),
  );
  const alternar = () => {
    const novo: Tema = t === "dark" ? "light" : "dark";
    aplicarTema(novo);
    setT(novo);
  };
  return [t, alternar];
}

// Hook só-leitura p/ componentes de tema próprio: reage ao evento de troca.
export function useTemaAtual(): Tema {
  const [t, setT] = useState<Tema>(
    () => (document.documentElement.dataset.theme as Tema) || temaInicial(),
  );
  useEffect(() => {
    const h = (e: Event) => setT((e as CustomEvent<Tema>).detail);
    window.addEventListener("cflow-tema", h);
    return () => window.removeEventListener("cflow-tema", h);
  }, []);
  return t;
}
