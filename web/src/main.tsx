import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Provider } from "react-redux";

import App from "./App";
import { aplicarTema, temaInicial } from "./cflow/tema";
// O CSS do React Flow ANTES do cflow.css, como era quando as telas estavam no bundle principal: o
// cflow.css sobrescreve dezenas de regras `.react-flow__*` com a mesma especificidade. Importado só
// pelas telas (carregadas sob demanda), ele chegaria DEPOIS e desfaria essas sobrescritas.
import "reactflow/dist/style.css";
import "./cflow.css";
import "@fontsource/space-grotesk/400.css";
import "@fontsource/space-grotesk/500.css";
import "@fontsource/space-grotesk/600.css";
import "@fontsource/space-grotesk/700.css";
import { store } from "./store/store";

// Tema aplicado antes do render — evita flash claro→escuro no boot.
aplicarTema(temaInicial());

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Provider store={store}>
      <App />
    </Provider>
  </StrictMode>,
);
