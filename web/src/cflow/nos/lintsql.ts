import { linter, type Diagnostic } from "@codemirror/lint";
import type { EditorView } from "@codemirror/view";

// Validação de sintaxe (leve, client-side) do SQL na code area: parênteses balanceados e aspas
// simples fechadas. Marca os pontos com squiggle vermelho (diagnostics do @codemirror/lint).
// Não substitui o parser do backend (sqlglot) — pega os erros mais comuns durante a escrita.
// Validação pura (sem CodeMirror): true se parênteses balanceados e aspas simples fechadas.
// Usada p/ habilitar o "Salvar" da janela de código só quando a sintaxe básica está ok.
export function sqlSintaxeOk(txt: string): boolean {
  let emAspas = false;
  let abertos = 0;
  for (let i = 0; i < txt.length; i++) {
    const c = txt[i];
    if (emAspas) {
      if (c === "'") {
        if (txt[i + 1] === "'") i++;
        else emAspas = false;
      }
      continue;
    }
    if (c === "'") emAspas = true;
    else if (c === "(") abertos++;
    else if (c === ")") {
      abertos--;
      if (abertos < 0) return false;
    }
  }
  return abertos === 0 && !emAspas;
}

export const validacaoSql = linter((view: EditorView): Diagnostic[] => {
  const txt = view.state.doc.toString();
  const diags: Diagnostic[] = [];

  let emAspas = false;
  let inicioAspas = -1;
  const pilha: number[] = [];
  for (let i = 0; i < txt.length; i++) {
    const c = txt[i];
    if (emAspas) {
      if (c === "'") {
        if (txt[i + 1] === "'") i++; // '' = aspas escapada
        else {
          emAspas = false;
          inicioAspas = -1;
        }
      }
      continue;
    }
    if (c === "'") {
      emAspas = true;
      inicioAspas = i;
    } else if (c === "(") {
      pilha.push(i);
    } else if (c === ")") {
      if (pilha.length === 0) diags.push({ from: i, to: i + 1, severity: "error", message: "Parêntese ')' sem abertura." });
      else pilha.pop();
    }
  }
  if (emAspas && inicioAspas >= 0) diags.push({ from: inicioAspas, to: txt.length, severity: "error", message: "Aspas simples não fechada." });
  for (const p of pilha) diags.push({ from: p, to: p + 1, severity: "error", message: "Parêntese '(' não fechado." });
  return diags;
});
