// Test of src/prefixo.ts, without a browser or a backend:
//     node scripts/teste-prefixo.mjs   (or: npm run test:prefixo)
//
// Proves: at the root the URLs are the plain ones (`/api/v1/...`, `/execucoes/1`); under any mount
// path (derived from the bundle URL) they gain that prefix at any route depth.
import { apiUrl, prefixo, rota } from "../src/prefixo.ts";

let falhas = 0;
function igual(obtido, esperado, oque) {
  if (obtido !== esperado) {
    falhas++;
    console.error(`FALHA: ${oque}: esperado ${JSON.stringify(esperado)}, veio ${JSON.stringify(obtido)}`);
  }
}

// --- prefixo fora do build: vazio -------------------------------------------------------------
igual(prefixo("/"), "", "raiz");
igual(prefixo("/pipeline"), "", "rota na raiz");
igual(prefixo("/execucoes/abc"), "", "rota profunda na raiz");

// --- prefixo pela URL do bundle (build servido em qualquer caminho) ---------------------------
// No navegador o módulo vem de `<prefixo>/assets/<chunk>.js`; o pathname da página não importa.
igual(prefixo("/", "https://h/assets/index-a1.js"), "", "bundle na raiz");
igual(prefixo("/execucoes/1", "https://h/assets/index-a1.js"), "", "bundle na raiz, rota profunda");
igual(prefixo("/", "https://example.com/cflow/assets/index-a1.js"), "/cflow", "bundle sob um proxy reverso");
igual(prefixo("/", "https://h/a/b/c/assets/x.js"), "/a/b/c", "qualquer profundidade de montagem");

// --- apiUrl ----------------------------------------------------------------------------------
igual(apiUrl("/auth/login", "/"), "/api/v1/auth/login", "API na raiz");
igual(apiUrl("/execucoes?status=falha", "/execucoes"), "/api/v1/execucoes?status=falha", "query preservada");
igual(apiUrl("arquivos", "/pipeline"), "/api/v1/arquivos", "caminho sem barra inicial");

// --- rota: links fora do Router e arquivos de public/ -----------------------------------------
igual(rota("/", "/redefinir"), "/", "login na raiz");
igual(rota("/execucoes/1", "/agendamentos"), "/execucoes/1", "link de execução na raiz");
igual(rota("favicon.svg", "/"), "/favicon.svg", "arquivo público na raiz");

if (falhas) {
  console.error(`${falhas} falha(s)`);
  process.exit(1);
}
console.log("ok — prefixo: raiz intacta e qualquer caminho de montagem (URL do bundle) prefixado em API, links e arquivos públicos.");
