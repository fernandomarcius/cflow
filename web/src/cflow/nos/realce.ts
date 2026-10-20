import { Decoration, MatchDecorator, ViewPlugin, type DecorationSet, type EditorView, type ViewUpdate } from "@codemirror/view";

// Realça os parâmetros `:nome` no editor (mesma regra do backend: ignora `::cast`) com classe
// .cm-param, p/ destacar no código onde um parâmetro de runtime é aplicado.
const marca = Decoration.mark({ class: "cm-param" });
const matcher = new MatchDecorator({ regexp: /(?<!:):([a-zA-Z_]\w*)/g, decoration: marca });

export const realceParametros = ViewPlugin.fromClass(
  class {
    decorations: DecorationSet;
    constructor(view: EditorView) {
      this.decorations = matcher.createDeco(view);
    }
    update(u: ViewUpdate) {
      this.decorations = matcher.updateDeco(u, this.decorations);
    }
  },
  { decorations: (v) => v.decorations },
);
