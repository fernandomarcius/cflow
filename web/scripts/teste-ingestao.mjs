// Teste da descrição das FONTES DE INGESTÃO na aba Bronze (src/cflow/surfaces/ingestao.ts).
// Roda sem navegador e sem banco:
//     node scripts/teste-ingestao.mjs      (ou: pnpm run test:ingestao)
//
// O que ele prova, e por que cada coisa está aqui:
//  1. "nunca executado" é um estado LEGÍTIMO e fica SEM pill — pintar um pill ali inventaria um
//     desfecho que não houve, e o pill dessa lista significa desfecho de execução;
//  2. o vocabulário de status da ingestão é o MESMO de pipeline (`executando|concluida|falha`), logo
//     `pillExecucao` serve sem tradutor — e status DESCONHECIDO nunca nasce verde;
//  3. status sem carimbo legível não vira data inventada (foi assim que uma linha da tela de
//     Agendamentos caiu inteira: `rotuloDia(null)`);
//  4. classe e cadência desconhecidas mostram o valor CRU, não um rótulo genérico: quando o backend
//     ganhar uma classe nova, a tela mostra o nome dela em vez de "outro";
//  5. a fonte com FALHA sobe para o topo (são 54 linhas; achar a pill vermelha rolando não é achar)
//     e o empate PRESERVA a ordem do servidor, que já vem por nome;
//  6. a QUARENTENA só vira coluna para quem tem, e só pinta de vermelho quando há fatia que nunca
//     pousou no bronze — vermelho no que já foi corrigido treina a pessoa a ignorar a cor, e aí o
//     vermelho que importa some junto.
import {
  abasDeClasse,
  contarFalhas,
  contarFatiasSemBronze,
  contarFontesComBuraco,
  contratoDeForm,
  daAbaClasse,
  estadoInicialContrato,
  formDeContrato,
  indexarQuarentena,
  metaFonte,
  metaQuarentena,
  ordenarFontes,
  pillQuarentena,
  rotuloCadencia,
  rotuloClasse,
  tituloQuarentena,
  ultimaExecucaoFonte,
} from "../src/cflow/surfaces/ingestao.ts";

let falhas = 0;
function ok(cond, oque) {
  if (!cond) {
    falhas++;
    console.error(`FALHA: ${oque}`);
  }
}
function igual(a, b, oque) {
  if (JSON.stringify(a) !== JSON.stringify(b)) {
    falhas++;
    console.error(`FALHA: ${oque}\n  esperado: ${JSON.stringify(b)}\n  obtido:   ${JSON.stringify(a)}`);
  }
}

// --- 4: rótulos ---------------------------------------------------------------
igual(rotuloClasse("http_file"), "arquivo publicado", "classe http_file em pt-BR");
igual(rotuloClasse("rest_api"), "API REST", "classe rest_api em pt-BR");
igual(rotuloClasse("odata"), "OData", "classe odata");
igual(rotuloClasse("portal_download"), "download de portal", "classe portal_download");
// Classe nova no backend não pode virar "outro": o nome cru é informação, o rótulo genérico não.
igual(rotuloClasse("ftp_batch"), "ftp_batch", "classe desconhecida mostra o valor cru");
igual(rotuloClasse(undefined), "classe não informada", "classe ausente é declarada, não vazia");

igual(rotuloCadencia("mensal"), "mensal", "cadência mensal");
igual(rotuloCadencia("diaria"), "diária", "cadência diaria acentuada");
// `manual` é o DEFAULT do banco: dizer só "manual" deixaria alguém esperando por um job que não há.
igual(rotuloCadencia("manual"), "só manual", "cadência manual diz que nada dispara sozinho");
igual(rotuloCadencia("quinzenal"), "quinzenal", "cadência desconhecida mostra o valor cru");

// --- meta da linha ------------------------------------------------------------
const base = {
  id: "1",
  nome: "CVM · informe diário FI",
  dataset: "cvm_informe_diario_fi",
  classe: "http_file",
  cadencia: "mensal",
  sistema: true,
};
igual(
  metaFonte(base),
  "cvm_informe_diario_fi · arquivo publicado · recarga mensal",
  "meta sem watermark não imprime marca que não existe",
);
igual(
  metaFonte({ ...base, watermark: "2026-08" }),
  "cvm_informe_diario_fi · arquivo publicado · recarga mensal · ingerido até 2026-08",
  "watermark entra na meta quando existe",
);
// O dataset vem PRIMEIRO: é a chave pela qual o nó `fonte lake` referencia a fonte, ou seja o que
// liga esta linha ao grafo que a consome.
ok(metaFonte(base).startsWith("cvm_informe_diario_fi"), "dataset é o primeiro campo da meta");

// --- 1/2/3: última execução ---------------------------------------------------
const nunca = ultimaExecucaoFonte(base);
igual(nunca.texto, "nunca executado", "fonte sem execução diz 'nunca executado'");
ok(nunca.status === undefined && nunca.scls === undefined, "'nunca executado' não ganha pill");

const okExec = ultimaExecucaoFonte({
  ...base,
  ultima_execucao_em: "2026-08-15T12:00:00+00:00",
  ultimo_status: "concluida",
});
igual(okExec.status, "concluída", "status concluida vira 'concluída'");
igual(okExec.scls, "ok", "concluida pinta verde");
ok(okExec.texto.startsWith("última execução "), "texto nomeia o que o carimbo é");
// Fuso EXPLÍCITO na linha (regra do projeto): "09:00" sem fuso não diz nada a quem lê de outra
// máquina — e 12:00 UTC são 09:00 em São Paulo, que é a prova de que a conversão acontece.
ok(okExec.texto.includes("(GMT-3)"), "momento traz o fuso explícito");
ok(okExec.texto.includes("09:00"), "momento convertido para America/Sao_Paulo");

const emExec = ultimaExecucaoFonte({
  ...base,
  ultima_execucao_em: "2026-08-15T12:00:00+00:00",
  ultimo_status: "executando",
});
igual(emExec.scls, "exec", "'executando' tem classe própria — nunca a verde de 'terminou bem'");
igual(emExec.status, "em execução", "vocabulário de tela para 'executando'");

const falhou = ultimaExecucaoFonte({
  ...base,
  ultima_execucao_em: "2026-08-15T12:00:00+00:00",
  ultimo_status: "falha",
});
igual(falhou.scls, "falha", "falha pinta vermelho");

// Status que o backend passe a devolver e a tela ainda não conheça NÃO nasce verde.
const desconhecido = ultimaExecucaoFonte({
  ...base,
  ultima_execucao_em: "2026-08-15T12:00:00+00:00",
  ultimo_status: "cancelada",
});
igual(desconhecido.scls, "skip", "status desconhecido não nasce verde");
igual(desconhecido.status, "cancelada", "status desconhecido mostra o valor cru");

// 3: há status, mas o carimbo é ilegível → DIZ que não há horário, não inventa data.
const semCarimbo = ultimaExecucaoFonte({ ...base, ultima_execucao_em: "vazio", ultimo_status: "falha" });
igual(semCarimbo.texto, "última execução sem horário registrado", "carimbo ilegível é declarado");
igual(semCarimbo.scls, "falha", "sem carimbo, o status continua valendo");

// --- 5: ordem e contagem ------------------------------------------------------
const lista = [
  { ...base, id: "a", nome: "A", dataset: "a", ultimo_status: "concluida" },
  { ...base, id: "b", nome: "B", dataset: "b", ultimo_status: "falha" },
  { ...base, id: "c", nome: "C", dataset: "c" },
  { ...base, id: "d", nome: "D", dataset: "d", ultimo_status: "falha" },
];
igual(ordenarFontes(lista).map((f) => f.id), ["b", "d", "a", "c"], "falha sobe; empate mantém a ordem do servidor");
// Ordenar não pode CONSUMIR a lista de quem chamou (a tela também conta e filtra sobre ela).
igual(lista.map((f) => f.id), ["a", "b", "c", "d"], "ordenarFontes não muta a lista recebida");
igual(contarFalhas(lista), 2, "conta só as fontes com falha na última execução");
igual(contarFalhas([]), 0, "lista vazia não tem falha");

// --- 6: quarentena ------------------------------------------------------------
// Sem quarentena a coluna NÃO EXISTE: `pillQuarentena` devolve null e o `GridLista` monta as
// colunas pela união dos rótulos, então uma coluna inteira de "—" nunca chega a nascer.
ok(pillQuarentena(undefined) === null, "fonte sem resumo de quarentena não ganha coluna");
ok(pillQuarentena(null) === null, "resumo nulo não ganha coluna");
ok(
  pillQuarentena({ fonte_id: "x", total: 0, fatias_sem_lote_bom: 0 }) === null,
  "quarentena zerada não ganha coluna",
);

// Quarentena RESOLVIDA (toda fatia acabou pousando no bronze) é ruído: pill NEUTRO, nunca vermelho
// — e nunca `ok`, porque verde significa "terminou bem" em todo o produto e quarentena não é isso.
const resolvida = pillQuarentena({ fonte_id: "x", total: 12, fatias_sem_lote_bom: 0 });
igual(resolvida.cls, "off", "quarentena resolvida é pill neutro");
ok(resolvida.cls !== "ok", "quarentena nunca usa a classe do verde 'terminou bem'");
igual(resolvida.texto, "quarentena 12", "resolvida não anuncia buraco que não existe");
ok(
  resolvida.titulo.includes("já resolvida"),
  "o title diz que a quarentena resolvida não pede investigação",
);

// `fatias_sem_lote_bom` ausente (servidor antigo, campo renomeado) NÃO pode virar vermelho por
// acidente: a tela só acusa buraco quando o servidor afirma que há buraco.
igual(
  pillQuarentena({ fonte_id: "x", total: 12 }).cls,
  "off",
  "campo ausente não inventa buraco no dado",
);

// Fatia que caiu na quarentena e NUNCA pousou no bronze é dado que não existe: aí sim vermelho.
const buraco = pillQuarentena({ fonte_id: "x", total: 12, fatias_sem_lote_bom: 3 });
igual(buraco.cls, "falha", "fatia sem lote bom no bronze pinta vermelho");
igual(buraco.texto, "quarentena 12 · 3 sem bronze", "o selo diz o total e o quanto é buraco");

// Números grandes saem em pt-BR (separador de milhar), como no resto do produto.
igual(
  pillQuarentena({ fonte_id: "x", total: 5907, fatias_sem_lote_bom: 1 }).texto,
  "quarentena 5.907 · 1 sem bronze",
  "contagem em pt-BR e singular de 'sem bronze'",
);

// O `title` carrega o que o selo não cabe: desde quando, recência e os códigos mais frequentes.
const titulo = tituloQuarentena({
  fonte_id: "x",
  total: 2,
  ultimos_7d: 1,
  primeiro_em: "2026-08-15T12:00:00+00:00",
  ultimo_em: "2026-08-16T12:00:00+00:00",
  codigos: [{ codigo: "parse_falhou", contagem: 2 }],
  fatias_sem_lote_bom: 2,
});
ok(titulo.includes("2 linhas na quarentena"), "o title abre com o total");
ok(titulo.includes("1 nos últimos 7 dias"), "o title traz a recência");
ok(titulo.includes("parse_falhou (2)"), "o title traz o código mais frequente");
ok(titulo.includes("15/08/2026"), "o title data o primeiro registro em America/Sao_Paulo");
ok(titulo.includes("não existe em lugar nenhum"), "o title diz o que o buraco significa");

// Carimbo ilegível não vira "Invalid Date" no title — a mesma armadilha que derrubou a linha de
// Agendamentos (`diaSP` só devolve null quando a formatação LANÇA, e `new Date("vazio")` não lança).
const tituloRuim = tituloQuarentena({
  fonte_id: "x",
  total: 1,
  primeiro_em: "vazio",
  ultimo_em: null,
  fatias_sem_lote_bom: 0,
});
ok(!tituloRuim.includes("Invalid"), "carimbo ilegível é omitido, não impresso como Invalid Date");
igual(tituloQuarentena({ fonte_id: "x", total: 1 }).includes("1 linha na quarentena"), true, "singular de linha");

// Índice por fonte: é ele que faz cada uma das 54 linhas achar o seu resumo sem varrer a lista.
const resumos = [
  { fonte_id: "a", total: 4, fatias_sem_lote_bom: 0 },
  { fonte_id: "b", total: 9, fatias_sem_lote_bom: 2 },
  { fonte_id: "c", total: 1, fatias_sem_lote_bom: 1 },
];
igual(indexarQuarentena(resumos).b.total, 9, "índice acha o resumo pela fonte");
ok(indexarQuarentena(resumos).zzz === undefined, "fonte sem quarentena não aparece no índice");
igual(indexarQuarentena([]), {}, "lista vazia devolve índice vazio");
// Contagens da faixa: fontes com buraco e fatias sem bronze são números DIFERENTES, e é o segundo
// que dimensiona o estrago.
igual(contarFontesComBuraco(resumos), 2, "conta as fontes com fatia sem lote bom");
igual(contarFatiasSemBronze(resumos), 3, "soma as fatias que nunca pousaram no bronze");
igual(contarFontesComBuraco([{ fonte_id: "a", total: 4 }]), 0, "campo ausente não conta como buraco");
igual(contarFatiasSemBronze([]), 0, "lista vazia não tem buraco");

// Sub-linha do drill-down: fatia, etapa e código. Campo ausente é DECLARADO, nunca vazio — numa
// linha de DLQ, "não registrado" e "vazio" são achados diferentes.
igual(
  metaQuarentena({ id: "1", fonte_id: "a", chave_lote: "2026-08", etapa: "parse", codigo: "parse_falhou" }),
  "2026-08 · parse · parse_falhou",
  "meta da linha de quarentena na ordem fatia · etapa · código",
);
igual(
  metaQuarentena({ id: "1", fonte_id: "a" }),
  "fatia sem chave · etapa não registrada",
  "ausência é declarada, não impressa como vazio",
);

// --- abas por classe (tela "Fontes de Ingestão") -------------------------------
const fontesPorClasse = [
  { classe: "http_file" },
  { classe: "http_file" },
  { classe: "rest_api" },
  { classe: "odata" },
];
const abasClasse = abasDeClasse(fontesPorClasse);
igual(
  abasClasse.map((a) => a.chave),
  ["", "http_file", "rest_api", "odata"],
  "abas seguem a ordem fixa do contrato; 'portal_download' ausente não aparece (sem uso hoje)",
);
igual(abasClasse[0].contagem, 4, "aba Todas soma todas as fontes");
igual(abasClasse[1].contagem, 2, "contagem da aba bate com o predicado (mesma conta, sem divergir)");
ok(
  daAbaClasse({ classe: "http_file" }, "http_file", ["", "http_file", "rest_api"]),
  "fonte da classe filtrada passa no predicado",
);
ok(
  !daAbaClasse({ classe: "rest_api" }, "http_file", ["", "http_file", "rest_api"]),
  "fonte de outra classe não passa",
);
ok(
  daAbaClasse({ classe: "rest_api" }, "xyz-nao-existe", ["", "http_file", "rest_api"]),
  "aba desconhecida (link antigo) cai em Todas, não numa lista vazia",
);
// Classe nova do backend não some da régua de abas — entra ordenada no fim.
igual(
  abasDeClasse([{ classe: "http_file" }, { classe: "ftp_batch" }]).map((a) => a.chave),
  ["", "http_file", "ftp_batch"],
  "classe fora da ordem conhecida entra no fim, não desaparece",
);

// --- formulário de contrato ↔ JSON ---------------------------------------------
// Ida e volta sem perda: um contrato salvo, relido, tem que reabrir com os MESMOS campos — senão
// editar uma fonte existente mostraria um formulário diferente do que está de fato salvo.
const contratoHttpFile = {
  classe: "http_file",
  dataset: "cvm_inf_diario_fi",
  descricao: "Informe diário de fundos (CVM)",
  cadencia: "diaria",
  janela: { tipo: "mensal", inicio: "2024-01" },
  acesso: {
    url_padrao: "https://dados.cvm.gov.br/inf_diario_fi_{ano}{mes}.zip",
    condicional: true,
  },
  formato: {
    tipo: "csv",
    encoding: "latin-1",
    delimitador: ";",
    compressao: "zip",
    arquivo_no_zip: "^inf_diario_fi_.*\\.csv$",
  },
  schema_esperado: ["CNPJ_FUNDO_CLASSE", "DT_COMPTC", "VL_QUOTA"],
  grao: { estado: "desconhecido", chaves: [] },
  limites: { timeout_segundos: 180, concorrencia: 4, tentativas: 3 },
};
const formHttpFile = formDeContrato(contratoHttpFile);
igual(formHttpFile.dataset, "cvm_inf_diario_fi", "dataset preenche o formulário ao reabrir");
igual(formHttpFile.urlPadrao, contratoHttpFile.acesso.url_padrao, "url_padrao preenche o formulário");
igual(formHttpFile.schemaEsperado, "CNPJ_FUNDO_CLASSE\nDT_COMPTC\nVL_QUOTA", "schema vira uma coluna por linha");
const voltaHttpFile = contratoDeForm(formHttpFile);
igual(voltaHttpFile.dataset, contratoHttpFile.dataset, "ida e volta preserva o dataset");
igual(voltaHttpFile.acesso, contratoHttpFile.acesso, "ida e volta preserva o bloco acesso (http_file)");
igual(voltaHttpFile.formato, contratoHttpFile.formato, "ida e volta preserva o bloco formato");
igual(voltaHttpFile.schema_esperado, contratoHttpFile.schema_esperado, "ida e volta preserva schema_esperado");
// grao desconhecido é o DEFAULT — não sai no JSON de volta (mesma regra do backend: ausência = desconhecido).
ok(voltaHttpFile.grao === undefined, "grão desconhecido não vaza campo vazio no contrato salvo");

// grão DECLARADO com desempate — o caso do issue #26 (CRA: reapresentação, resolve por data_entrega).
const contratoComDesempate = {
  classe: "http_file",
  dataset: "cvm_cra_geral",
  cadencia: "mensal",
  janela: { tipo: "mensal", inicio: "2024-01" },
  acesso: { url_padrao: "https://dados.cvm.gov.br/cra.zip" },
  formato: { tipo: "csv", encoding: "latin-1" },
  grao: {
    estado: "declarado",
    chaves: ["cnpj_emissora", "codigo_identificacao_certificado"],
    desempate: { colunas: [{ coluna: "data_entrega", ordem: "desc" }], motivo: "reapresentação — vale a última" },
  },
};
const formDesempate = formDeContrato(contratoComDesempate);
igual(formDesempate.graoEstado, "declarado", "grão declarado preenche o formulário");
igual(formDesempate.graoChaves, "cnpj_emissora\ncodigo_identificacao_certificado", "chaves do grão viram lista");
igual(formDesempate.graoDesempateColunas, "data_entrega:desc", "desempate vira 'coluna:ordem' por linha");
const voltaDesempate = contratoDeForm(formDesempate);
igual(voltaDesempate.grao, contratoComDesempate.grao, "ida e volta preserva grão declarado + desempate");

// grão SEM_CHAVE exige motivo — mesma disciplina do backend (descartar sem dizer por quê é o que o
// campo existe para impedir).
const formSemChave = { ...estadoInicialContrato("odata"), dataset: "bcb_pix_liquidados", graoEstado: "sem_chave" };
const semMotivo = contratoDeForm(formSemChave);
ok(semMotivo.grao.motivo === undefined, "sem_chave sem motivo preenchido não inventa motivo");
const comMotivo = contratoDeForm({ ...formSemChave, graoMotivo: "evento repetível, sem identidade" });
igual(comMotivo.grao, { estado: "sem_chave", chaves: [], motivo: "evento repetível, sem identidade" }, "sem_chave com motivo sai completo");

// rest_api — campos próprios da classe não vazam pro contrato de outra classe.
const formSgs = {
  ...estadoInicialContrato("rest_api"),
  dataset: "bcb_sgs_selic",
  cadencia: "manual",
  janelaTipo: "periodo",
  janelaInicio: "2016-01-01",
  passoMaxDias: "3650",
  urlPadrao: "https://api.bcb.gov.br/dados/serie/bcdata.sgs.11/dados",
  paramInicio: "dataInicial",
  paramFim: "dataFinal",
  formatoData: "%d/%m/%Y",
  formatoTipo: "json",
};
const contratoSgs = contratoDeForm(formSgs);
igual(contratoSgs.acesso.param_inicio, "dataInicial", "rest_api leva param_inicio");
ok(contratoSgs.acesso.pagina_indice === undefined, "campo de portal_download não vaza pro contrato rest_api");
igual(contratoSgs.janela.passo_max_dias, 3650, "passo_max_dias numérico, só em janela periodo");

// odata (BCB Olinda PTAX) — paginar:false é o caso do PixLiquidadosAtual (serviço que rejeita $skip).
const formOdataSemPaginar = { ...estadoInicialContrato("odata"), dataset: "bcb_pix", paginar: false };
igual(contratoDeForm(formOdataSemPaginar).acesso.paginar, false, "paginar:false sai explícito no contrato");
const formOdataComPaginar = { ...estadoInicialContrato("odata"), dataset: "bcb_ptax" };
ok(
  contratoDeForm(formOdataComPaginar).acesso.paginar === undefined,
  "paginar (default true) não polui o contrato quando não foi desligado",
);

console.log(falhas === 0 ? "OK — nenhuma falha" : `${falhas} falha(s)`);
process.exit(falhas === 0 ? 0 : 1);
