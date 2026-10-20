// Ordem das leituras ao abrir a tela de Páginas (lista e construtor). Lógica pura, testada sem
// navegador em scripts/teste-pagina-carga.mjs.
//
// Com a API atrás de uma rede lenta, cada ida do navegador custa centenas de ms e a API responde
// em milissegundos: o que a pessoa espera é o NÚMERO de idas em série, não o servidor. Aqui ficam
// as leituras que não dependem uma da outra e por isso saem juntas. O que a tela faz com cada
// resposta — e em que ordem aplica — não muda: só deixou de esperar uma para PEDIR a outra.

/** Promessa que ninguém vai aguardar não pode estourar como "unhandled rejection" no console. */
function semAlarme(p: Promise<unknown>): void {
  p.catch(() => {
    /* a falha é tratada por quem aguarda (ou ignorada de propósito, ver cada uso) */
  });
}

/**
 * Listas auxiliares do construtor: views publicadas (paleta) e pipelines (execução parametrizada).
 * As duas são pedidas AO MESMO TEMPO; a aplicação segue a ordem e a régua de antes: views primeiro,
 * e se a leitura das views falha a de pipelines também não é aplicada (a falha das listas
 * auxiliares não bloqueia o construtor — o chamador trata).
 */
export async function carregarListasAuxiliares<V, P>(
  pedir: { views: () => Promise<V>; pipelines: () => Promise<P> },
  aplicar: { views: (v: V) => void; pipelines: (p: P) => void },
): Promise<void> {
  const views = pedir.views();
  const pipelines = pedir.pipelines();
  // Se as views falharem, ninguém aguarda a promessa dos pipelines.
  semAlarme(pipelines);
  aplicar.views(await views);
  aplicar.pipelines(await pipelines);
}

/**
 * Catálogo (páginas publicadas) e rascunhos lembrados neste navegador. Antes a leitura de cada
 * rascunho só saía depois do catálogo voltar, porque o catálogo diz quais deles já foram
 * publicados. Agora saem juntas, e o catálogo continua decidindo quais leituras contam: a de um
 * rascunho que já está publicado é descartada, exatamente como se nunca tivesse sido feita.
 *
 * `aoCatalogo` roda assim que o catálogo chega (a lista aparece sem esperar os rascunhos, como
 * antes). `lerRascunho` não deve rejeitar: trata a própria falha (ver `RascunhoLido` em
 * Pagina.tsx). Se o catálogo falha, a exceção sobe e as leituras de rascunho são ignoradas.
 */
export async function lerCatalogoComRascunhos<C extends { id: string }, R>(
  lerCatalogo: () => Promise<C[]>,
  idsRascunho: string[],
  lerRascunho: (id: string) => Promise<R>,
  aoCatalogo: (publicadas: C[]) => void,
): Promise<{ publicadas: C[]; lidos: R[] }> {
  const pendentes = idsRascunho.map((id) => {
    const p = lerRascunho(id);
    semAlarme(p);
    return { id, p };
  });
  const publicadas = await lerCatalogo();
  aoCatalogo(publicadas);
  const publicadosIds = new Set(publicadas.map((p) => p.id));
  const lidos = await Promise.all(pendentes.filter((x) => !publicadosIds.has(x.id)).map((x) => x.p));
  return { publicadas, lidos };
}
