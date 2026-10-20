import { createElement } from "react";

// Neutral cFlow mark (same drawing as public/favicon.svg), inlined so it needs no extra request.
export const LOGO_URI =
  "data:image/svg+xml,%3Csvg viewBox=%220 0 64 64%22 xmlns=%22http://www.w3.org/2000/svg%22 role=%22img%22 aria-label=%22cFlow%22%3E%3Crect width=%2264%22 height=%2264%22 rx=%2214%22 fill=%22%231f3a5f%22/%3E%3Ccircle cx=%2216%22 cy=%2232%22 r=%225%22 fill=%22%239ec531%22/%3E%3Ccircle cx=%2248%22 cy=%2218%22 r=%225%22 fill=%22%23e8eef7%22/%3E%3Ccircle cx=%2248%22 cy=%2246%22 r=%225%22 fill=%22%23e8eef7%22/%3E%3Cpath d=%22M21 32 C 32 32, 34 18, 43 18 M21 32 C 32 32, 34 46, 43 46%22 fill=%22none%22 stroke=%22%239ec531%22 stroke-width=%223%22 stroke-linecap=%22round%22/%3E%3C/svg%3E";

// Wordmark = ícone (gradiente + ondas, funciona em qualquer fundo) + texto "cFlow".
// O texto usa a cor herdada do container (`.wordmark-text`, definida por tema no CSS) → a wordmark
// recolore por tema: clara no light, escura no dark. O "Flow" mantém o verde da marca.
export function Wordmark({
  height = 22,
  className,
}: {
  height?: number;
  className?: string;
}): JSX.Element {
  return createElement(
    "span",
    {
      className: ["wordmark", className].filter(Boolean).join(" "),
      style: { display: "inline-flex", alignItems: "center", gap: Math.round(height * 0.34), lineHeight: 1 },
    },
    createElement("img", {
      src: LOGO_URI,
      height,
      width: height,
      alt: "",
      "aria-hidden": true,
      style: { display: "block", borderRadius: Math.round(height * 0.28) },
    }),
    createElement(
      "span",
      {
        className: "wordmark-text",
        style: {
          fontFamily: '"Space Grotesk", sans-serif',
          fontWeight: 700,
          fontSize: Math.round(height * 0.84),
          letterSpacing: "-.01em",
        },
      },
      "c",
      createElement("b", { style: { color: "#9ec531", fontWeight: 700 } }, "Flow"),
    ),
  );
}
