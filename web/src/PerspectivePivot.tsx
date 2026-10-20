import { useEffect, useRef, useState } from "react";

import { PivotTable } from "./PivotTable";

// O elemento JSX <perspective-viewer> é tipado pela própria lib (@finos/perspective-viewer
// augmenta JSX.IntrinsicElements). Não redeclarar aqui — colidiria com o tipo da lib (TS2717).

type Resultado = { colunas: string[]; linhas: any[]; layout?: { linhas?: string[]; colunas?: string[]; valores?: string[] } };

// Carrega o Perspective (WASM) sob demanda — DINÂMICO, fora do bundle principal, para um eventual
// problema de WASM/Vite não derrubar o app. Inicializa uma única vez.
let _perspective: Promise<any> | null = null;
async function carregar(): Promise<any> {
  if (!_perspective) {
    _perspective = (async () => {
      const [{ default: perspective }, { default: viewer }] = await Promise.all([
        import("@finos/perspective"),
        import("@finos/perspective-viewer"),
      ]);
      await import("@finos/perspective-viewer-datagrid");
      const SERVER = (await import("@finos/perspective/dist/wasm/perspective-server.wasm?url")).default;
      const CLIENT = (await import("@finos/perspective-viewer/dist/wasm/perspective-viewer.wasm?url")).default;
      await Promise.all([perspective.init_server(fetch(SERVER)), viewer.init_client(fetch(CLIENT))]);
      return perspective;
    })();
  }
  return _perspective;
}

function comTimeout<T>(p: Promise<T>, ms: number): Promise<T> {
  return Promise.race([p, new Promise<T>((_, rej) => setTimeout(() => rej(new Error("timeout")), ms))]);
}

// Pivot Perspective/FINOS (Apache 2.0). Se o WASM falhar ou demorar, cai para a PivotTable.
export function PerspectivePivot({ resultado }: { resultado: Resultado }) {
  const ref = useRef<any>(null);
  const [falhou, setFalhou] = useState(false);

  useEffect(() => {
    let tabela: any;
    let cancelado = false;
    (async () => {
      try {
        const perspective = await comTimeout(carregar(), 8000);
        if (cancelado || !ref.current || !resultado.linhas?.length) return;
        const worker = await perspective.worker();
        tabela = await worker.table(resultado.linhas);
        if (cancelado) { tabela.delete(); return; }
        await ref.current.load(tabela);
        const lay = resultado.layout;
        if (lay) await ref.current.restore({ group_by: lay.linhas || [], split_by: lay.colunas || [], columns: lay.valores });
      } catch {
        if (!cancelado) setFalhou(true);
      }
    })();
    return () => { cancelado = true; try { tabela?.delete(); } catch { /* noop */ } };
  }, [resultado]);

  if (falhou) return <PivotTable resultado={resultado} />;
  return <perspective-viewer ref={ref} style={{ height: 320, border: "1px solid #ddd" }} />;
}
