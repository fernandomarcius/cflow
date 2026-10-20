// Resolvedor de import extensionless para os testes que rodam no Node puro (sem navegador, sem
// bundler): `node --import ./scripts/resolver-ts.mjs scripts/teste-*.mjs`.
//
// Por que existe: o código do app escreve `from "../execucao/formato"` (sem extensão), que é o que o
// Vite resolve. O Node não resolve — ele exige o caminho completo. Os dois testes anteriores não
// tropeçaram nisso por acidente: `layout.ts` não importa nada e `camadas.ts` só faz `import type`
// (apagado na compilação). O primeiro módulo puro a reusar uma função de verdade de outro arquivo
// (`ingestao.ts` → `formato.ts`, para não ter uma segunda cópia da formatação de momento) bateu.
//
// As alternativas foram piores: escrever `"./formato.ts"` no código do app exige
// `allowImportingTsExtensions` no tsconfig compartilhado e deixa UM import diferente de todos os
// outros; e duplicar a formatação no módulo puro é justamente a divergência entre telas que
// `formato.ts` existe para impedir.
//
// Não é build nem empacotamento: só ensina o Node a achar `X.ts` (ou `X.tsx`) quando o import diz
// `X`. Nada aqui roda em produção.
//
// `.tsx` (telas): o Node tira os tipos de `.ts` sozinho, mas não entende JSX. Um `.tsx` importado por
// um teste é transpilado aqui pelo `typescript` do projeto (o mesmo do `tsc`), com JSX automático e
// source map embutido — o c8 mapeia a cobertura de volta para as linhas do `.tsx`.
import { existsSync, readFileSync } from "node:fs";
import { createRequire, registerHooks } from "node:module";
import { dirname, resolve as resolverCaminho } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

let ts = null;
function transpilarTsx(caminho) {
  ts ??= createRequire(import.meta.url)("typescript");
  return ts.transpileModule(readFileSync(caminho, "utf8"), {
    fileName: caminho,
    compilerOptions: {
      jsx: ts.JsxEmit.ReactJSX,
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022,
      inlineSourceMap: true,
      inlineSources: true,
    },
  }).outputText;
}

registerHooks({
  resolve(especificador, contexto, proximo) {
    // Só import RELATIVO e só sem extensão. Pacote do node_modules e caminho já completo seguem o
    // resolvedor normal — interceptar mais que isso seria reimplementar a resolução do Node.
    const relativo = especificador.startsWith("./") || especificador.startsWith("../");
    const semExtensao = !/\.[cm]?[jt]sx?$|\.json$/i.test(especificador);
    if (relativo && semExtensao && contexto?.parentURL?.startsWith("file:")) {
      const base = dirname(fileURLToPath(contexto.parentURL));
      // Só desvia se o arquivo EXISTE: sem esta checagem, um import errado passaria a falhar
      // apontando um `.ts` que ninguém escreveu, escondendo o erro de verdade. `.ts` antes de `.tsx`,
      // como no Vite.
      for (const extensao of [".ts", ".tsx"]) {
        const alvo = resolverCaminho(base, `${especificador}${extensao}`);
        if (existsSync(alvo)) return proximo(pathToFileURL(alvo).href, contexto);
      }
    }
    return proximo(especificador, contexto);
  },
  load(url, contexto, proximo) {
    if (url.startsWith("file:") && url.endsWith(".tsx")) {
      return { format: "module", source: transpilarTsx(fileURLToPath(url)), shortCircuit: true };
    }
    return proximo(url, contexto);
  },
});
