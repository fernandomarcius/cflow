// Teste do poll que pausa com a aba oculta (src/cflow/poll.ts), sem navegador:
//     node --import ./scripts/resolver-ts.mjs scripts/teste-poll.mjs   (ou: npm run test:poll)
//
// Prova: com a aba visível o tique sai a cada intervalo; oculta, o relógio para e nada é pedido;
// ao voltar, UM tique imediato e o relógio recomeça; evento repetido não dobra pedido; desligar
// solta o ouvinte. A segunda parte usa um EventTarget de verdade e o setInterval do Node, para
// provar que o ouvinte é mesmo o `visibilitychange` e que o relógio real para.
import { ambienteNavegador, iniciarPollVisivel } from "../src/cflow/poll.ts";

let falhas = 0;
function ok(cond, oque) {
  if (!cond) {
    falhas++;
    console.error(`FALHA: ${oque}`);
  }
}

// --- Relógio e aba de mentira: o teste avança o tempo à mão -------------------------------------
function ambienteFalso(visivelInicial = true) {
  let visivel = visivelInicial;
  const ouvintes = new Set();
  let agora = 0;
  const relogios = new Map(); // id → { cb, ms, proximo }
  let seq = 0;
  return {
    amb: {
      visivel: () => visivel,
      aoMudarVisibilidade(cb) {
        ouvintes.add(cb);
        return () => ouvintes.delete(cb);
      },
      agendar(cb, ms) {
        const id = ++seq;
        relogios.set(id, { cb, ms, proximo: agora + ms });
        return id;
      },
      cancelar(id) {
        relogios.delete(id);
      },
    },
    mudar(v) {
      visivel = v;
      for (const cb of [...ouvintes]) cb();
    },
    avancar(ms) {
      const fim = agora + ms;
      for (;;) {
        const prox = [...relogios.values()].filter((r) => r.proximo <= fim).sort((a, b) => a.proximo - b.proximo)[0];
        if (!prox) break;
        agora = prox.proximo;
        prox.proximo += prox.ms;
        prox.cb();
      }
      agora = fim;
    },
    relogiosVivos: () => relogios.size,
    ouvintes: () => ouvintes.size,
  };
}

{
  const f = ambienteFalso(true);
  let tiques = 0;
  const parar = iniciarPollVisivel(() => tiques++, 5000, f.amb);
  ok(tiques === 0, "ligar não dispara tique (a carga inicial é da tela)");
  f.avancar(15000);
  ok(tiques === 3, `visível: 3 tiques em 15 s a cada 5 s (veio ${tiques})`);

  f.mudar(false);
  ok(f.relogiosVivos() === 0, "aba oculta: relógio cancelado");
  f.avancar(60000);
  ok(tiques === 3, `oculta por 60 s: nenhum tique (veio ${tiques - 3} a mais)`);

  f.mudar(true);
  ok(tiques === 4, "voltou a ficar visível: UM tique imediato");
  ok(f.relogiosVivos() === 1, "voltou: relógio religado");
  f.mudar(true);
  ok(tiques === 4, "evento repetido com a aba já visível não pede de novo");
  f.avancar(5000);
  ok(tiques === 5, "relógio retomado conta 5 s a partir da volta");

  parar();
  ok(f.relogiosVivos() === 0 && f.ouvintes() === 0, "desligar solta relógio e ouvinte");
  f.mudar(false);
  f.mudar(true);
  f.avancar(20000);
  ok(tiques === 5, "depois de desligado, nenhuma troca de aba pede nada");
}

{
  // Tela aberta numa aba que já nasceu oculta (ex.: link aberto em segundo plano).
  const f = ambienteFalso(false);
  let tiques = 0;
  const parar = iniciarPollVisivel(() => tiques++, 5000, f.amb);
  ok(f.relogiosVivos() === 0, "nasceu oculta: relógio não liga");
  f.avancar(30000);
  ok(tiques === 0, "nasceu oculta: nada pedido");
  f.mudar(true);
  ok(tiques === 1 && f.relogiosVivos() === 1, "ficou visível: tique imediato e relógio ligado");
  parar();
}

// --- visibilitychange de verdade + setInterval do Node ------------------------------------------
{
  const doc = new EventTarget();
  let estado = "visible";
  Object.defineProperty(doc, "visibilityState", { get: () => estado });
  const trocar = (v) => {
    estado = v;
    doc.dispatchEvent(new Event("visibilitychange"));
  };
  const espera = (ms) => new Promise((r) => setTimeout(r, ms));

  let tiques = 0;
  const parar = iniciarPollVisivel(() => tiques++, 20, ambienteNavegador(doc));
  await espera(110);
  const visiveis = tiques;
  ok(visiveis >= 3, `relógio real, visível: tiques a cada 20 ms (veio ${visiveis} em 110 ms)`);

  trocar("hidden");
  const aoOcultar = tiques;
  await espera(150);
  ok(tiques === aoOcultar, `relógio real, oculta: parou (veio ${tiques - aoOcultar} a mais em 150 ms)`);

  trocar("visible");
  ok(tiques === aoOcultar + 1, "relógio real: visibilitychange para visível dá um tique na hora");
  await espera(70);
  ok(tiques > aoOcultar + 1, "relógio real: retomou depois de voltar");

  parar();
  const aoParar = tiques;
  await espera(80);
  trocar("hidden");
  trocar("visible");
  ok(tiques === aoParar, "relógio real: depois de desligado, nem relógio nem evento pedem nada");
}

if (falhas) {
  console.error(`\n${falhas} falha(s) no poll com a aba oculta.`);
  process.exit(1);
}
console.log("teste-poll: ok — pausa com a aba oculta, tique imediato ao voltar, desligar solta tudo.");
