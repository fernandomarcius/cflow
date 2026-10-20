// Teste da ordem das leituras da tela de Páginas (src/cflow/surfaces/paginaCarga.ts), sem navegador:
//     node --import ./scripts/resolver-ts.mjs scripts/teste-pagina-carga.mjs   (ou: npm run test:pagina-carga)
//
// Prova que as leituras independentes SAEM JUNTAS (a segunda é pedida antes de a primeira voltar) e
// que o resultado é o de antes, quando elas eram em série: mesma ordem de aplicação, mesma régua de
// falha, e a leitura de um rascunho já publicado é descartada. Cada "ida ao servidor" é uma promessa
// que o teste resolve à mão — é o que deixa ver quem foi pedido antes de quem voltou.
import { carregarListasAuxiliares, lerCatalogoComRascunhos } from "../src/cflow/surfaces/paginaCarga.ts";

let falhas = 0;
function ok(cond, oque) {
  if (!cond) {
    falhas++;
    console.error(`FALHA: ${oque}`);
  }
}
const vez = () => new Promise((r) => setImmediate(r)); // deixa as continuações correrem

function adiado() {
  let resolver, rejeitar;
  const p = new Promise((res, rej) => {
    resolver = res;
    rejeitar = rej;
  });
  return { p, resolver, rejeitar };
}

// Servidor de mentira: registra a ordem dos PEDIDOS e deixa o teste responder quando quiser.
function servidor() {
  const log = [];
  const pendentes = new Map();
  return {
    log,
    pedir(nome) {
      return () => {
        log.push(`pede:${nome}`);
        const d = adiado();
        pendentes.set(nome, d);
        return d.p;
      };
    },
    // Responder o que ainda não foi pedido = a leitura saiu em SÉRIE (esperou outra voltar).
    // Falha explícita em vez de exceção; o `await` do teste que depende dela fica sem desfecho e o
    // Node sai com erro ("unsettled top-level await") — nunca passa em silêncio.
    responder(nome, valor) {
      log.push(`volta:${nome}`);
      if (!pendentes.has(nome)) return ok(false, `respondeu "${nome}" antes de ele ser pedido`);
      pendentes.get(nome).resolver(valor);
    },
    falhar(nome) {
      log.push(`falha:${nome}`);
      if (!pendentes.has(nome)) return ok(false, `falhou "${nome}" antes de ele ser pedido`);
      pendentes.get(nome).rejeitar(new Error(`${nome} fora do ar`));
    },
  };
}

// --- Construtor: views e pipelines -------------------------------------------------------------
{
  const s = servidor();
  const aplicados = [];
  const fim = carregarListasAuxiliares(
    { views: s.pedir("views"), pipelines: s.pedir("pipelines") },
    { views: (v) => aplicados.push(["views", v]), pipelines: (p) => aplicados.push(["pipelines", p]) },
  );
  await vez();
  ok(
    s.log.join(",") === "pede:views,pede:pipelines",
    `as duas listas são pedidas antes de qualquer resposta (log: ${s.log.join(",")})`,
  );
  // O servidor responde fora de ordem: pipelines primeiro.
  s.responder("pipelines", ["p1"]);
  await vez();
  ok(aplicados.length === 0, "pipelines não é aplicado antes das views (mesma ordem de antes)");
  s.responder("views", ["v1"]);
  await fim;
  ok(
    JSON.stringify(aplicados) === JSON.stringify([["views", ["v1"]], ["pipelines", ["p1"]]]),
    `aplicação na ordem views → pipelines (veio ${JSON.stringify(aplicados)})`,
  );
}
{
  // Falha nas views: como em série, pipelines não é aplicado e o erro sobe para o chamador.
  const s = servidor();
  const aplicados = [];
  const fim = carregarListasAuxiliares(
    { views: s.pedir("views"), pipelines: s.pedir("pipelines") },
    { views: () => aplicados.push("views"), pipelines: () => aplicados.push("pipelines") },
  );
  await vez();
  s.falhar("views");
  s.falhar("pipelines"); // falha de quem ninguém aguarda não pode virar "unhandled rejection"
  let subiu = false;
  await fim.catch(() => (subiu = true));
  ok(subiu && aplicados.length === 0, "views falhou: nada aplicado e o erro chega ao chamador");
}
{
  // Falha só nos pipelines: a paleta de views fica (como antes).
  const s = servidor();
  const aplicados = [];
  const fim = carregarListasAuxiliares(
    { views: s.pedir("views"), pipelines: s.pedir("pipelines") },
    { views: () => aplicados.push("views"), pipelines: () => aplicados.push("pipelines") },
  );
  await vez();
  s.falhar("pipelines");
  s.responder("views", []);
  let subiu = false;
  await fim.catch(() => (subiu = true));
  ok(subiu && aplicados.join() === "views", "pipelines falhou: views aplicadas, pipelines não");
}

// --- Lista: catálogo e rascunhos ---------------------------------------------------------------
{
  const s = servidor();
  const lerRascunho = (id) => s.pedir(`rascunho:${id}`)();
  let catalogoAplicado = null;
  const fim = lerCatalogoComRascunhos(s.pedir("catalogo"), ["r1", "pub"], lerRascunho, (c) => {
    catalogoAplicado = c;
  });
  await vez();
  ok(
    ["pede:rascunho:r1", "pede:rascunho:pub", "pede:catalogo"].every((x) => s.log.includes(x)) &&
      !s.log.some((x) => x.startsWith("volta:")),
    `catálogo e rascunhos pedidos antes de qualquer resposta (log: ${s.log.join(",")})`,
  );
  s.responder("catalogo", [{ id: "pub" }, { id: "outra" }]);
  await vez();
  ok(catalogoAplicado?.length === 2, "a lista aparece assim que o catálogo volta, sem esperar rascunhos");
  s.responder("rascunho:pub", { id: "pub", morto: false, nome: "já publicada" });
  s.responder("rascunho:r1", { id: "r1", morto: false, nome: "Rascunho 1" });
  const r = await fim;
  ok(
    JSON.stringify(r.lidos) === JSON.stringify([{ id: "r1", morto: false, nome: "Rascunho 1" }]),
    `rascunho já publicado é descartado; os outros ficam na ordem (veio ${JSON.stringify(r.lidos)})`,
  );
  ok(r.publicadas.length === 2, "publicadas devolvidas como o catálogo mandou");
}
{
  // Catálogo fora do ar: o erro sobe como antes e nada é aplicado.
  const s = servidor();
  let aplicou = false;
  const fim = lerCatalogoComRascunhos(s.pedir("catalogo"), ["r1"], (id) => s.pedir(`rascunho:${id}`)(), () => {
    aplicou = true;
  });
  await vez();
  s.falhar("catalogo");
  s.responder("rascunho:r1", { id: "r1", morto: true });
  let subiu = false;
  await fim.catch(() => (subiu = true));
  ok(subiu && !aplicou, "catálogo falhou: erro chega ao chamador e a lista não é aplicada");
}
{
  // Sem rascunhos lembrados: só o catálogo é pedido.
  const s = servidor();
  const fim = lerCatalogoComRascunhos(s.pedir("catalogo"), [], () => Promise.reject(new Error("não")), () => {});
  await vez();
  s.responder("catalogo", []);
  const r = await fim;
  ok(s.log.join(",") === "pede:catalogo,volta:catalogo" && r.lidos.length === 0, "sem rascunhos: só o catálogo");
}

if (falhas) {
  console.error(`\n${falhas} falha(s) na ordem das leituras da tela de Páginas.`);
  process.exit(1);
}
console.log("teste-pagina-carga: ok — leituras independentes saem juntas, resultado igual ao de antes.");
