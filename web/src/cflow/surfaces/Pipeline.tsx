import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useDispatch, useSelector } from "react-redux";
import { useNavigate, useSearchParams } from "react-router-dom";

import * as api from "../../api/client";
import type { Conexao, DerivacaoParametro, ParametroPipeline } from "../../api/client";
import { definicaoComParametros, parametroParaSalvar } from "../definicao";
import { extrairRefs, parametrosAutomaticos, refCobertaPor, textosDoNo } from "../nos/deteccao";
import { setBaseNumero, setErro, setPipeline, type RootState } from "../../store/store";
import { SeletorVersao } from "../SeletorVersao";
import StudioGraph, { type StudioGraphHandle } from "../StudioGraph";
import {
  campoCamada,
  campoSistema,
  ConfirmarAcao,
  Icon,
  Interruptor,
  ListaCadastro,
  type ItemCadastro,
} from "../ui";
import { execucoesDaPagina } from "./cadeias";
import {
  ABA_PADRAO,
  ABAS_CAMADA,
  abaValida,
  contarPorAba,
  naAba,
} from "./camadas";
import { CarimboEscopo } from "./CarimboEscopo";
import { FontesIngestao } from "./FontesIngestao";
import { diaSP, fmtHora, pillExecucao, rotuloDia } from "../execucao/formato";

// Tipos de parâmetro suportados pelo runtime (referenciados no SQL dos nós como `:nome`).
// "competencia" (mês) é especial: NÃO injeta `:nome`, e sim a janela `:nome_inicio`/`:nome_fim`.
const TIPOS_PARAMETRO: [ParametroPipeline["tipo"], string][] = [
  ["data", "data"],
  ["texto", "texto"],
  ["inteiro", "inteiro"],
  ["opcoes", "opções"],
  ["competencia", "competência (mês)"],
  // as_of: corte de tempo de SISTEMA (data_registro). Injeta `:nome` direto (como data/texto);
  // valor = token ("hoje"/"ontem"/"2026-05"), data ou timestamp. Ver o dado "como estava".
  ["as_of", "as-of (data de registro)"],
];

// ============================================================================
// LINT de parâmetros — confere os parâmetros DECLARADOS contra as referências
// `:nome` USADAS pelos nós. Função PURA (sem efeitos): recebe os `nos` do grafo
// SALVO + os `parametros` em edição, e devolve a lista de avisos.
//
// Checagens (as três primeiras espelham o que o BACKEND recusa no salvar —
// `cflow_core.pipelines.definicao._validar_parametros`; melhor dizer aqui do que
// devolver 400 depois de a pessoa ter preenchido tudo):
//   1) NOME_INVALIDO          — fora de `^[a-zA-Z_][a-zA-Z0-9_]*$`.
//   2) NOME_DUPLICADO         — dois parâmetros com o mesmo nome.
//   3) CREDENCIAL_SEM_MARCA   — nome que aparenta credencial sem `segredo: true`.
//   4) OBRIGATORIO_COM_DEFAULT— obrigatório COM padrão: o backend usa o padrão
//      ANTES de cobrar o valor (`definicao.aplicar_parametros`), então "em branco"
//      nunca vira erro e o padrão define o escopo em silêncio — exatamente o
//      resultado plausível-e-errado que a regra de parâmetro de escopo proíbe.
//   5) USADO_NAO_DECLARADO    — `:ref` num nó sem parâmetro correspondente.
//   6) DECLARADO_NAO_USADO    — parâmetro que nenhum nó referencia.
//
// As duas últimas dependem de CONHECER o grafo: com a leitura em voo ou falha,
// `grafoConhecido=false` as desliga — afirmar "nenhum nó usa" sem ter lido o
// grafo é a tela inventando um fato.
//
// Convenções toleradas ao casar `:ref` com um param:
//   • `::cast` (PostgreSQL) NÃO é referência — o regex ignora `:` precedido de `:`.
//   • Competência: um param `X` cobre também `:X_inicio` e `:X_fim` (intervalo do mês).
// ============================================================================

export type GravidadeLint = "erro" | "aviso";
export interface AvisoLint {
  codigo:
    | "usado_nao_declarado"
    | "declarado_nao_usado"
    | "obrigatorio_com_default"
    | "credencial_sem_marca"
    | "nome_invalido"
    | "nome_duplicado"
    | "sem_nome"
    | "default_invalido";
  gravidade: GravidadeLint;
  alvo: string; // nome do parâmetro ou da referência envolvida
  mensagem: string;
  // Índice do parâmetro na lista editada, quando o aviso é DE um parâmetro. Serve para o resumo
  // levar ao campo que se corrige (com 8 parâmetros a lista do topo fica fora de vista) e para
  // acusar o bloco de um parâmetro que ainda não tem nome (`alvo` vazio não casa por nome).
  idx?: number;
}

// Cópias fiéis dos padrões do backend (`definicao._NOME_PARAM` / `_NOME_SEGREDO`): divergir aqui
// faria a tela aprovar o que o salvar recusa (ou o contrário).
const NOME_PARAM_OK = /^[a-zA-Z_][a-zA-Z0-9_]*$/;
const NOME_PARECE_SEGREDO =
  /(senha|password|passwd|pwd|secret|segredo|token|api[_-]?key|apikey|credencial|credential)/i;
// Contrato dos valores por tipo, também copiado do backend: competência é ESTRITA (só mês —
// `definicao._competencia_janela`); as-of aceita timestamp, data, 'YYYY-MM', intervalo e os tokens
// de período (`periodos.resolver_corte`/`resolver_janela`). Divergir aqui faria a tela aprovar o
// que o salvar recusa.
const COMPETENCIA_OK = /^(atual|anterior|\d{4}-(0[1-9]|1[0-2]))$/;
const AS_OF_OK =
  /^(hoje|ontem|atual|anterior|mes_atual|mes_anterior|semana_atual|semana_anterior|ultimos_7_dias|ultimos_30_dias|\d{4}-(0[1-9]|1[0-2])|\d{4}-\d{2}-\d{2}(\.\.\d{4}-\d{2}-\d{2})?|\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)$/;

export function temValor(v: unknown): boolean {
  // `""` é "sem valor", não o valor `""` (regra de parâmetro: em branco = sem filtro).
  return v !== undefined && v !== null && String(v) !== "";
}

// Todas as refs `:nome` de UM nó. `textosDoNo` cobre fonte (`config.sql`), nó SQL (`sql`) E a
// condição da operação guiada (`config.op.condicao`) — o lint só olhava os dois primeiros e
// acusava de "não usado" o parâmetro que só aparecia numa operação guiada, onde `:nome` vale
// igual. É a mesma leitura da autodeclaração no salvar.
export function refsDoNoSalvo(no: any): string[] {
  return extrairRefs(textosDoNo(no).join("\n"));
}

// `extrairRefs`/`refCobertaPor` moraram aqui; agora vivem em `nos/deteccao.ts` (reusados pela
// autodeclaração de parâmetros no save do grafo/code area, ver `parametrosAutomaticos`).

export function lintarParametros(
  nos: any[],
  parametros: ParametroPipeline[],
  grafoConhecido = true,
): AvisoLint[] {
  const lista = parametros ?? [];
  const params = lista.filter((p) => typeof p?.nome === "string" && p.nome.trim() !== "");
  const nomesDeclarados = new Set(params.map((p) => p.nome.trim()));

  // Todas as refs `:nome` usadas por qualquer nó (deduplicadas).
  const refsUsadas = new Set<string>();
  for (const no of nos ?? []) {
    for (const ref of refsDoNoSalvo(no)) refsUsadas.add(ref);
  }

  const avisos: AvisoLint[] = [];
  const jaVistos = new Set<string>();

  lista.forEach((p, idx) => {
    const nome = typeof p?.nome === "string" ? p.nome.trim() : "";

    // Parâmetro SEM NOME é descartado no salvar (`salvarParametros` filtra) — e a lista dizia
    // "nada a corrigir" enquanto o contador já o somava. Trabalho perdido em silêncio.
    if (nome === "") {
      avisos.push({
        codigo: "sem_nome",
        gravidade: "erro",
        alvo: "",
        idx,
        mensagem: `O ${idx + 1}º parâmetro da lista está sem nome — ele NÃO é salvo. Dê um nome (ou remova o bloco).`,
      });
      return;
    }

    if (!NOME_PARAM_OK.test(nome)) {
      avisos.push({
        codigo: "nome_invalido",
        gravidade: "erro",
        alvo: nome,
        idx,
        mensagem: `«${nome}» não é um nome válido: use letras, números e _ (começando por letra ou _). O salvar é recusado assim.`,
      });
    }
    if (jaVistos.has(nome)) {
      avisos.push({
        codigo: "nome_duplicado",
        gravidade: "erro",
        alvo: nome,
        idx,
        mensagem: `«${nome}» está declarado mais de uma vez — o salvar é recusado.`,
      });
    }
    jaVistos.add(nome);

    if (NOME_PARECE_SEGREDO.test(nome) && !p.segredo) {
      avisos.push({
        codigo: "credencial_sem_marca",
        gravidade: "erro",
        alvo: nome,
        idx,
        mensagem: `«${nome}» aparenta ser credencial: marque «credencial», senão o valor fica em claro na trilha (que é permanente) e o salvar é recusado.`,
      });
    }

    if (p.obrigatorio && !p.derivacao && temValor(p.default)) {
      avisos.push({
        codigo: "obrigatorio_com_default",
        gravidade: "erro",
        alvo: nome,
        idx,
        mensagem: `«${nome}» é obrigatório e tem valor padrão: o padrão entra primeiro, então em branco nunca dá erro e o escopo fica definido em silêncio. Tire o padrão ou desmarque «obrigatório».`,
      });
    }

    // VALOR PADRÃO × TIPO. O backend já recusa o salvar de um padrão fora do contrato do tipo
    // (`_validar_parametros`) — e recusa a definição INTEIRA, nós inclusive. Dizer aqui evita
    // descobrir isso num 422 depois de preencher tudo. `data` o backend aceita, mas o campo do
    // Executar é `type=date`: fora de ISO ele abre EM BRANCO, e o padrão vira letra morta.
    if (temValor(p.default) && !p.segredo) {
      const d = String(p.default).trim();
      const ruim = (msg: string) =>
        avisos.push({ codigo: "default_invalido", gravidade: "erro", alvo: nome, idx, mensagem: msg });
      if (p.tipo === "inteiro" && !/^-?\d+$/.test(d))
        ruim(`«${nome}» é inteiro e o valor padrão «${d}» não é um número inteiro — o salvar é recusado.`);
      else if (p.tipo === "competencia" && !COMPETENCIA_OK.test(d))
        ruim(`«${nome}»: valor padrão «${d}» não é competência — use atual, anterior ou YYYY-MM. O salvar é recusado.`);
      else if (p.tipo === "as_of" && !AS_OF_OK.test(d))
        ruim(`«${nome}» (as-of): valor padrão «${d}» não é data/hora nem token de período (hoje, ontem, YYYY-MM, YYYY-MM-DD). O salvar é recusado.`);
      else if (p.tipo === "opcoes" && !p.opcoes_fonte && (p.opcoes ?? []).length > 0 && !(p.opcoes ?? []).includes(d))
        ruim(`«${nome}»: valor padrão «${d}» não está entre as opções declaradas — o salvar é recusado.`);
      else if (p.tipo === "data" && !/^\d{4}-\d{2}-\d{2}$/.test(d))
        avisos.push({
          codigo: "default_invalido",
          gravidade: "aviso",
          alvo: nome,
          idx,
          mensagem: `«${nome}» é data e o valor padrão «${d}» não está em AAAA-MM-DD: o campo de data do Executar abre em branco (o padrão só vale para quem chama a API).`,
        });
    }
    // Preview fora das opções: o modal do Executar pré-preenche com ele e o `select` não casa —
    // abre em branco sem dizer por quê.
    if (
      p.tipo === "opcoes" &&
      !p.opcoes_fonte &&
      temValor(p.valor_preview) &&
      (p.opcoes ?? []).length > 0 &&
      !(p.opcoes ?? []).includes(String(p.valor_preview).trim())
    ) {
      avisos.push({
        codigo: "default_invalido",
        gravidade: "aviso",
        alvo: nome,
        idx,
        mensagem: `«${nome}»: o valor de preview «${String(p.valor_preview).trim()}» não está entre as opções — o campo abre em branco no Executar.`,
      });
    }
  });

  if (grafoConhecido) {
    for (const ref of [...refsUsadas].sort()) {
      if (!refCobertaPor(ref, nomesDeclarados)) {
        avisos.push({
          codigo: "usado_nao_declarado",
          gravidade: "erro",
          alvo: ref,
          mensagem: `:${ref} é usado por um nó e não está declarado. Ao salvar, ele é criado como «texto» opcional — declare-o antes com o tipo certo.`,
        });
      }
    }
    lista.forEach((p, idx) => {
      const nome = typeof p?.nome === "string" ? p.nome.trim() : "";
      if (nome === "") return;
      // "Usado" = aparece direto OU via convenção de competência (X_inicio / X_fim).
      const usado =
        refsUsadas.has(nome) || refsUsadas.has(`${nome}_inicio`) || refsUsadas.has(`${nome}_fim`);
      if (!usado) {
        avisos.push({
          codigo: "declarado_nao_usado",
          gravidade: "aviso",
          alvo: nome,
          idx,
          mensagem: `«${nome}» não é referenciado por nenhum nó do grafo salvo — use :${nome} na fonte, no SQL ou na condição de uma operação guiada.`,
        });
      }
    });
  }

  return avisos;
}

// Detalhe de um pipeline já carregado (metadados do cabeçalho).
interface DetalheCarregado {
  nome: string;
  nos: number;
  fontes: number;
  ativo: boolean;
}

export default function Pipeline() {
  const dispatch = useDispatch();
  const navigate = useNavigate();
  // Publicar é ação irreversível e auditada: passa por confirmação com nota de mudança.
  const [modalPublicar, setModalPublicar] = useState(false);
  // Desativar bloqueia EXECUÇÃO em todo gatilho (só desativar confirma — ligar de volta não é a
  // direção perigosa, mesmo padrão de Agendamentos/GridLista).
  const [pedirDesativar, setPedirDesativar] = useState(false);
  const [tentouExec, setTentouExec] = useState(false);
  const [salvando, setSalvando] = useState(false);
  const { pipelineId, baseNumero } = useSelector((s: RootState) => s.studio);

  // null = listagem; string (id ou "__novo__") = detalhe aberto.
  const [aberto, setAberto] = useState<string | null>(null);
  const [listaRaw, setListaRaw] = useState<api.PipelineResumo[]>([]);
  const [ehAdmin, setEhAdmin] = useState(false);
  const [sistemaBusy, setSistemaBusy] = useState<string | null>(null);
  // Carimbo de obsoleto do ponteiro de escopo: o pipeline cuja saída lake se vai tratar.
  const [carimbo, setCarimbo] = useState<{ id: string; nome: string } | null>(null);
  const [carregandoLista, setCarregandoLista] = useState(true);
  const [erroLista, setErroLista] = useState<string | null>(null);

  // Querystring: guarda o deep-link `?abrir=<id>` (consumido no mount) E a aba de filtro `?aba=`.
  // A aba NÃO é `useState` de propósito — filtro de lista precisa ser linkável e sobreviver ao
  // voltar do navegador (regra do projeto); em estado local ela se perde nos dois casos.
  const [paramsUrl, setParamsUrl] = useSearchParams();
  const aba = abaValida(paramsUrl.get("aba"));
  const trocarAba = (chave: string) => {
    const p = new URLSearchParams(paramsUrl);
    // A aba padrão sai da URL: `?aba=geral` é ruído no link que se copia. A leitura tolera as duas
    // formas, então um link com `?aba=geral` continua abrindo em Geral.
    if (chave === ABA_PADRAO) p.delete("aba");
    else p.set("aba", chave);
    setParamsUrl(p, { replace: true });
  };

  const [detalhe, setDetalhe] = useState<DetalheCarregado | null>(null);
  const [erroDetalhe, setErroDetalhe] = useState<string | null>(null);

  // Handle imperativo do grafo (publicar usa a definição ATUAL do canvas).
  const studioRef = useRef<StudioGraphHandle>(null);
  // Amostra da prévia em massa: `null` = fechado; senão o resumo lido do canvas ao abrir.
  const [modalAmostra, setModalAmostra] = useState<{ nos: number; valores: number[] } | null>(null);
  const [avisoAmostra, setAvisoAmostra] = useState<string | null>(null);
  const [erroAmostra, setErroAmostra] = useState<string | null>(null);

  // --- Parâmetros do pipeline (definicao.parametros) ---
  // O painel lê/edita os parâmetros e os persiste pelo MESMO rascunho do grafo:
  // ao salvar, recarrega nós/arestas do pipeline e regrava a definição com os
  // parâmetros editados (não mexe nos NÓS que o StudioGraph mantém em memória).
  // Este estado é a ÚNICA cópia: o StudioGraph o recebe por prop (`parametros`/`onParametros`).
  // Antes ele mantinha uma cópia própria, carregada na abertura — salvar o grafo (ou Executar,
  // que salva antes) regravava essa cópia velha e apagava o que a aba tinha acabado de salvar.
  const [parametros, setParametros] = useState<ParametroPipeline[]>([]);
  // Aba do editor: Grafo (canvas) | Parâmetros. O StudioGraph fica sempre montado (só escondido)
  // p/ não perder o estado do canvas ao trocar de aba. (Bibliotecas viraram fx por nó.)
  const [abaEditor, setAbaEditor] = useState<"grafo" | "parametros">("grafo");
  // Versões do pipeline: dropdown no cabeçalho. `versaoSel` = a versão CARREGADA no canvas
  // (null = a última). Selecionar uma anterior carrega-a; Executar/Publicar agem sobre ela.
  const [versoes, setVersoes] = useState<api.VersaoPipeline[]>([]);
  const [versaoSel, setVersaoSel] = useState<number | null>(null);
  // Por que a lista de versões não veio. Some com o controle era a pior forma de comunicar isso.
  const [erroVersoes, setErroVersoes] = useState<string | null>(null);
  // `sujo` = canvas com edição não salva. Executar só cria versão nova SE houve edição; rodar sem
  // mudança roda a versão vigente/selecionada (não gera versão).
  const [sujo, setSujo] = useState(false);
  // Modal de execução: coleta valores dos parâmetros declarados antes de rodar (build screen).
  const [modalExec, setModalExec] = useState(false);
  const [valoresExec, setValoresExec] = useState<Record<string, string>>({});
  const [salvandoParams, setSalvandoParams] = useState(false);
  const [okParams, setOkParams] = useState(false);
  const [erroParams, setErroParams] = useState<string | null>(null);
  // Nomes de parâmetro autodeclarados (a partir de `:nome` no SQL) no último save — do code area
  // (StudioGraph) OU da aba Parâmetros. Aparece sempre (independe da aba aberta).
  const [criadosAuto, setCriadosAuto] = useState<string[] | null>(null);
  // Incrementa a cada save do grafo (toolbar OU "Salvar" do code area) — o lint de parâmetros
  // (PainelParametros) usa isto p/ saber quando refazer o fetch dos nós SALVOS.
  const [grafoSalvoEm, setGrafoSalvoEm] = useState(0);
  // Conexões `arquivo` disponíveis como mapa de referência (select da derivação).
  const [conexoes, setConexoes] = useState<Conexao[]>([]);

  // --- Botão único Executar/Publicar (com dependência) ---
  // dirty = houve mudança desde a última execução bem-sucedida → botão volta a "Executar".
  // publicavel = última execução teve sucesso e nada mudou depois → botão vira "Publicar".
  const [executando, setExecutando] = useState(false);
  const [publicando, setPublicando] = useState(false);
  const [publicavel, setPublicavel] = useState(false);
  const [resultadoExec, setResultadoExec] = useState<any>(null);
  const [erroExec, setErroExec] = useState<string | null>(null);
  // 409 `execucao_em_andamento`: a execução que já está rodando com estes parâmetros.
  const [execEmVoo, setExecEmVoo] = useState<string | null>(null);

  // Qualquer edição (grafo OU parâmetros) reverte o botão para "Executar".
  const marcarSujo = useCallback(() => {
    setPublicavel(false);
    setResultadoExec(null);
    setOkParams(false);
    setSujo(true);
  }, []);

  // Edição na aba de Parâmetros: avisa o canvas para uma carga em voo não sobrescrever o que
  // acabou de ser digitado (o grafo já protege os NÓS assim; os parâmetros ficavam de fora).
  const marcarSujoParams = useCallback(() => {
    studioRef.current?.registrarEdicaoExterna();
    marcarSujo();
  }, [marcarSujo]);

  // Contagens vindas do grafo (nós/fontes) — atualizadas em tempo real.
  const aplicarMeta = useCallback((m: { nos: number; fontes: number }) => {
    setDetalhe((prev) =>
      prev ? { ...prev, ...m } : { nome: "Novo pipeline", ativo: true, ...m },
    );
  }, []);

  // Última execução por pipeline: a lista dizia só "abrir para editar o grafo" em toda linha — o
  // especialista tinha de abrir cada pipeline para saber se rodou, quando e se falhou.
  const [ultimaExec, setUltimaExec] = useState<Record<string, api.ExecucaoItem>>({});
  const [execsIndisponiveis, setExecsIndisponiveis] = useState(false);

  const recarregarUltimas = useCallback(async () => {
    try {
      setExecsIndisponiveis(false);
      const pg = await api.listarExecucoes({ limite: 100 });
      const mapa: Record<string, api.ExecucaoItem> = {};
      // A resposta é de CORRIDAS (cada item traz a raiz e as dependências do mesmo disparo):
      // achata para execuções, em ordem decrescente de início — a PRIMEIRA de cada pipeline é a
      // última que rodou. Sem achatar, o pipeline que só roda como dependência de outro nunca
      // apareceria com a data da última execução.
      for (const e of execucoesDaPagina(pg.itens)) if (!mapa[e.pipeline_id]) mapa[e.pipeline_id] = e;
      setUltimaExec(mapa);
    } catch {
      // Falha ao buscar as execuções NÃO é "nunca executado": afirmar isso em toda linha é a
      // tela inventando um fato. Sem o dado, a linha simplesmente não fala do assunto.
      setUltimaExec({});
      setExecsIndisponiveis(true);
    }
  }, []);

  // --- Listagem ---
  const recarregarLista = async () => {
    setCarregandoLista(true);
    setErroLista(null);
    try {
      const pipes = await api.listarPipelines();
      setListaRaw(pipes);
      void recarregarUltimas();
    } catch (e: any) {
      setErroLista(e?.message ?? "Falha ao carregar pipelines.");
    } finally {
      setCarregandoLista(false);
    }
  };

  // Descobre se o usuário é admin (mostra/oculta o toggle "Sistema"). O backend reforça.
  useEffect(() => {
    void api.meAtual().then((u) => setEhAdmin(u.perfil === "admin")).catch(() => setEhAdmin(false));
  }, []);

  // Liga/desliga a marca de sistema (otimista, reverte no erro — padrão de Conexões).
  const alternarSistema = async (id: string, sistema: boolean) => {
    setErroLista(null);
    setSistemaBusy(id);
    setListaRaw((l) => l.map((p) => (p.id === id ? { ...p, sistema } : p)));
    try {
      await api.definirSistema("pipelines", id, sistema);
    } catch (e: any) {
      setListaRaw((l) => l.map((p) => (p.id === id ? { ...p, sistema: !sistema } : p)));
      setErroLista(e?.message ?? "Falha ao alterar a marca de sistema.");
    } finally {
      setSistemaBusy(null);
    }
  };

  // Define/remove a camada (otimista, reverte no erro — mesmo padrão de `alternarSistema`). A
  // camada ANTERIOR é guardada antes do update: um booleano se reverte com `!sistema`, um enum de
  // quatro estados não — sem guardar, o rollback teria de adivinhar de onde o item veio.
  const alternarCamada = async (id: string, camada: api.Camada | null) => {
    setErroLista(null);
    const anterior = listaRaw.find((p) => p.id === id)?.camada ?? null;
    setListaRaw((l) => l.map((p) => (p.id === id ? { ...p, camada } : p)));
    try {
      await api.definirCamada(id, camada);
    } catch (e: any) {
      setListaRaw((l) => l.map((p) => (p.id === id ? { ...p, camada: anterior } : p)));
      setErroLista(e?.message ?? "Falha ao alterar a camada.");
    }
  };

  // Liga/desliga a EXECUÇÃO do pipeline (otimista, reverte no erro — mesmo padrão de
  // `alternarCamada`). Bloqueia disparo em TODO gatilho (UI, agendador cron, reexecução) — o
  // backend checa no choke-point único de `engine.criar_execucao`, não só na rota de executar.
  const alternarAtivo = async (id: string, ativo: boolean) => {
    setErroLista(null);
    setListaRaw((l) => l.map((p) => (p.id === id ? { ...p, ativo } : p)));
    if (detalhe && aberto === id) setDetalhe((d) => (d ? { ...d, ativo } : d));
    try {
      await api.definirAtivo(id, ativo);
    } catch (e: any) {
      setListaRaw((l) => l.map((p) => (p.id === id ? { ...p, ativo: !ativo } : p)));
      if (detalhe && aberto === id) setDetalhe((d) => (d ? { ...d, ativo: !ativo } : d));
      setErroLista(e?.message ?? "Falha ao alterar o estado do pipeline.");
    }
  };

  const contagens = useMemo(() => contarPorAba(listaRaw), [listaRaw]);
  // Desativado vai para o FIM da lista — quem abre a tela quer ver primeiro o que roda; o switch
  // na linha já diz o estado, então afundar (não esconder) o desativado é o suficiente. `.sort` é
  // estável (spec desde ES2019): dentro de cada grupo a ordem original (de criação) se mantém.
  const listaFiltrada = useMemo(
    () =>
      listaRaw
        .filter((p) => naAba(p, aba))
        .slice()
        .sort((a, b) => Number(a.ativo === false) - Number(b.ativo === false)),
    [listaRaw, aba],
  );

  const itens: ItemCadastro[] = useMemo(
    () =>
      listaFiltrada.map((p) => {
        const campos = [
          campoSistema(ehAdmin, p.id, p.sistema === true),
          campoCamada(p.id, p.camada ?? null),
        ].filter((c): c is NonNullable<typeof c> => c !== null);
        const ex = ultimaExec[p.id];
        const meta = ex?.iniciada_em
          ? `v${ex.versao} · última execução ${rotuloDia(diaSP(ex.iniciada_em)!)} ${fmtHora(ex.iniciada_em)} (GMT-3)` +
            (ex.origem === "agendado" ? " · agendada" : "")
          : ex
            ? `v${ex.versao} · última execução sem horário registrado`
            : execsIndisponiveis
              ? "histórico de execuções indisponível"
              : "nunca executado";
        const pill = ex ? pillExecucao(ex.status) : null;
        return {
          id: p.id,
          nome: p.nome,
          meta,
          status: pill?.label,
          scls: pill?.cls,
          // A coluna se chama "Status", mas o que ela mostra é o desfecho da ÚLTIMA EXECUÇÃO:
          // sem isso "falha" lia como "este pipeline está quebrado".
          statusRotulo: "Última execução",
          campos,
        };
      }),
    // alternarSistema recriada a cada render é estável o bastante aqui (fecha sobre setState).
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [listaFiltrada, ehAdmin, sistemaBusy, ultimaExec, execsIndisponiveis],
  );

  useEffect(() => {
    if (aberto === null) void recarregarLista();
    else {
      // Ao abrir um detalhe, carrega as conexões (mapa p/ derivação + origem de opções dinâmicas)
      // e as bibliotecas de funções disponíveis para anexar ao pipeline.
      void api.listarConexoes().then(setConexoes).catch(() => setConexoes([]));
    }
  }, [aberto]);

  // --- Excluir (CRUD) ---
  // Sem `window.confirm`: o GridLista já confirma no modal do design system — eram DUAS
  // confirmações para a mesma exclusão, a primeira fora do tema.
  const excluirPipeline = async (id: string) => {
    try {
      await api.excluirPipeline(id);
      await recarregarLista();
    } catch (e: any) {
      setErroLista(e?.message ?? "Falha ao excluir pipeline.");
    }
  };

  // --- Renomear (CRUD) — pela listagem ---
  const renomearPipeline = async (id: string, nome: string) => {
    setErroLista(null);
    try {
      await api.renomearPipeline(id, nome);
      await recarregarLista();
    } catch (e: any) {
      setErroLista(e?.message ?? "Falha ao renomear pipeline.");
    }
  };

  // --- Carrega os metadados do cabeçalho ao abrir um pipeline existente ---
  // (O grafo em si é auto-carregado pelo StudioGraph a partir do pipelineId no Redux.)
  // Última chamada vence: abrir um pipeline dispara mais de uma carga (o StrictMode dobra o efeito
  // de montagem no dev; trocar de pipeline depressa faz o mesmo em produção), e a resposta mais
  // ANTIGA chegando por último reescrevia a lista de parâmetros — levando junto o que acabou de ser
  // editado. Abrir outro, criar um novo e voltar para a lista também invalidam a carga em voo.
  const serieDetalhe = useRef(0);
  const carregarDetalhe = async (id: string) => {
    const minha = ++serieDetalhe.current;
    setErroDetalhe(null);
    setDetalhe(null);
    try {
      const det = await api.obterPipeline(id);
      if (minha !== serieDetalhe.current) return;
      const nos = det.definicao?.nos ?? [];
      const fontes = nos.filter((n: any) => n.tipo === "fonte").length;
      setDetalhe({ nome: det.nome, nos: nos.length, fontes, ativo: det.ativo !== false });
      setParametros(det.definicao?.parametros ?? []);
      setOkParams(false);
      setErroParams(null);
      setVersaoSel(null); // sempre abre na última versão
      setSujo(false);
      dispatch(setPipeline({ id, numero: det.numero }));
      void carregarVersoes(id); // o id vai explícito: o Redux ainda não propagou neste ponto
    } catch (e: any) {
      if (minha !== serieDetalhe.current) return;
      setErroDetalhe(e?.message ?? "Falha ao abrir pipeline.");
    }
  };

  const reiniciarEstadoDetalhe = () => {
    setResultadoExec(null);
    setErroExec(null);
    setPublicavel(false);
  };

  const abrirExistente = (id: string) => {
    reiniciarEstadoDetalhe();
    setAberto(id);
    void carregarDetalhe(id);
  };

  // `/pipeline?abrir=<id>` abre o pipeline direto no detalhe. É o caminho de volta da tela de
  // Execuções ("abrir no Studio"): quem achou o nó que falhou precisa chegar onde se corrige.
  // (`paramsUrl`/`setParamsUrl` são declarados junto do estado da listagem, porque a aba de filtro
  // também mora na querystring e é lida antes deste ponto.)
  useEffect(() => {
    const id = paramsUrl.get("abrir");
    if (!id) return;
    const p = new URLSearchParams(paramsUrl);
    p.delete("abrir"); // consome o parâmetro para o F5 não reabrir por cima do trabalho
    setParamsUrl(p, { replace: true });
    abrirExistente(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const novoPipeline = async () => {
    serieDetalhe.current++; // a carga de outro pipeline em voo não pode pintar este
    setErroDetalhe(null);
    reiniciarEstadoDetalhe();
    try {
      const p = await api.criarPipeline("Novo pipeline");
      dispatch(setPipeline({ id: p.id, numero: p.numero }));
      setDetalhe({ nome: "Novo pipeline", nos: 0, fontes: 0, ativo: true });
      setParametros([]);
      setOkParams(false);
      setErroParams(null);
      setAberto(p.id);
    } catch (e: any) {
      setErroDetalhe(e?.message ?? "Falha ao criar pipeline.");
      setAberto("__novo__");
    }
  };

  // Guarda única para os dois pontos de fuga do editor (voltar e trocar de versão): o estado
  // `sujo` já era rastreado e não protegia nada — três nós ligados e configurados iam embora sem
  // uma linha na tela.
  const [pedirDescarte, setPedirDescarte] = useState<null | (() => void)>(null);
  const comGuarda = (acao: () => void) => {
    if (sujo) setPedirDescarte(() => acao);
    else acao();
  };

  const voltar = () =>
    comGuarda(() => {
      serieDetalhe.current++; // a carga em voo não reabre o que se acabou de fechar
      setAberto(null);
      setDetalhe(null);
      reiniciarEstadoDetalhe();
      dispatch(setPipeline({ id: "", numero: 0 }));
    });

  // Parâmetros que o operador/especialista PRECISA informar para rodar (declarados e NÃO derivados;
  // os derivados são calculados no backend a partir de outros). Vazio → executa direto.
  const paramsRuntime = parametros.filter((p) => !p.derivacao && (p.nome ?? "").trim() !== "");

  // Clique em "Executar": se há parâmetros a informar, abre o modal (pré-preenchido com
  // valor_preview/default); senão executa direto. Evita o erro "parâmetro obrigatório ausente".
  const abrirExecucao = () => {
    if (paramsRuntime.length === 0) {
      void executar();
      return;
    }
    const iniciais: Record<string, string> = {};
    for (const p of paramsRuntime) {
      const v = p.valor_preview ?? p.default;
      iniciais[p.nome] = v != null ? String(v) : "";
    }
    setValoresExec(iniciais);
    setErroExec(null);
    setTentouExec(false);
    setModalExec(true);
  };

  // Confirma o modal: valida obrigatórios, monta os valores (inteiro→número) e executa.
  // Nomes dos obrigatórios ainda em branco: alimentam `aria-invalid` no campo em vez de só um
  // texto solto no fim do modal (que não dizia QUAL campo faltava, com 8 parâmetros na tela).
  const emBrancoObrig = modalExec
    ? paramsRuntime.filter((p) => p.obrigatorio && (valoresExec[p.nome] ?? "").trim() === "").map((p) => p.nome)
    : [];
  // Só marca campo como inválido DEPOIS da tentativa: abrir o modal já com tudo em vermelho
  // culpa a pessoa por não ter digitado ainda.
  const faltando = tentouExec ? emBrancoObrig : [];

  const confirmarExecucao = () => {
    setTentouExec(true);
    if (emBrancoObrig.length) {
      setErroExec(null);
      // Leva o foco ao primeiro campo que falta: com o modal rolado, a mensagem no rodapé podia
      // estar fora de vista.
      requestAnimationFrame(() =>
        document.querySelector<HTMLElement>(`[data-testid="exec-param-${emBrancoObrig[0]}"]`)?.focus(),
      );
      return;
    }
    const valores: Record<string, unknown> = {};
    for (const p of paramsRuntime) {
      const bruto = (valoresExec[p.nome] ?? "").trim();
      if (bruto === "") continue; // vazio (opcional) → não envia (backend trata default/null)
      valores[p.nome] = p.tipo === "inteiro" ? Number(bruto) : bruto;
    }
    setModalExec(false);
    void executar(valores);
  };

  // --- Botão único: Executar → (sucesso) → Publicar ---
  const executar = async (valores?: Record<string, unknown>) => {
    if (!pipelineId) {
      setErroExec("Crie ou abra um pipeline antes de executar.");
      return;
    }
    setExecutando(true);
    setErroExec(null);
    setExecEmVoo(null);
    setResultadoExec(null);
    dispatch(setErro(null));
    try {
      // Só persiste o canvas (criando versão) SE houve edição. Sem edição, roda a versão vigente/
      // selecionada SEM criar outra (executar não muda lógica → não gera versão). Com edição,
      // salvar primeiro reflete o canvas e gera a nova versão (comportamento esperado).
      let alvoVersao: number | null = versaoSel ?? ultimaVersao;
      if (sujo) {
        await studioRef.current?.salvar();
        setSujo(false);
        setVersaoSel(null);
        alvoVersao = null; // recém-salvo é a última → roda a última
      }
      let r = await api.executarPipeline(pipelineId, valores, undefined, alvoVersao);
      for (let i = 0; i < 40 && r?.status === "executando"; i++) {
        await new Promise((s) => setTimeout(s, 1000));
        r = await api.consultarExecucao(r.id);
      }
      setResultadoExec(r);
      // SÓ habilita "Publicar" com execução CONCLUÍDA. Antes era `!== "falha"`, então quando o poll
      // desistia (40s — comum nas pipelines reais deste produto) o status continuava "executando" e
      // a tela liberava publicar sobre uma execução não verificada, dizendo "Execução concluída".
      setPublicavel(r?.status === "concluida");
      void carregarVersoes(); // atualiza contagem de execuções por versão
    } catch (e: any) {
      // Já há uma execução em andamento com estes parâmetros: a plataforma recusa disparar outra
      // (duplicaria efeitos e custo). A saída é ACOMPANHAR a que roda, não insistir.
      setExecEmVoo(api.execucaoEmVoo(e));
      setErroExec(e?.message ?? "Falha ao executar pipeline.");
    } finally {
      setExecutando(false);
    }
  };

  const publicar = async (nota?: string) => {
    if (!pipelineId || !studioRef.current) return;
    setPublicando(true);
    setErroExec(null);
    dispatch(setErro(null));
    try {
      const numero = await studioRef.current.publicar(nota);
      setResultadoExec({ publicado: true, numero });
      // Após publicar, o botão volta a "Executar"; a nova versão é a ativa/última.
      setPublicavel(false);
      setSujo(false);
      setVersaoSel(null);
      void carregarVersoes();
    } catch (e: any) {
      setErroExec(e?.message ?? "Falha ao publicar pipeline.");
    } finally {
      setPublicando(false);
    }
  };

  // Fechar a aba/recarregar com rascunho pendente: única guarda que o app não controla sozinho.
  useEffect(() => {
    if (!sujo) return;
    const aviso = (ev: BeforeUnloadEvent) => {
      ev.preventDefault();
      ev.returnValue = "";
    };
    window.addEventListener("beforeunload", aviso);
    return () => window.removeEventListener("beforeunload", aviso);
  }, [sujo]);

  // --- Versões (dropdown no cabeçalho) ---
  // O `catch` daqui apagava o erro E o seletor junto: sem lista, o cabeçalho simplesmente não
  // renderizava o controle, e quem abria o pipeline concluía que o produto não tem seleção de
  // versão. Falha de carga agora é DITA, e o seletor continua na tela mostrando a versão atual.
  // `id` explícito porque `carregarDetalhe` chama esta função LOGO APÓS o `dispatch(setPipeline)`:
  // naquele instante o `pipelineId` do Redux ainda é o valor anterior dentro deste closure, e o
  // guard `if (!id) return` abortava em silêncio. Resultado: ao ABRIR um pipeline a lista de
  // versões nunca chegava, o seletor não renderizava e a tela parecia não ter seleção de versão —
  // ela só aparecia depois de publicar/salvar, quando o Redux já havia propagado.
  const carregarVersoes = useCallback(
    async (idExplicito?: string) => {
      const id = idExplicito ?? pipelineId;
      if (!id) return;
      try {
        setVersoes(await api.listarVersoes(id));
        setErroVersoes(null);
      } catch (e: any) {
        setVersoes([]);
        setErroVersoes(e?.message ?? "Falha ao listar as versões deste pipeline.");
      }
    },
    [pipelineId],
  );

  // Versões são IMUTÁVEIS. Selecionar uma anterior só a CARREGA no canvas (para ver/rodar). Ao
  // Executar/Publicar a versão carregada, o backend gera uma NOVA versão no topo (ex.: editar a v70
  // estando na v89 → cria a v90); a v70 nunca é alterada (StudioGraph usa baseNumero = nº máximo).
  const selecionarVersao = async (numero: number | null) => {
    if (sujo) {
      setPedirDescarte(() => () => void trocarVersao(numero));
      return;
    }
    await trocarVersao(numero);
  };

  const trocarVersao = async (numero: number | null) => {
    setVersaoSel(numero);
    setPublicavel(false);
    setResultadoExec(null);
    setErroExec(null);
    setSujo(false); // carregar uma versão não é edição → não força salvar/criar versão ao rodar
    studioRef.current?.abrirVersao(numero ?? undefined);
    // Mantém os parâmetros do modal de execução coerentes com a versão carregada.
    if (pipelineId) {
      try {
        const det = await api.obterPipeline(pipelineId, numero ?? undefined);
        setParametros(det.definicao?.parametros ?? []);
      } catch {
        /* ignora: o StudioGraph já carregou a versão; o modal cai no que tinha */
      }
    }
  };

  // --- Mutações em memória dos parâmetros (editar parâmetros = dirty) ---
  const addParametro = () => {
    marcarSujoParams();
    setParametros((ps) => [...ps, { nome: "", tipo: "texto", obrigatorio: false }]);
  };
  const removerParametro = (idx: number) => {
    marcarSujoParams();
    setParametros((ps) => ps.filter((_, i) => i !== idx));
  };
  const editarParametro = (idx: number, patch: Partial<ParametroPipeline>) => {
    marcarSujoParams();
    setParametros((ps) => ps.map((p, i) => (i === idx ? { ...p, ...patch } : p)));
  };
  // Declara um `:nome` que já aparece num nó. Mesmo tipo da autodeclaração do salvar (`texto`
  // opcional) — a pessoa então acerta o tipo aqui, que é justamente o motivo de declarar antes.
  const declararParametro = (nome: string) => {
    const limpo = (nome ?? "").trim();
    if (limpo === "") return;
    marcarSujoParams();
    setParametros((ps) =>
      ps.some((p) => (p.nome ?? "").trim() === limpo)
        ? ps
        : [...ps, { nome: limpo, tipo: "texto", obrigatorio: false }],
    );
  };

  // Persiste os parâmetros pelo MESMO rascunho do grafo (api.salvarRascunho).
  // Recarrega nós/arestas do pipeline para não sobrescrever o que o StudioGraph
  // mantém: regrava { nos, arestas, parametros } na definição.
  const salvarParametros = async () => {
    if (!pipelineId) {
      setErroParams("Abra um pipeline antes de salvar os parâmetros.");
      return;
    }
    setSalvandoParams(true);
    setErroParams(null);
    setOkParams(false);
    try {
      const det = await api.obterPipeline(pipelineId);
      // A limpeza campo a campo (credencial sem valor, padrão de inteiro como número, metadado em
      // branco omitido) mora em `definicao.ts`, testada sem navegador — e o que esta aba não edita,
      // no parâmetro e dentro de `derivacao`/`opcoes_fonte`, volta intacto.
      const limpos: ParametroPipeline[] = parametros
        .filter((p) => p.nome.trim() !== "")
        .map(parametroParaSalvar);
      // Autodeclara `:ref` sem parâmetro correspondente antes de persistir — cobre o caso de
      // salvar por aqui sem nunca ter passado pelo "Salvar" do code area.
      const { criados, todos } = parametrosAutomaticos(det.definicao?.nos ?? [], limpos);
      // Só os parâmetros mudam: nós, arestas e o resto do topo (`montante_publica`…) seguem como o
      // servidor os tem. Montar `{nos, arestas, parametros}` do zero apagava o resto no salvar.
      const definicao = definicaoComParametros(det.definicao, todos);
      const r = await api.salvarRascunho(pipelineId, definicao, baseNumero);
      dispatch(setBaseNumero(r.numero));
      if (criados.length) {
        setParametros((ps) => [...ps, ...criados]);
        setCriadosAuto(criados.map((p) => p.nome));
      }
      setOkParams(true);
      setSujo(false);
      void carregarVersoes();
    } catch (e: any) {
      setErroParams(e?.message ?? "Falha ao salvar parâmetros.");
    } finally {
      setSalvandoParams(false);
    }
  };

  // ===== Listagem =====
  if (aberto === null) {
    if (erroLista) {
      return (
        <div className="page-head">
          <h1 className="display">Pipelines</h1>
          <p className="sub" style={{ color: "crimson" }}>
            {erroLista}
          </p>
          <div className="pad">
            <button className="btn-stamp" onClick={() => void recarregarLista()}>
              Tentar novamente
            </button>
          </div>
        </div>
      );
    }
    if (carregandoLista) {
      return (
        <div className="page-head">
          <h1 className="display">Pipelines</h1>
          <p className="sub">Carregando…</p>
        </div>
      );
    }
    // Cada aba explica o próprio vazio dizendo COMO se entra nela — a pessoa que abriu "Silver" e
    // achou nada precisa saber que classificar é uma ação do menu ⋯, não algo que se cria de novo.
    // Sistema não cabe nessa frase: quem povoa aquela aba é "Marcar como sistema", outra ação.
    const vazioDaAba = (
      <div className="card lc-empty">
        <div className="big">Nenhum pipeline nesta aba</div>
        <div>
          {aba === "sistema" ? (
            <>
              Nenhum pipeline está marcado como item de sistema. A marca fica em{" "}
              <b>Marcar como sistema</b>, no menu <b>⋯</b> da linha.
            </>
          ) : aba === ABA_PADRAO ? (
            <>
              Todos os pipelines estão classificados em alguma camada, ou marcados como
              item de sistema — veja as outras abas.
            </>
          ) : (
            <>
              Nenhum pipeline está classificado na camada <b>{aba}</b>. Para classificar um, use{" "}
              <b>Definir camada…</b> no menu <b>⋯</b> da linha.
            </>
          )}
        </div>
      </div>
    );
    return (
      <>
      <ListaCadastro
        erro={!!erroLista}
        titulo="Pipelines"
        novoLabel="Novo pipeline"
        itens={itens}
        // Conta a lista FILTRADA: com uma aba ativa, "12 pipelines" acima de 3 linhas visíveis é a
        // barra mentindo. O total por aba fica no rótulo de cada aba.
        contagem={`${itens.length} ${itens.length === 1 ? "pipeline" : "pipelines"}`}
        abas={ABAS_CAMADA.map((a) => ({ ...a, contagem: contagens[a.chave] }))}
        abaAtiva={aba}
        onAba={trocarAba}
        // Aba vazia com pipelines cadastrados NÃO é "nada cadastrado ainda": há pipelines, só não
        // nesta aba — e dizer o contrário manda a pessoa criar um pipeline que ela já tem. A
        // condição é `listaRaw` ter itens, não a aba ser diferente da padrão: se TODOS estiverem
        // classificados, "Geral" fica vazia e a mensagem de lista virgem também seria falsa ali.
        vazio={listaRaw.length === 0 ? undefined : vazioDaAba}
        onNovo={() => void novoPipeline()}
        onAbrir={(id) => abrirExistente(id)}
        onExcluir={(id) => void excluirPipeline(id)}
        onRenomear={(id, nomeAtual) => void renomearPipeline(id, nomeAtual)}
        // A marca de sistema deixou de ser checkbox na linha (era o acidente mais barato da tela) e
        // virou ação com confirmação — mas só existe para admin.
        onSistema={ehAdmin ? (id, sistema) => void alternarSistema(id, sistema) : undefined}
        ehSistema={(it) => listaRaw.find((p) => p.id === it.id)?.sistema === true}
        // Camada também é admin-only (a rota devolve 403 para os demais). Busca no `listaRaw`, não
        // no filtrado: com a aba trocando durante a ação, o item pode ter saído da lista visível.
        onCamada={ehAdmin ? (id, camada) => void alternarCamada(id, camada) : undefined}
        camadaDe={(it) => listaRaw.find((p) => p.id === it.id)?.camada ?? null}
        // Ativo é admin-only (a rota devolve 403 para os demais), como as três de cima. Default
        // `true` quando ausente — pipeline sem o campo carregado não deve aparecer "desativado".
        onAtivo={ehAdmin ? (id, ativo) => void alternarAtivo(id, ativo) : undefined}
        ativoDe={(it) => listaRaw.find((p) => p.id === it.id)?.ativo !== false}
        // Carimbo de obsoleto: escrita no ponteiro que todo consumidor do dataset lê, e
        // por isso admin-only como as quatro de cima (a rota devolve 403 para os demais). Fica no
        // menu de TODO pipeline; o modal diz quando ele não publica no lake.
        acoesMenu={
          ehAdmin
            ? (it) => (
                <button
                  data-testid={`carimbo-acao-${it.id}`}
                  onClick={() => setCarimbo({ id: it.id, nome: it.nome })}
                >
                  Carimbar escopo obsoleto…
                </button>
              )
            : undefined
        }
      />
      {carimbo && (
        <CarimboEscopo
          pipelineId={carimbo.id}
          pipelineNome={carimbo.nome}
          onFechar={() => setCarimbo(null)}
        />
      )}
      {/* A aba Bronze é a única que mostra DUAS coisas: os pipelines classificados em bronze (acima)
          e as fontes de ingestão que POVOAM o bronze (abaixo). São recursos diferentes — a fonte não
          tem grafo — e por isso vivem em blocos separados, cada um com sua própria grade e suas
          próprias ações. Montado só na aba Bronze: é o que faz a chamada a `/ingestao/fontes`
          acontecer ali e não em toda abertura da tela de Pipelines. */}
      {aba === "bronze" && <FontesIngestao />}
      </>
    );
  }

  // ===== Detalhe =====
  const nome = detalhe?.nome ?? "Novo pipeline";

  // Renomear no cabeçalho do detalhe: confirma o novo nome, persiste (PATCH) e atualiza o estado local.
  const renomearNoDetalhe = async (novo: string) => {
    const limpo = novo.trim();
    if (!pipelineId || !limpo || limpo === nome) return;
    setErroDetalhe(null);
    try {
      const r = await api.renomearPipeline(pipelineId, limpo);
      setDetalhe((prev) => (prev ? { ...prev, nome: r.nome } : prev));
    } catch (e: any) {
      setErroDetalhe(e?.message ?? "Falha ao renomear pipeline.");
    }
  };

  // Estado/rótulo do botão único.
  const ocupado = executando || publicando;
  // Executar e Publicar deixaram de ser o MESMO botão trocando de identidade: o clique reflexo
  // depois de uma execução publicava uma versão imutável e auditada sem nenhuma confirmação.
  const labelBotao = ocupado ? (publicando ? "Publicando…" : "Executando…") : "Executar";
  const iconeBotao = "run";
  const acaoBotao = () => abrirExecucao();

  // Versões: a última (maior nº) e se estamos vendo uma anterior.
  const ultimaVersao = versoes.length ? versoes[0].numero : null;
  const vendoAnterior = versaoSel != null && versaoSel !== ultimaVersao;

  // Estado do resultado da execução/publicação (para o banner E para marcar os nós no canvas).
  const execId = (() => {
    const id = resultadoExec?.id ?? resultadoExec?.execucao_id;
    return typeof id === "string" ? id.slice(0, 8) : null;
  })();
  const execPublicado = !!resultadoExec?.publicado;
  const execFalhou = !!resultadoExec && !execPublicado && resultadoExec.status === "falha";
  // O poll do Studio desiste em ~40s; a execução segue no worker. Sem este estado a tela dizia
  // "Execução concluída" e liberava publicar sobre algo que ainda estava rodando.
  const execAndando = !!resultadoExec && !execPublicado && resultadoExec.status === "executando";
  const nosFalhos: any[] = execFalhou && Array.isArray(resultadoExec?.nos)
    ? resultadoExec.nos.filter((n: any) => n.status === "falha")
    : [];
  // Mapa no_id → erro (passado ao StudioGraph p/ pintar o card que falhou).
  const errosExec: Record<string, string> = {};
  for (const n of nosFalhos) errosExec[n.no_id] = n.erro || "Falha neste nó.";

  return (
    <>
      {pedirDescarte && (
        <ConfirmarAcao
          titulo="Descartar alterações não salvas?"
          corpo="Há mudanças no grafo que ainda não foram salvas. Se continuar, elas são perdidas."
          rotuloConfirmar="Descartar e continuar"
          perigo
          onConfirmar={() => {
            const acao = pedirDescarte;
            setPedirDescarte(null);
            setSujo(false);
            acao();
          }}
          onCancelar={() => setPedirDescarte(null)}
        />
      )}
      {modalAmostra && (
        <ConfirmarAcao
          titulo="Amostra da prévia"
          corpo={
            <>
              Quantas linhas a prévia lê em cada <b>fonte</b> e em cada nó <b>SQL</b> (nos outros nós
              o tamanho vem de quem vem antes). Vale só para esta sessão — a amostra não entra na
              definição salva, e reabrir o pipeline volta para 10.
              <br />
              <b>
                {modalAmostra.nos} nó{modalAmostra.nos === 1 ? "" : "s"}
              </b>{" "}
              {modalAmostra.nos === 0
                ? "— nada a aplicar."
                : modalAmostra.valores.length === 1
                  ? `· hoje em ${modalAmostra.valores[0].toLocaleString("pt-BR")}`
                  : `· hoje com valores diferentes (${modalAmostra.valores
                      .map((v) => v.toLocaleString("pt-BR"))
                      .join(", ")})`}
            </>
          }
          rotuloCampo="Linhas por nó"
          valorInicial={String(modalAmostra.valores.length === 1 ? modalAmostra.valores[0] : 1000)}
          campoNumero
          extra={(definir, valor) => (
            <div className="amostra-atalhos">
              {[10, 100, 1000, 10000, 100000].map((v) => (
                <button
                  key={v}
                  type="button"
                  className={"pag-btn" + (Number(valor) === v ? " is-ativo" : "")}
                  data-testid={`amostra-atalho-${v}`}
                  onClick={() => definir(String(v))}
                >
                  {v.toLocaleString("pt-BR")}
                </button>
              ))}
            </div>
          )}
          rotuloConfirmar={`Aplicar a ${modalAmostra.nos} nó${modalAmostra.nos === 1 ? "" : "s"}`}
          erro={erroAmostra}
          onConfirmar={(texto) => {
            const valor = Math.floor(Number(texto));
            // O campo é numérico, mas nada impede digitar 0 ou letra: um "0" aceito em silêncio
            // faria a prévia devolver zero linha e parecer que a fonte está vazia.
            if (!Number.isFinite(valor) || valor < 1) {
              setErroAmostra("Informe um número de linhas maior que zero.");
              return;
            }
            setErroAmostra(null);
            const alterados = studioRef.current?.aplicarAmostra(valor) ?? 0;
            setModalAmostra(null);
            setAvisoAmostra(
              alterados === 0
                ? `Todos os nós já estavam em ${valor.toLocaleString("pt-BR")} linhas.`
                : `Amostra de ${alterados} nó${alterados === 1 ? "" : "s"} definida em ${valor.toLocaleString("pt-BR")} linhas. Rode a prévia do nó para ver o resultado.`,
            );
          }}
          onCancelar={() => {
            setModalAmostra(null);
            setErroAmostra(null);
          }}
        />
      )}
      {pedirDesativar && (
        <ConfirmarAcao
          titulo="Desativar pipeline"
          corpo={
            <>
              <b>{nome}</b> deixa de EXECUTAR — inclusive por gatilho externo, agendamento e
              reexecução. O rascunho continua editável e publicável; só RODAR fica bloqueado. Dá
              para reativar depois.
            </>
          }
          rotuloConfirmar="Desativar"
          perigo
          onConfirmar={() => {
            setPedirDesativar(false);
            if (pipelineId) void alternarAtivo(pipelineId, false);
          }}
          onCancelar={() => setPedirDesativar(false)}
        />
      )}
      {modalPublicar && (
        <ConfirmarAcao
          titulo={`Publicar v${(ultimaVersao ?? 0) + 1}`}
          corpo="Publicar cria uma versão imutável e auditada. A nota de mudança fica na trilha e no histórico de versões — descreva o que mudou."
          rotuloConfirmar="Publicar"
          rotuloCampo="O que mudou nesta versão?"
          valorInicial=""
          onConfirmar={(nota) => {
            setModalPublicar(false);
            void publicar(nota);
          }}
          onCancelar={() => setModalPublicar(false)}
        />
      )}
      <div className="page-head page-head--compacta">
        <div className="sd-head sd-head--compacta">
          <button className="link-back link-back--inline" onClick={voltar} title="Voltar">
            ←
          </button>
          <TituloEditavel
            valor={nome}
            podeEditar={!!pipelineId}
            onConfirmar={(v) => void renomearNoDetalhe(v)}
          />
          {/* Aparece SEMPRE que há um pipeline aberto — inclusive quando a lista falhou. Com
              `versoes.length > 0`, qualquer erro de carga apagava o controle em silêncio, e a
              tela ficava sem nenhuma pista de que a seleção de versão existe. */}
          {pipelineId && (
            <SeletorVersao
              versoes={versoes}
              // Sem a lista, a versão atual vem do Redux — a MESMA fonte do caption "versão
              // atual: vN" do canvas, para os dois números nunca se contradizerem na tela.
              ultima={ultimaVersao ?? baseNumero ?? null}
              sel={versaoSel}
              ocupado={ocupado}
              onSelecionar={(n) => void selecionarVersao(n)}
              erro={erroVersoes}
            />
          )}
          <div className="meta-inline">
            <span>
              <b>{detalhe?.nos ?? "—"}</b> nós
            </span>
            <span>
              <b>{detalhe?.fontes ?? "—"}</b> fontes
            </span>
            {vendoAnterior && (
              <span className="ver-aviso" data-testid="ver-aviso">
                vendo versão anterior — Executar/Publicar gera uma nova (a v{versaoSel} fica intacta)
              </span>
            )}
          </div>
        </div>
      </div>
      <div className="pad pad--editor">
        {/* Executar/Publicar flutua no canto sup. direito do editor (sobre o canvas no grafo),
            sempre acessível (qualquer aba). */}
        <div className="sd-acoes-flutuante">
          {/* Amostra de TODAS as fontes de uma vez: sem isto, testar o grafo com mais linhas era
              abrir nó por nó e trocar 10 → 1000 em cada um.
              Fica DESABILITADO enquanto o grafo não reportou nós: clicar durante a carga abria o
              diálogo dizendo "0 nós — nada a aplicar" num pipeline de 23 nós, o que é mentira sobre
              o estado (em ambientes onde a carga é mais lenta). */}
          <button
            type="button"
            className="pag-btn"
            data-testid="btn-amostra-massa"
            disabled={!pipelineId || ocupado || !detalhe?.nos}
            title={
              detalhe?.nos
                ? "Quantas linhas a prévia lê em cada fonte e em cada nó SQL"
                : "Abra um pipeline com nós para ajustar a amostra"
            }
            onClick={() => {
              setAvisoAmostra(null);
              setErroAmostra(null);
              setModalAmostra(studioRef.current?.resumoAmostra() ?? { nos: 0, valores: [] });
            }}
          >
            <Icon name="grid" cls="ic" /> Amostra da prévia
          </button>
          {/* Salvar EXPLÍCITO: o editor rastreava `sujo` com cuidado e nunca oferecia salvar — só
              persistia por efeito colateral do Executar (ou pela janela de código). Voltar ou
              espiar outra versão descartava o trabalho em silêncio. */}
          <button
            type="button"
            className="pag-btn"
            data-testid="btn-salvar-rascunho"
            disabled={!sujo || ocupado || salvando || !pipelineId}
            onClick={() => {
              setSalvando(true);
              void studioRef.current
                ?.salvar()
                .then(() => setSujo(false))
                .catch((e: any) => setErroDetalhe(e?.message ?? "Falha ao salvar o rascunho."))
                .finally(() => setSalvando(false));
            }}
          >
            {salvando ? "Salvando…" : sujo ? "Salvar rascunho" : "Salvo"}
          </button>
          <button
            data-testid="btn-exec-pub"
            className="btn-run"
            disabled={ocupado || !pipelineId || detalhe?.ativo === false}
            title={detalhe?.ativo === false ? "Pipeline desativado — reative para executar." : undefined}
            onClick={acaoBotao}
          >
            <Icon name={iconeBotao} cls="ic" /> {labelBotao}
          </button>
          {publicavel && !ocupado && (
            <button
              type="button"
              className="btn-stamp"
              data-testid="btn-publicar"
              onClick={() => setModalPublicar(true)}
            >
              <Icon name="stamp" cls="ic" /> Publicar
            </button>
          )}
          {/* Ativo/desativado bloqueia EXECUÇÃO (não edição/publicação) em todo gatilho — UI,
              agendador cron, reexecução. Admin-only, como sistema/camada na listagem.
              Interruptor de verdade (não botão de texto): é um controle de segurança, precisa
              estar sempre visível e óbvio, igual à coluna Ativo da listagem — mesmo componente. */}
          {ehAdmin && pipelineId && detalhe && (
            <span
              className="pag-btn"
              style={{ display: "inline-flex", alignItems: "center", gap: 8, cursor: "default" }}
              data-testid="ativo-detalhe"
            >
              Ativo
              <Interruptor
                ligado={detalhe.ativo}
                rotulo={detalhe.ativo ? "Desativar pipeline" : "Ativar pipeline"}
                testid="switch-ativo-detalhe"
                onAlternar={() => {
                  if (ocupado) return;
                  if (detalhe.ativo) setPedirDesativar(true);
                  else void alternarAtivo(pipelineId, true);
                }}
              />
            </span>
          )}
        </div>
        {detalhe && !detalhe.ativo && (
          <div className="run-banner" role="status" data-testid="aviso-desativado" style={{ marginTop: 0, marginBottom: 14 }}>
            <span>
              Pipeline desativado — Executar recusa (inclusive por gatilho externo, agendamento e
              reexecução). Edição e publicação continuam liberadas.
            </span>
          </div>
        )}
        {/* `.exec-banner-falha` em vez de hex inline: estilo inline vence o bloco [data-theme=dark],
            então estes banners eram ilhas de tema claro dentro da UI escura. */}
        {avisoAmostra && (
          <div className="run-banner" role="status" data-testid="aviso-amostra" style={{ marginTop: 0, marginBottom: 14 }}>
            <span>{avisoAmostra}</span>
            <button type="button" className="pag-btn" onClick={() => setAvisoAmostra(null)} aria-label="Dispensar aviso">
              ✕
            </button>
          </div>
        )}
        {erroDetalhe && (
          <div className="exec-banner-falha" role="alert" style={{ marginBottom: 14 }}>
            <span className="dot" />
            <span>{erroDetalhe}</span>
          </div>
        )}
        {erroExec && (
          <div className="exec-banner-falha" role="alert" style={{ marginBottom: 14 }}>
            <span className="dot" />
            <span>{erroExec}</span>
            {execEmVoo && (
              <button
                type="button"
                className="btn-sec"
                style={{ marginLeft: 10 }}
                onClick={() => navigate(`/execucoes/${execEmVoo}`)}
              >
                Acompanhar a execução em andamento
              </button>
            )}
          </div>
        )}
        {(executando || publicando) && (
          <div className="run-banner" style={{ marginTop: 0, marginBottom: 14 }}>
            <span className="dot" />
            <span>{executando ? "Executando…" : "Publicando…"}</span>
          </div>
        )}
        {!ocupado && execPublicado && (
          <div className="run-banner" style={{ marginTop: 0, marginBottom: 14 }}>
            <span className="dot" />
            <span>
              <b>Publicado</b> · versão v{resultadoExec.numero} · imutável
            </span>
          </div>
        )}
        {!ocupado && execFalhou && (
          <div className="exec-banner-falha" data-testid="exec-falha" role="alert" style={{ marginBottom: 14 }}>
            <span className="dot" />
            <span>
              <b>Execução falhou</b>
              {nosFalhos.length > 0 && ` · nó ${nosFalhos.map((n) => n.no_id).join(", ")}`}
              {execId && ` · execução ${execId}`}
              {nosFalhos[0]?.erro && <span style={{ display: "block", marginTop: 4, fontWeight: 400 }}>{nosFalhos[0].erro}</span>}
              {nosFalhos.length === 0 && resultadoExec?.erro && (
                <span style={{ display: "block", marginTop: 4, fontWeight: 400 }}>{resultadoExec.erro}</span>
              )}
            </span>
          </div>
        )}
        {!ocupado && execAndando && (
          <div className="run-banner" data-testid="exec-andando" style={{ marginTop: 0, marginBottom: 14 }}>
            <span className="statuspill exec">
              <span className="d" />
              em execução
            </span>
            <span>
              <b>Ainda executando</b> — o Studio parou de acompanhar, mas a execução continua.{" "}
              <button
                type="button"
                className="lk"
                onClick={() => navigate(`/execucoes/${resultadoExec!.id}`)}
              >
                acompanhar em Execuções
              </button>
            </span>
          </div>
        )}
        {!ocupado && resultadoExec && !execPublicado && !execFalhou && !execAndando && (
          <div className="run-banner" style={{ marginTop: 0, marginBottom: 14 }}>
            <span className="dot" />
            <span>
              <b>Execução concluída</b>
              {execId && ` · execução ${execId}`} · snapshot Parquet por nó
            </span>
          </div>
        )}
        {criadosAuto && criadosAuto.length > 0 && (
          <div className="run-banner" data-testid="banner-parametros-auto" style={{ marginTop: 0, marginBottom: 14 }}>
            <span className="dot" />
            <span>
              Parâmetro(s) criado(s) automaticamente a partir do SQL: {criadosAuto.map((n) => `:${n}`).join(", ")} —
              revise tipo/valor padrão na aba Parâmetros.
            </span>
            <button
              type="button"
              onClick={() => setCriadosAuto(null)}
              style={{ marginLeft: "auto", border: "none", background: "none", cursor: "pointer", color: "inherit", fontSize: 16, lineHeight: 1 }}
              aria-label="Dispensar aviso"
            >
              ×
            </button>
          </div>
        )}
        {/* Abas do editor: Grafo (canvas) | Parâmetros | Bibliotecas. */}
        <div
          className="cfg-tabs"
          data-testid="pipeline-abas"
          
          role="tablist"
          aria-label="Seções do editor de pipeline"
          onKeyDown={(ev) => {
            // Setas navegam entre abas (padrão ARIA); sem isto o teclado só alcançava a aba ativa.
            const ordem: typeof abaEditor[] = ["grafo", "parametros"];
            const i = ordem.indexOf(abaEditor);
            const delta = ev.key === "ArrowRight" ? 1 : ev.key === "ArrowLeft" ? -1 : 0;
            if (!delta) return;
            ev.preventDefault();
            const proxima = ordem[(i + delta + ordem.length) % ordem.length];
            setAbaEditor(proxima);
            document.getElementById(`aba-${proxima}`)?.focus();
          }}
        >
          <button
            type="button"
            id="aba-grafo"
            data-testid="aba-grafo"
            role="tab"
            aria-selected={abaEditor === "grafo"}
            aria-controls="painel-grafo"
            tabIndex={abaEditor === "grafo" ? 0 : -1}
            className={abaEditor === "grafo" ? "on" : ""}
            onClick={() => setAbaEditor("grafo")}
          >
            Grafo
          </button>
          <button
            type="button"
            id="aba-parametros"
            data-testid="aba-parametros"
            role="tab"
            aria-selected={abaEditor === "parametros"}
            aria-controls="painel-parametros"
            tabIndex={abaEditor === "parametros" ? 0 : -1}
            className={abaEditor === "parametros" ? "on" : ""}
            onClick={() => setAbaEditor("parametros")}
          >
            {/* Conta só os que TÊM NOME: parâmetro sem nome é descartado no salvar, e a aba
                anunciava um parâmetro que o servidor nunca recebeu. */}
            Parâmetros
            {(() => {
              const n = parametros.filter((p) => (p.nome ?? "").trim() !== "").length;
              return n > 0 ? ` (${n})` : "";
            })()}
          </button>
        </div>

        {/* Painel de Parâmetros: lê/edita definicao.parametros e persiste pelo mesmo rascunho. */}
        <div
          id="painel-parametros"
          role="tabpanel"
          aria-labelledby="aba-parametros"
          hidden={abaEditor !== "parametros"}
        >
          <PainelParametros
            pipelineId={pipelineId || null}
            parametros={parametros}
            conexoes={conexoes}
            podeSalvar={!!pipelineId}
            salvando={salvandoParams}
            ok={okParams}
            erro={erroParams}
            refrescarEm={grafoSalvoEm}
            onAdd={addParametro}
            onRemover={removerParametro}
            onEditar={editarParametro}
            onDeclarar={declararParametro}
            onSalvar={() => void salvarParametros()}
          />
        </div>

        {/* Editor de grafo real (React Flow): nós, SQL, preview, inspetor e preview do dado.
            Auto-carrega o pipeline aberto; reporta dirty (onSujo) e contagens (onMeta) ao cabeçalho.
            SEMPRE montado (só escondido) p/ preservar o canvas ao trocar de aba. */}
        {/* `display:none` em vez de `hidden`: o React Flow precisa continuar montado — remontar
            perdia o viewport (zoom/pan) a cada volta para o grafo. */}
        <div
          className="sd-grafo-wrap"
          id="painel-grafo"
          role="tabpanel"
          aria-labelledby="aba-grafo"
          style={{ display: abaEditor === "grafo" ? "block" : "none" }}
        >
          <StudioGraph
            ref={studioRef}
            onSujo={marcarSujo}
            onMeta={aplicarMeta}
            errosExec={errosExec}
            parametros={parametros}
            onParametros={setParametros}
            onParametrosCriados={(nomes) => {
              // A lista em si já foi atualizada pelo `onParametros` do próprio save (estado
              // compartilhado) — aqui só o aviso ao especialista.
              setCriadosAuto(nomes);
            }}
            onSalvo={() => setGrafoSalvoEm((n) => n + 1)}
          />
        </div>
      </div>

      {/* Modal de execução: coleta os valores dos parâmetros antes de rodar. */}
      {modalExec && (
        <div className="modal-fundo" data-testid="modal-exec" onClick={() => setModalExec(false)}>
          <form
            className="modal-caixa"
            style={{ maxWidth: 440 }}
            role="dialog"
            aria-modal="true"
            aria-labelledby="exec-modal-tit"
            // `noValidate`: a validação é nossa, em pt-BR e apontando o campo — a nativa mostra
            // tooltip no idioma do navegador.
            noValidate
            onClick={(e) => e.stopPropagation()}
            onKeyDown={(e) => {
              if (e.key === "Escape") setModalExec(false);
            }}
            onSubmit={(e) => {
              // Formulário de verdade: antes o Enter num campo não fazia nada e o único caminho era
              // achar o botão com o mouse.
              e.preventDefault();
              confirmarExecucao();
            }}
          >
            <h3 id="exec-modal-tit">Executar pipeline</h3>
            <p>Informe os parâmetros desta execução.</p>
            <div className="exec-params">
              {paramsRuntime.map((p) => (
                <label key={p.nome} className="exec-param">
                  <span className="exec-param-rot">
                    {p.rotulo || p.nome}
                    {p.obrigatorio ? (
                      <b className="exec-param-obg" title="Obrigatório"> *</b>
                    ) : (
                      // "Em branco = tudo" é regra do produto e era invisível: quem deixava em
                      // branco não sabia se filtrava por vazio ou não filtrava.
                      <span className="exec-param-opc"> · em branco = sem filtro</span>
                    )}
                    <code> :{p.nome}</code>
                  </span>
                  {p.tipo === "opcoes" && (p.opcoes ?? []).length > 0 ? (
                    <select
                      data-testid={`exec-param-${p.nome}`}
                      required={p.obrigatorio}
                      aria-invalid={faltando.includes(p.nome) || undefined}
                      value={valoresExec[p.nome] ?? ""}
                      onChange={(e) => setValoresExec((v) => ({ ...v, [p.nome]: e.target.value }))}
                    >
                      <option value="">— escolha —</option>
                      {(p.opcoes ?? []).map((o) => (
                        <option key={o} value={o}>
                          {o}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <input
                      data-testid={`exec-param-${p.nome}`}
                      required={p.obrigatorio}
                      aria-invalid={faltando.includes(p.nome) || undefined}
                      // Credencial não aparece em claro na tela (nem em screenshot de suporte).
                      type={
                        p.segredo
                          ? "password"
                          : p.tipo === "data"
                            ? "date"
                            : p.tipo === "inteiro"
                              ? "number"
                              : p.tipo === "competencia"
                                ? "month"
                                : "text"
                      }
                      autoComplete={p.segredo ? "new-password" : undefined}
                      value={valoresExec[p.nome] ?? ""}
                      onChange={(e) => setValoresExec((v) => ({ ...v, [p.nome]: e.target.value }))}
                    />
                  )}
                </label>
              ))}
            </div>
            {faltando.length > 0 && (
              <p className="exec-erro" role="alert" data-testid="exec-faltando">
                {faltando.length === 1
                  ? "Informe o parâmetro obrigatório destacado."
                  : `Informe os ${faltando.length} parâmetros obrigatórios destacados.`}{" "}
                Parâmetro de escopo em branco leria a base inteira.
              </p>
            )}
            {erroExec && (
              <p className="exec-erro" role="alert">
                {erroExec}
                {execEmVoo && (
                  <>
                    {" "}
                    <button
                      type="button"
                      className="btn-sec"
                      onClick={() => navigate(`/execucoes/${execEmVoo}`)}
                    >
                      Acompanhar a execução em andamento
                    </button>
                  </>
                )}
              </p>
            )}
            <div className="modal-acoes">
              <button type="button" className="btn-sec" onClick={() => setModalExec(false)}>
                Cancelar
              </button>
              <button type="submit" className="btn-primario" data-testid="exec-confirmar">
                Executar
              </button>
            </div>
          </form>
        </div>
      )}

    </>
  );
}

// ============================================================================
// PainelParametros — lista/edita os parâmetros do pipeline (definicao.parametros).
// Cada parâmetro é referenciado por `:nome` na fonte, no SQL e na condição guiada.
// ============================================================================

// Estilos locais do painel. Inline porque `cflow.css` não é editável nesta empreitada — por isso
// tudo aqui sai de TOKEN (`var(--…)`), que o tema escuro já redefine. Cor fixa neste painel foi
// justamente o que deixou o bloco de avisos creme sobre fundo escuro.
const P_MONO = "ui-monospace,Menlo,monospace";
const P_AJUDA: React.CSSProperties = { fontSize: 11.5, color: "var(--ink-3)", lineHeight: 1.45 };
const P_COD: React.CSSProperties = { fontFamily: P_MONO, fontSize: "0.95em" };
const P_MARCA: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 7,
  fontSize: 13,
  cursor: "pointer",
};
const P_CHIP: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  fontSize: 11.5,
  border: "1px solid var(--hair)",
  background: "var(--soft)",
  color: "var(--ink-2)",
  borderRadius: 20,
  padding: "2px 9px",
};
const P_REF: React.CSSProperties = {
  fontFamily: P_MONO,
  fontSize: 12,
  color: "var(--navy)",
  background: "var(--soft-lime)",
  borderRadius: 4,
  padding: "2px 7px",
};

interface PainelParametrosProps {
  pipelineId: string | null;
  parametros: ParametroPipeline[];
  conexoes: Conexao[];
  podeSalvar: boolean;
  salvando: boolean;
  ok: boolean;
  erro: string | null;
  // Contador que muda a cada save do grafo (toolbar OU code area) — refaz o fetch dos nós SALVOS
  // p/ o lint refletir SQL persistido fora da aba Parâmetros.
  refrescarEm: number;
  onAdd: () => void;
  onRemover: (idx: number) => void;
  onEditar: (idx: number, patch: Partial<ParametroPipeline>) => void;
  // Declara um parâmetro JÁ referenciado por um nó (`:nome` no SQL) com o nome exato — o resumo
  // dizia "declare-o antes com o tipo certo" e deixava a digitação do nome (e o erro de digitação)
  // por conta de quem lê.
  onDeclarar: (nome: string) => void;
  onSalvar: () => void;
}

function PainelParametros({
  pipelineId,
  parametros,
  conexoes,
  podeSalvar,
  salvando,
  ok,
  erro,
  refrescarEm,
  onAdd,
  onRemover,
  onEditar,
  onDeclarar,
  onSalvar,
}: PainelParametrosProps) {
  // Conexões ordenadas para o select de origem dinâmica: as do tipo `arquivo` (mapas) primeiro.
  const conexoesOrdenadas = [...conexoes].sort((a, b) => {
    const pa = a.tipo === "arquivo" ? 0 : 1;
    const pb = b.tipo === "arquivo" ? 0 : 1;
    return pa - pb || a.nome.localeCompare(b.nome, "pt-BR");
  });

  // Nós do grafo SALVO — base do lint E do "usado em". Recarrega ao trocar de pipeline e após cada
  // salvamento bem-sucedido (`ok`/`refrescarEm`), p/ refletir SQL editado no canvas.
  const [nosSalvos, setNosSalvos] = useState<any[]>([]);
  // Três estados, não dois: com a leitura em voo — ou se ela falhou — a tela NÃO pode afirmar
  // "nenhum nó usa este parâmetro". Lista vazia por falha não é ausência de uso.
  const [estadoNos, setEstadoNos] = useState<"carregando" | "ok" | "falha">("ok");
  useEffect(() => {
    let vivo = true;
    if (!pipelineId) {
      setNosSalvos([]);
      setEstadoNos("ok");
      return;
    }
    setEstadoNos("carregando");
    api
      .obterPipeline(pipelineId)
      .then((det) => {
        if (!vivo) return;
        setNosSalvos(det.definicao?.nos ?? []);
        setEstadoNos("ok");
      })
      .catch(() => {
        if (!vivo) return;
        setNosSalvos([]);
        setEstadoNos("falha");
      });
    return () => {
      vivo = false;
    };
  }, [pipelineId, ok, refrescarEm]);
  const grafoConhecido = estadoNos === "ok";

  // Lint sobre os parâmetros EDITADOS (em memória) × os nós SALVOS.
  // "Sem nome" só é ERRO depois de uma tentativa de salvar: pintar de vermelho o bloco que a pessoa
  // acabou de criar culpa quem ainda não digitou (mesma regra do modal de execução). Antes disso
  // ele fica em "Conferir" — dito, sem acusar.
  const [tentouSalvar, setTentouSalvar] = useState(false);
  const avisos = lintarParametros(nosSalvos, parametros, grafoConhecido).map((a) =>
    a.codigo === "sem_nome" && !tentouSalvar ? { ...a, gravidade: "aviso" as GravidadeLint } : a,
  );
  const erros = avisos.filter((a) => a.gravidade === "erro");
  const alertas = avisos.filter((a) => a.gravidade === "aviso");

  // Onde cada parâmetro é USADO: nós do grafo salvo que o referenciam. Faltava na tela — o
  // especialista não tinha como saber o que quebra ao renomear/remover um parâmetro.
  const usosPorParam = useMemo(() => {
    const declarados = new Set(
      (parametros ?? []).map((p) => (p?.nome ?? "").trim()).filter((n) => n !== ""),
    );
    const mapa = new Map<string, { id: string; rotulo: string }[]>();
    for (const no of nosSalvos ?? []) {
      const id = String(no?.id ?? "");
      if (!id) continue;
      const rot = String(no?.rotulo ?? "").trim() || id;
      for (const ref of new Set(refsDoNoSalvo(no))) {
        // `:X_inicio`/`:X_fim` (janela da competência) contam como uso do parâmetro `X`.
        const base = declarados.has(ref) ? ref : (/^(.+)_(inicio|fim)$/.exec(ref)?.[1] ?? ref);
        const lista = mapa.get(base) ?? [];
        if (!lista.some((n) => n.id === id)) lista.push({ id, rotulo: rot });
        mapa.set(base, lista);
      }
    }
    return mapa;
  }, [nosSalvos, parametros]);

  // Rótulo/descrição (o que quem executa lê) ficam recolhidos: não mudam o resultado da execução e
  // dobravam a altura de cada bloco. NUNCA escondem dado preenchido — se algum parâmetro já tem
  // esses metadados, o grupo aparece aberto para todos.
  const [pedirForm, setPedirForm] = useState(false);
  const temMetaOperador = parametros.some(
    (p) => (p.rotulo ?? "").trim() !== "" || (p.descricao ?? "").trim() !== "",
  );
  const mostrarForm = pedirForm || temMetaOperador;

  // Remover parâmetro passa pela confirmação do design system: ele pode estar em uso por N nós, e
  // o "Remover" ficava a um clique de distância no meio de um formulário denso.
  const [remover, setRemover] = useState<number | null>(null);
  const paramRemover = remover != null ? parametros[remover] : undefined;

  // Foco no nome do parâmetro recém-criado — sem isto o clique em "adicionar" empurrava a página e
  // deixava o cursor no botão, longe do campo que a pessoa acabou de pedir.
  const [focarIdx, setFocarIdx] = useState<number | null>(null);
  useEffect(() => {
    if (focarIdx == null) return;
    irAoParametro(focarIdx);
    setFocarIdx(null);
  }, [focarIdx]);

  // Leva ao parâmetro: rola até o bloco e põe o cursor no NOME. Um pipeline com 8 parâmetros passa
  // de 4.000px — o resumo do topo dizia qual corrigir e deixava a procura por conta de quem lê.
  function irAoParametro(idx: number) {
    const campo = document.querySelector<HTMLInputElement>(`[data-testid="param-nome-${idx}"]`);
    if (!campo) return;
    campo.closest(".cfg-item")?.scrollIntoView({ block: "center", behavior: "smooth" });
    campo.focus();
  }

  // Quantos parâmetros VALEM (têm nome). Sem nome, o salvar descarta — contar como "declarado"
  // fazia o cabeçalho e a aba prometerem um parâmetro que o servidor nunca recebeu.
  const comNome = parametros.filter((p) => (p.nome ?? "").trim() !== "").length;
  const semNome = parametros.length - comNome;

  // Ação de cada linha do resumo. O botão herda a COR do banner (não usa `.lnk`, que é navy fixo):
  // dentro da faixa vermelha/creme do tema escuro um link navy fica ilegível.
  const acaoDoAviso = (a: AvisoLint) => {
    const estilo: React.CSSProperties = {
      background: "none",
      border: "none",
      padding: 0,
      font: "inherit",
      color: "inherit",
      textDecoration: "underline",
      cursor: "pointer",
    };
    if (a.codigo === "usado_nao_declarado")
      return (
        <button
          type="button"
          style={estilo}
          data-testid={`param-declarar-${a.alvo}`}
          onClick={() => {
            onDeclarar(a.alvo);
            setFocarIdx(parametros.length);
          }}
        >
          declarar :{a.alvo}
        </button>
      );
    if (a.idx == null) return null;
    return (
      <button type="button" style={estilo} onClick={() => irAoParametro(a.idx!)}>
        ir ao parâmetro
      </button>
    );
  };

  const baseLint =
    estadoNos === "carregando"
      ? "Conferindo contra o grafo salvo…"
      : estadoNos === "falha"
        ? "O grafo salvo não pôde ser lido: o uso de cada parâmetro NÃO foi conferido nesta lista."
        : "Conferido contra o grafo salvo — o que está no canvas sem salvar ainda não entra.";

  return (
    <div data-testid="painel-parametros" className="sbox" style={{ marginBottom: 16, padding: 16 }}>
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          justifyContent: "space-between",
          gap: 12,
          flexWrap: "wrap",
        }}
      >
        <h3 className="display" style={{ margin: 0, fontSize: 18 }}>
          Parâmetros
          {parametros.length > 0 && (
            <span style={{ fontSize: 13, fontWeight: 400, color: "var(--ink-3)" }}>
              {" "}
              · {comNome} {comNome === 1 ? "declarado" : "declarados"}
              {semNome > 0 && ` · ${semNome} sem nome (não salva)`}
            </span>
          )}
        </h3>
        <button
          data-testid="add-parametro"
          className="add"
          onClick={() => {
            onAdd();
            setFocarIdx(parametros.length);
          }}
        >
          + adicionar parâmetro
        </button>
      </div>
      <p style={{ ...P_AJUDA, margin: "6px 0 0", maxWidth: "82ch" }}>
        Use <code style={P_COD}>:nome</code> na consulta da fonte, no SQL do nó e na condição de uma
        operação guiada — sempre por vínculo, o valor nunca é colado no texto do SQL. Parâmetro{" "}
        <b>em branco não filtra</b>: o predicado que o compara é neutralizado. Se ele define o
        escopo da execução (operação, empresa, janela de datas), marque <b>obrigatório</b>.
      </p>

      {erro && (
        <div className="exec-banner-falha" role="alert" style={{ marginTop: 12 }}>
          <span className="dot" />
          <span>{erro}</span>
        </div>
      )}
      {ok && (
        <div className="run-banner" role="status" style={{ marginBottom: 0 }}>
          <span className="dot" />
          {/* O salvar DESCARTA parâmetro sem nome. Antes o banner dizia só "salvos" e o bloco
              continuava na tela: ao reabrir o pipeline o trabalho tinha sumido sem uma palavra. */}
          <span>
            {semNome === 0
              ? "Parâmetros salvos no rascunho."
              : comNome === 0
                ? `Rascunho salvo, mas nenhum parâmetro foi gravado: ${semNome} sem nome (dê um nome ou remova o bloco).`
                : `${comNome} ${comNome === 1 ? "parâmetro salvo" : "parâmetros salvos"} no rascunho — ${semNome} sem nome ${semNome === 1 ? "não foi gravado" : "não foram gravados"} (dê um nome ou remova o bloco).`}
          </span>
        </div>
      )}

      {/* LINT: parâmetros declarados × referências `:nome` nos nós + o que o backend recusa no
          salvar. `role=status` porque o conteúdo muda enquanto a pessoa digita, sem mover o foco.
          As classes trazem o tema escuro pronto — as cores fixas de antes viravam um bloco creme
          sobre fundo escuro. */}
      <div
        data-testid="param-lint"
        role="status"
        aria-live="polite"
        className={
          parametros.length === 0
            ? ""
            : erros.length
              ? "exec-banner-falha"
              : alertas.length
                ? "exec-aviso"
                : "run-banner"
        }
        style={{
          marginTop: 12,
          marginBottom: 0,
          display: "block",
          fontSize: 12.5,
          // Sem parâmetro não há o que conferir: uma faixa colorida aqui só concorreria com o
          // estado vazio logo abaixo. O bloco continua no DOM (mesmo `data-testid`).
          ...(parametros.length === 0 ? P_AJUDA : null),
        }}
      >
        {parametros.length === 0 ? (
          <div>Nada a conferir: nenhum parâmetro declarado.</div>
        ) : avisos.length === 0 ? (
          <div>
            <b>Nada a corrigir</b> nesta lista.
          </div>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {erros.length > 0 && (
              <div>
                <strong>Corrigir ({erros.length}):</strong>
                <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
                  {erros.map((a, i) => (
                    <li key={`${a.codigo}:${a.alvo}:${a.idx ?? i}`} style={{ marginTop: 2 }}>
                      {a.mensagem} {acaoDoAviso(a)}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {alertas.length > 0 && (
              <div>
                <strong>Conferir ({alertas.length}):</strong>
                <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
                  {alertas.map((a, i) => (
                    <li key={`${a.codigo}:${a.alvo}:${a.idx ?? i}`} style={{ marginTop: 2 }}>
                      {a.mensagem} {acaoDoAviso(a)}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}
        {parametros.length > 0 && <div style={{ marginTop: 6, opacity: 0.8 }}>{baseLint}</div>}
      </div>

      {/* ÍNDICE. Cada bloco tem ~400px: com 8 parâmetros o painel passa de 3.900px e não existia
          visão do conjunto nem como chegar a um deles sem rolar procurando. Marcado por TEXTO, não
          por cor — cor fixa aqui foi o que já quebrou este painel no tema escuro. */}
      {parametros.length >= 4 && (
        <div
          data-testid="param-indice"
          style={{ marginTop: 12, display: "flex", gap: 6, alignItems: "baseline", flexWrap: "wrap" }}
        >
          <span className="fld" style={{ marginBottom: 0 }}>
            Ir para
          </span>
          {parametros.map((p, i) => {
            const n = (p.nome ?? "").trim();
            const marcas = [
              p.derivacao ? "derivado" : null,
              p.obrigatorio && !p.derivacao ? "obrigatório" : null,
              p.segredo ? "credencial" : null,
            ].filter(Boolean);
            const temErro = erros.some((a) => a.idx === i || (n !== "" && a.alvo === n));
            return (
              <button
                key={i}
                type="button"
                onClick={() => irAoParametro(i)}
                title={`${n || `${i + 1}º parâmetro (sem nome)`} · ${p.tipo}${marcas.length ? ` · ${marcas.join(" · ")}` : ""}`}
                style={{
                  ...P_CHIP,
                  fontFamily: P_MONO,
                  cursor: "pointer",
                  gap: 5,
                  borderStyle: temErro ? "dashed" : "solid",
                }}
              >
                {n || `${i + 1}º sem nome`}
                {marcas.length > 0 && (
                  // Abreviar a marca ("obriga.") economiza pixel e cobra decifração: cabe inteira.
                  <span style={{ fontFamily: "inherit", color: "var(--ink-3)" }}>· {marcas[0]}</span>
                )}
                {temErro && <span style={{ color: "var(--ink-2)" }}>· corrigir</span>}
              </button>
            );
          })}
        </div>
      )}

      {parametros.length === 0 ? (
        <div
          style={{
            marginTop: 14,
            padding: "18px 16px",
            border: "1px dashed var(--hair)",
            borderRadius: 8,
            background: "var(--soft)",
          }}
        >
          <div style={{ fontSize: 13, color: "var(--ink-2)" }}>
            Nenhum parâmetro declarado — este pipeline roda sempre com o mesmo recorte.
          </div>
          <div style={{ ...P_AJUDA, marginTop: 4, maxWidth: "72ch" }}>
            Declare um para escolher o recorte (operação, competência, janela de datas) na hora de
            executar. Escrever <code style={P_COD}>:nome</code> no SQL de um nó também declara o
            parâmetro no salvar — aqui você acerta o tipo e se ele é obrigatório.
          </div>
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 12, marginTop: 12 }}>
          {parametros.map((p, idx) => {
            const nome = (p.nome ?? "").trim();
            const usos = nome ? (usosPorParam.get(nome) ?? []) : [];
            const idNotas = `param-notas-${idx}`;
            // O erro tem de aparecer ONDE se corrige: com 6 parâmetros na tela, a lista no topo
            // fica fora de vista. O resumo continua lá (é a visão do conjunto). Casa por ÍNDICE
            // também: o bloco sem nome não tem por onde casar pelo nome, e era justamente ele que
            // o salvar descartava calado.
            const errosDoParam = erros.filter((a) => a.idx === idx || (nome !== "" && a.alvo === nome));
            const alertasDoParam = alertas.filter(
              (a) => a.idx === idx && (a.codigo === "default_invalido" || a.codigo === "sem_nome"),
            );
            const nomeInvalido = errosDoParam.some(
              (a) =>
                a.codigo === "nome_invalido" || a.codigo === "nome_duplicado" || a.codigo === "sem_nome",
            );
            // Nome fora do padrão não é referência usável: mostrar `:nome inválido` como se fosse
            // colável no SQL é a tela sugerindo o que o parser recusa.
            const refUsavel = nome !== "" && NOME_PARAM_OK.test(nome);
            // Opções DIGITADAS: o valor padrão/preview tem de sair da lista. Texto livre aqui é
            // 422 no salvar ("Default … não está entre as opções") ou campo em branco no Executar.
            const opcoesFixas = p.tipo === "opcoes" && !p.opcoes_fonte ? (p.opcoes ?? []).filter((o) => o.trim() !== "") : [];
            const temMetaPreenchida =
              (p.rotulo ?? "").trim() !== "" || (p.descricao ?? "").trim() !== "";
            // Uma frase que descreve o parâmetro COMO ELE ESTÁ — as marcas mudam o contrato dele
            // (erro em branco, valor redigido, oculto ao operador) e isso só existia em `title`,
            // invisível para teclado e toque.
            const notas: string[] = [
              p.derivacao
                ? "Derivado: calculado a partir de outro parâmetro; o operador não o preenche (e «obrigatório» não se aplica)."
                : p.obrigatorio
                  ? "Obrigatório: em branco, a execução para com erro em vez de ler a base inteira."
                  : "Opcional: em branco não filtra — a leitura vem sem esse recorte.",
            ];
            if (p.segredo)
              notas.push(
                "Credencial: o valor é redigido na trilha e no histórico da execução, não fica guardado na definição e não pode ser usado em agendamento.",
              );
            // Fonte de opções vale para QUALQUER tipo (ex.: `entidade_id` inteiro com dropdown
            // id+apelido) e o painel só a mostrava no tipo "opções" — quem tivesse a fonte salva
            // em outro tipo não a via nem podia editá-la.
            const temFonteOpcoes = !!p.opcoes_fonte;
            const mostrarOpcoes = p.tipo === "opcoes" || temFonteOpcoes;

            return (
              <div
                key={idx}
                className="cfg-item"
                role="group"
                aria-label={`Parâmetro ${nome || idx + 1}`}
                style={{ display: "flex", flexDirection: "column", gap: 10 }}
              >
                {/* Identidade: o que o parâmetro É. */}
                <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end" }}>
                  <label style={{ display: "flex", flexDirection: "column", flex: "1 1 220px", maxWidth: 340 }}>
                    <span className="fld">Nome</span>
                    <input
                      className="nm"
                      data-testid={`param-nome-${idx}`}
                      style={{ fontWeight: 400, fontFamily: P_MONO }}
                      value={p.nome}
                      placeholder="ex.: data_inicio"
                      aria-invalid={nomeInvalido || undefined}
                      aria-describedby={errosDoParam.length ? `param-erros-${idx}` : undefined}
                      onChange={(e) => onEditar(idx, { nome: e.target.value })}
                    />
                  </label>
                  <label style={{ display: "flex", flexDirection: "column", flex: "0 1 180px" }}>
                    <span className="fld">Tipo</span>
                    <select
                      className="nm"
                      style={{ fontWeight: 400 }}
                      value={p.tipo}
                      onChange={(e) =>
                        onEditar(idx, { tipo: e.target.value as ParametroPipeline["tipo"] })
                      }
                    >
                      {TIPOS_PARAMETRO.map(([t, l]) => (
                        <option key={t} value={t}>
                          {l}
                        </option>
                      ))}
                    </select>
                  </label>
                  <div
                    style={{
                      marginLeft: "auto",
                      display: "flex",
                      alignItems: "center",
                      gap: 10,
                      paddingBottom: 1,
                    }}
                  >
                    {p.tipo !== "competencia" && refUsavel && (
                      <code style={P_REF} title="Como referenciar este parâmetro num nó">
                        :{nome}
                      </code>
                    )}
                    <button
                      type="button"
                      className="btn-remover-param"
                      onClick={() => (nome ? setRemover(idx) : onRemover(idx))}
                      aria-label={`Remover parâmetro ${nome || idx + 1}`}
                    >
                      Remover
                    </button>
                  </div>
                </div>

                {/* Marcas: mudam o CONTRATO do parâmetro. A frase abaixo descreve o estado atual. */}
                <div style={{ display: "flex", gap: 18, flexWrap: "wrap", alignItems: "center" }}>
                  <label style={P_MARCA}>
                    <input
                      type="checkbox"
                      data-testid={`param-obrigatorio-${idx}`}
                      checked={!!p.obrigatorio}
                      disabled={!!p.derivacao}
                      aria-describedby={idNotas}
                      onChange={(e) => onEditar(idx, { obrigatorio: e.target.checked })}
                    />
                    obrigatório
                  </label>
                  <label style={P_MARCA}>
                    <input
                      type="checkbox"
                      data-testid={`param-segredo-${idx}`}
                      checked={!!p.segredo}
                      aria-describedby={idNotas}
                      onChange={(e) => onEditar(idx, { segredo: e.target.checked })}
                    />
                    credencial
                  </label>
                  <label style={P_MARCA}>
                    <input
                      type="checkbox"
                      data-testid={`param-derivado-${idx}`}
                      checked={!!p.derivacao}
                      aria-describedby={idNotas}
                      onChange={(e) =>
                        onEditar(idx, {
                          derivacao: e.target.checked
                            ? { de: "", mapa: "", campo_id: "", campo_valor: "" }
                            : undefined,
                        })
                      }
                    />
                    derivado de outro parâmetro
                  </label>
                  {/* Onde ele é usado — o que quebra se você renomear/remover. Na mesma linha das
                      marcas p/ não custar mais uma faixa de altura em cada bloco. Sem nome não há
                      o que procurar: aí a linha não fala do assunto. */}
                  {nome !== "" && (
                    <div
                      style={{
                        marginLeft: "auto",
                        display: "flex",
                        gap: 6,
                        alignItems: "center",
                        flexWrap: "wrap",
                      }}
                    >
                      <span className="fld" style={{ marginBottom: 0 }}>
                        Usado em
                      </span>
                      {!grafoConhecido ? (
                        <span style={P_AJUDA}>
                          {estadoNos === "carregando"
                            ? "conferindo o grafo salvo…"
                            : "grafo salvo não lido — não dá para dizer"}
                        </span>
                      ) : usos.length === 0 ? (
                        <span style={P_AJUDA}>
                          nenhum nó referencia <code style={P_COD}>:{nome}</code>
                        </span>
                      ) : (
                        usos.map((n) => (
                          <span key={n.id} style={P_CHIP} title={`id do nó: ${n.id}`}>
                            {n.rotulo}
                          </span>
                        ))
                      )}
                    </div>
                  )}
                </div>
                {errosDoParam.length > 0 && (
                  <div
                    id={`param-erros-${idx}`}
                    className="exec-banner-falha"
                    style={{ padding: "7px 11px", fontSize: 12, display: "block" }}
                  >
                    {errosDoParam.map((a, i) => (
                      <div key={`${a.codigo}:${i}`}>{a.mensagem}</div>
                    ))}
                  </div>
                )}
                {alertasDoParam.length > 0 && (
                  <div className="exec-aviso" style={{ padding: "7px 11px", fontSize: 12 }}>
                    {alertasDoParam.map((a, i) => (
                      <div key={`${a.codigo}:${i}`}>{a.mensagem}</div>
                    ))}
                  </div>
                )}
                <p id={idNotas} style={{ ...P_AJUDA, margin: 0, maxWidth: "88ch" }}>
                  {notas.join(" ")}
                </p>

                {/* Valores. */}
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "repeat(auto-fit, minmax(230px, 1fr))",
                    gap: 10,
                  }}
                >
                  <label style={{ display: "flex", flexDirection: "column" }}>
                    <span className="fld">Valor padrão</span>
                    {/* Opções DIGITADAS: escolher da lista, não digitar. Texto livre aqui virava 422
                        no salvar ("Default … não está entre as opções") — o campo aceitava o que o
                        produto recusa. Valor salvo fora da lista continua visível (e marcado). */}
                    {opcoesFixas.length > 0 && !p.segredo ? (
                      <select
                        className="nm"
                        style={{ fontWeight: 400 }}
                        value={temValor(p.default) ? String(p.default) : ""}
                        onChange={(e) => onEditar(idx, { default: e.target.value })}
                      >
                        <option value="">— sem padrão (em branco = sem filtro) —</option>
                        {opcoesFixas.map((o) => (
                          <option key={o} value={o}>
                            {o}
                          </option>
                        ))}
                        {temValor(p.default) && !opcoesFixas.includes(String(p.default)) && (
                          <option value={String(p.default)}>
                            {String(p.default)} — fora das opções
                          </option>
                        )}
                      </select>
                    ) : (
                      <input
                        className="nm"
                        style={{ fontWeight: 400 }}
                        // Credencial não tem "valor padrão": ele iria para a definição versionada.
                        // Derivado de calendário também não: o valor é função da
                        // competência — o campo só abre para apagar um padrão antigo.
                        disabled={
                          !!p.segredo ||
                          (p.derivacao?.origem === "competencia" && !temValor(p.default))
                        }
                        inputMode={p.tipo === "inteiro" ? "numeric" : undefined}
                        value={p.segredo ? "" : temValor(p.default) ? String(p.default) : ""}
                        placeholder={
                          p.segredo
                            ? "credencial não tem valor padrão"
                            : p.obrigatorio
                              ? "deixe vazio — é obrigatório"
                              : p.tipo === "competencia"
                                ? "atual, anterior ou 2026-05"
                                : p.tipo === "as_of"
                                  ? "hoje, ontem, 2026-05 ou data/hora"
                                  : p.tipo === "data"
                                    ? "AAAA-MM-DD"
                                    : p.tipo === "inteiro"
                                      ? "só dígitos (ex.: 61)"
                                      : "em branco = sem filtro"
                        }
                        onChange={(e) => onEditar(idx, { default: e.target.value })}
                      />
                    )}
                    <small style={{ ...P_AJUDA, marginTop: 4 }}>
                      {p.segredo
                        ? "Desabilitado: um padrão iria para a definição versionada (permanente)."
                        : p.derivacao?.origem === "competencia"
                          ? "Derivado da competência: sem padrão — a plataforma calcula a cada execução."
                          : p.derivacao
                            ? "Derivado: com valor aqui o mapa NÃO roda — este valor substitui o cálculo."
                            : p.obrigatorio
                              ? "Com padrão, «obrigatório» nunca acusa falta: o padrão entra antes."
                              : "Vale na execução, não só no preview."}
                    </small>
                  </label>
                  <label style={{ display: "flex", flexDirection: "column" }}>
                    <span className="fld">Valor de preview</span>
                    {opcoesFixas.length > 0 && !p.segredo ? (
                      <select
                        className="nm"
                        style={{ fontWeight: 400 }}
                        value={temValor(p.valor_preview) ? String(p.valor_preview) : ""}
                        onChange={(e) => onEditar(idx, { valor_preview: e.target.value })}
                      >
                        <option value="">— sem valor de preview —</option>
                        {opcoesFixas.map((o) => (
                          <option key={o} value={o}>
                            {o}
                          </option>
                        ))}
                        {temValor(p.valor_preview) &&
                          !opcoesFixas.includes(String(p.valor_preview)) && (
                            <option value={String(p.valor_preview)}>
                              {String(p.valor_preview)} — fora das opções
                            </option>
                          )}
                      </select>
                    ) : (
                      <input
                        className="nm"
                        style={{ fontWeight: 400 }}
                        // `password` mesmo aqui: o valor não é persistido para credencial, mas
                        // digitá-lo em claro deixa a senha na tela (e o navegador oferece salvar).
                        type={p.segredo ? "password" : "text"}
                        autoComplete={p.segredo ? "new-password" : undefined}
                        value={temValor(p.valor_preview) ? String(p.valor_preview) : ""}
                        placeholder={p.segredo ? "só nesta sessão" : "só no Studio"}
                        onChange={(e) => onEditar(idx, { valor_preview: e.target.value })}
                      />
                    )}
                    <small style={{ ...P_AJUDA, marginTop: 4 }}>
                      {p.segredo
                        ? "Só nesta sessão: não é gravado na definição nem na trilha."
                        : p.derivacao
                          ? "Só no preview: com valor aqui o preview usa ele em vez de resolver o mapa."
                          : "Preenche o preview e sugere o valor ao Executar daqui; o Portal não o recebe."}
                    </small>
                  </label>
                </div>

                {/* O que quem executa lê. Recolhido por padrão (ver `mostrarForm`). Parâmetro
                    DERIVADO não tem campo em lugar nenhum (o Executar do Studio filtra derivados e
                    a Página só lista o nome): oferecer "rótulo" e "dica ao operador" ali era a tela
                    prometendo uma tela que ninguém vê. Se já houver valor salvo, o campo aparece —
                    com o aviso — porque esconder dado preenchido é pior. */}
                {mostrarForm && (!p.derivacao || temMetaPreenchida) ? (
                  <div
                    style={{
                      display: "grid",
                      gridTemplateColumns: "repeat(auto-fit, minmax(230px, 1fr))",
                      gap: 10,
                      borderTop: "1px dashed var(--hair)",
                      paddingTop: 10,
                    }}
                  >
                    {p.derivacao && (
                      <div style={{ ...P_AJUDA, gridColumn: "1 / -1", margin: 0 }}>
                        Derivado é oculto a quem executa: rótulo e descrição abaixo <b>não</b>{" "}
                        aparecem em nenhuma tela — ficam só como anotação da definição.
                      </div>
                    )}
                    <label style={{ display: "flex", flexDirection: "column" }}>
                      <span className="fld">Rótulo (no Executar do Studio)</span>
                      <input
                        className="nm"
                        style={{ fontWeight: 400 }}
                        value={p.rotulo ?? ""}
                        placeholder={nome || "ex.: Operação"}
                        onChange={(e) => onEditar(idx, { rotulo: e.target.value })}
                      />
                      <small style={{ ...P_AJUDA, marginTop: 4 }}>
                        No Portal quem manda é o rótulo definido na Página; em branco lá, o operador
                        lê «{nome || "nome"}».
                      </small>
                    </label>
                    <label style={{ display: "flex", flexDirection: "column" }}>
                      <span className="fld">Descrição (ajuda ao operador)</span>
                      <input
                        className="nm"
                        style={{ fontWeight: 400 }}
                        value={p.descricao ?? ""}
                        placeholder="texto de ajuda sob o campo, no Portal"
                        onChange={(e) => onEditar(idx, { descricao: e.target.value })}
                      />
                      <small style={{ ...P_AJUDA, marginTop: 4 }}>
                        Aparece como dica do campo no Portal, junto do contrato do vazio.
                      </small>
                    </label>
                  </div>
                ) : null}

                {mostrarOpcoes &&
                  (() => {
                    const dinamico = temFonteOpcoes;
                    return (
                      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                        {/* Origem das opções: lista digitada (estática) ou valores vindos do dado. */}
                        <div style={{ display: "flex", gap: 16, fontSize: 13, alignItems: "center", flexWrap: "wrap" }}>
                          <span className="fld" style={{ marginBottom: 0 }}>
                            Opções do campo
                          </span>
                          <label style={{ display: "flex", alignItems: "center", gap: 6 }}>
                            <input
                              type="radio"
                              name={`opcoes-origem-${idx}`}
                              checked={!dinamico}
                              onChange={() => onEditar(idx, { opcoes_fonte: undefined })}
                            />
                            {p.tipo === "opcoes" ? "lista digitada" : "sem dropdown (digita o valor)"}
                          </label>
                          <label style={{ display: "flex", alignItems: "center", gap: 6 }}>
                            <input
                              type="radio"
                              name={`opcoes-origem-${idx}`}
                              checked={dinamico}
                              onChange={() =>
                                onEditar(idx, {
                                  opcoes_fonte: { conexao_id: "", coluna_valor: "" },
                                  opcoes: [],
                                })
                              }
                            />
                            lista vinda do dado
                          </label>
                        </div>

                        {dinamico ? (
                          (() => {
                            // Merge que preserva os demais campos e MIGRA o legado `coluna` → `coluna_valor`
                            // (canônico daqui pra frente; o backend já prefere coluna_valor). Ao editar,
                            // largamos `coluna` pra não duplicar a fonte de verdade.
                            const f = p.opcoes_fonte ?? { conexao_id: "" };
                            const patch = (campos: Partial<import("../../api/client").OpcoesFonte>) =>
                              onEditar(idx, { opcoes_fonte: { ...f, coluna: undefined, ...campos } });
                            const valorAtual = f.coluna_valor ?? f.coluna ?? "";
                            const cxSel = conexoesOrdenadas.find((c) => c.id === f.conexao_id);
                            const ehArquivo = cxSel?.tipo === "arquivo";
                            const origemFonte = f.origem ?? "conexao";
                            // Nós elegíveis como fonte de opções (transformação/leitura; efeito/código fora).
                            const nosOpcoes = (nosSalvos ?? []).filter((n) =>
                              ["fonte", "sql", "guiada", "comparacao", "saida"].includes(n?.tipo),
                            );
                            const rotuloNo = (n: any) => (n?.rotulo?.trim() ? `${n.rotulo} (${n.id})` : n.id);
                            return (
                              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                                {/* Sem `flex-basis` aqui: o contêiner é COLUNA, então basis vira
                                    ALTURA — o `flex:1 1 200px` de antes abria 200px de vazio no
                                    meio do bloco (só não aparecia porque o bloco nunca abria). */}
                                <label style={{ display: "flex", flexDirection: "column", maxWidth: 260 }}>
                                  <span className="fld">Origem das opções</span>
                                  <select
                                    className="nm"
                                    style={{ fontWeight: 400 }}
                                    value={origemFonte}
                                    onChange={(e) =>
                                      patch({ origem: e.target.value as "conexao" | "no" })
                                    }
                                  >
                                    <option value="conexao">Conexão</option>
                                    <option value="no">Nó da pipeline</option>
                                  </select>
                                </label>
                                <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end" }}>
                                  {origemFonte === "no" ? (
                                    <label style={{ display: "flex", flexDirection: "column", flex: "1 1 220px" }}>
                                      <span className="fld">Nó da pipeline (saída = opções)</span>
                                      <select
                                        className="nm"
                                        style={{ fontWeight: 400 }}
                                        value={f.no_id ?? ""}
                                        onChange={(e) => patch({ no_id: e.target.value })}
                                      >
                                        <option value="">Selecione o nó</option>
                                        {nosOpcoes.map((n) => (
                                          <option key={n.id} value={n.id}>
                                            {rotuloNo(n)}
                                          </option>
                                        ))}
                                        {f.no_id && !nosOpcoes.some((n) => n.id === f.no_id) && (
                                          <option value={f.no_id}>{f.no_id}</option>
                                        )}
                                      </select>
                                    </label>
                                  ) : (
                                    <label style={{ display: "flex", flexDirection: "column", flex: "1 1 220px" }}>
                                      <span className="fld">Conexão</span>
                                      <select
                                        className="nm"
                                        style={{ fontWeight: 400 }}
                                        value={f.conexao_id ?? ""}
                                        onChange={(e) => patch({ conexao_id: e.target.value })}
                                      >
                                        <option value="">Selecione a conexão</option>
                                        {conexoesOrdenadas.map((c) => (
                                          <option key={c.id} value={c.id}>
                                            {c.nome}
                                            {c.tipo === "arquivo" ? "" : ` (${c.tipo})`}
                                          </option>
                                        ))}
                                      </select>
                                    </label>
                                  )}
                                  <label style={{ display: "flex", flexDirection: "column", flex: "1 1 180px" }}>
                                    <span className="fld">Coluna do valor</span>
                                    <input
                                      className="nm"
                                      style={{ fontWeight: 400, fontFamily: P_MONO }}
                                      value={valorAtual}
                                      placeholder="ex.: operacaoid"
                                      onChange={(e) => patch({ coluna_valor: e.target.value })}
                                    />
                                  </label>
                                  <label style={{ display: "flex", flexDirection: "column", flex: "1 1 180px" }}>
                                    <span className="fld">Coluna do rótulo (opcional)</span>
                                    <input
                                      className="nm"
                                      style={{ fontWeight: 400, fontFamily: P_MONO }}
                                      value={f.coluna_rotulo ?? ""}
                                      placeholder="ex.: apelidoOperacao"
                                      onChange={(e) => patch({ coluna_rotulo: e.target.value })}
                                    />
                                  </label>
                                </div>
                                {origemFonte === "conexao" && (
                                  <label style={{ display: "flex", flexDirection: "column" }}>
                                    <span className="fld">
                                      Consulta SQL {ehArquivo ? "(opcional — arquivo lê a aba toda)" : "(recomendado p/ banco/gateway)"}
                                    </span>
                                    <input
                                      className="nm"
                                      style={{ fontWeight: 400, fontFamily: P_MONO }}
                                      value={f.sql ?? ""}
                                      placeholder="ex.: SELECT operacaoid, apelidoOperacao FROM operacoes WHERE ativo = 1"
                                      onChange={(e) => patch({ sql: e.target.value })}
                                    />
                                  </label>
                                )}
                                <span style={P_AJUDA}>
                                  Rótulo≠valor: mostra a <b>coluna do rótulo</b> a quem executa e envia a{" "}
                                  <b>coluna do valor</b>.{" "}
                                  {origemFonte === "no"
                                    ? "A saída do nó (já filtrada na pipeline) vira o dropdown."
                                    : "Sem SQL, lê a conexão inteira (ok p/ arquivo)."}
                                </span>
                              </div>
                            );
                          })()
                        ) : (
                          <label style={{ display: "flex", flexDirection: "column" }}>
                            <span className="fld">Opções (separadas por vírgula)</span>
                            <input
                              className="nm"
                              style={{ fontWeight: 400 }}
                              value={(p.opcoes ?? []).join(", ")}
                              placeholder="ex.: A, B, C"
                              onChange={(e) =>
                                onEditar(idx, {
                                  opcoes: e.target.value
                                    .split(",")
                                    .map((o) => o.trim())
                                    .filter(Boolean),
                                })
                              }
                            />
                          </label>
                        )}
                      </div>
                    );
                  })()}
                {/* Derivado ninguém preenche — oferecer dropdown a ele seria oferta falsa. Idem
                    credencial: uma lista de senhas lida de uma conexão não é coisa que se ofereça. */}
                {!mostrarOpcoes && !p.derivacao && !p.segredo && (
                  <button
                    type="button"
                    className="lnk"
                    onClick={() =>
                      onEditar(idx, { opcoes_fonte: { conexao_id: "", coluna_valor: "" } })
                    }
                  >
                    + oferecer uma lista de valores (dropdown)
                  </button>
                )}
                {p.tipo === "competencia" && (
                  <div style={{ ...P_AJUDA, maxWidth: "88ch" }}>
                    Aceita <code style={P_COD}>atual</code>, <code style={P_COD}>anterior</code> ou{" "}
                    <code style={P_COD}>YYYY-MM</code>. Nos nós, o parâmetro NÃO entra como{" "}
                    <code style={P_COD}>:{p.nome || "nome"}</code>: use a janela meio-aberta{" "}
                    <code style={P_COD}>:{p.nome || "nome"}_inicio</code> e{" "}
                    <code style={P_COD}>:{p.nome || "nome"}_fim</code> (1º do mês → 1º do mês
                    seguinte).
                  </div>
                )}
                {p.tipo === "as_of" && (
                  <div style={{ ...P_AJUDA, maxWidth: "88ch" }}>
                    Corte de tempo de <b>sistema</b> (não de negócio): a fonte filtra{" "}
                    <code style={P_COD}>DATA_REGISTRO &lt;= :{p.nome || "nome"}</code> para ver o dado
                    como ele estava. Aceita <code style={P_COD}>hoje</code>,{" "}
                    <code style={P_COD}>ontem</code>, <code style={P_COD}>YYYY-MM</code>, data ou
                    data/hora.
                  </div>
                )}
                {p.derivacao && (
                  <CamposDerivacao
                    d={p.derivacao}
                    conexoes={conexoes}
                    nos={nosSalvos}
                    parametros={parametros}
                    nomeParam={p.nome}
                    onPatch={(patch) => onEditar(idx, { derivacao: { ...p.derivacao!, ...patch } })}
                  />
                )}
              </div>
            );
          })}
        </div>
      )}

      {parametros.length > 0 && !mostrarForm && (
        <button
          type="button"
          className="lnk"
          style={{ marginTop: 10 }}
          onClick={() => setPedirForm(true)}
        >
          + rótulo e descrição de cada parâmetro
        </button>
      )}

      {/* Rodapé no fluxo, NÃO `sticky`: uma barra fixa aqui fica por cima do último bloco de
          parâmetro (fundo da mesma cor do cartão) e o formulário parece terminar antes da hora. */}
      <div
        style={{
          marginTop: 14,
          paddingTop: 12,
          borderTop: "1px solid var(--hair-2)",
          display: "flex",
          alignItems: "center",
          gap: 12,
          flexWrap: "wrap",
        }}
      >
        <button
          type="button"
          className="btn-stamp"
          data-testid="btn-salvar-params"
          disabled={salvando || !podeSalvar}
          onClick={() => {
            // A partir daqui "sem nome" é erro (não mais aviso) e o cursor vai para o campo que
            // falta — o bloco descartado podia estar a três telas de distância do botão.
            setTentouSalvar(true);
            const primeiro = parametros.findIndex((p) => (p.nome ?? "").trim() === "");
            if (primeiro >= 0) requestAnimationFrame(() => irAoParametro(primeiro));
            onSalvar();
          }}
        >
          <Icon name="stamp" cls="ic" /> {salvando ? "Salvando…" : "Salvar parâmetros"}
        </button>
        {/* Com erro na lista o salvar é recusado pelo BACKEND (422) e leva a definição inteira, nós
            inclusive. O botão continua clicável — o servidor é a autoridade —, mas a tela para de
            deixar a pessoa descobrir isso só depois de clicar. */}
        <span style={P_AJUDA}>
          {!podeSalvar
            ? "Crie ou abra um pipeline para salvar os parâmetros."
            : erros.length > 0
              ? `${erros.length} ${erros.length === 1 ? "erro acima impede" : "erros acima impedem"} o salvar (a definição toda é recusada, nós inclusive).`
              : "Grava no rascunho do pipeline (versão nova só ao publicar)."}
        </span>
      </div>

      {paramRemover && remover != null && (
        <ConfirmarAcao
          titulo="Remover parâmetro"
          corpo={
            <>
              Remover <b>«{paramRemover.nome}»</b>?{" "}
              {(() => {
                const u = usosPorParam.get((paramRemover.nome ?? "").trim()) ?? [];
                if (!grafoConhecido)
                  return "Não foi possível ler o grafo salvo para conferir quem o usa.";
                if (u.length === 0) return "Nenhum nó do grafo salvo o referencia.";
                return `${u.length} ${u.length === 1 ? "nó o referencia" : "nós o referenciam"} (${u
                  .map((n) => n.rotulo)
                  .join(", ")}): ao salvar, o parâmetro é recriado como «texto» opcional a partir do :nome que ficou no SQL.`;
              })()}{" "}
              A remoção só vale depois de salvar.
            </>
          }
          rotuloConfirmar="Remover"
          perigo
          onConfirmar={() => {
            onRemover(remover);
            setRemover(null);
          }}
          onCancelar={() => setRemover(null)}
        />
      )}
    </div>
  );
}

// ============================================================================
// TituloEditavel — h1 do cabeçalho com edição in-line (lápis). Confirma no Enter
// ou ao perder o foco; Esc cancela. Persiste via onConfirmar (PATCH no backend).
// ============================================================================
function TituloEditavel({
  valor,
  podeEditar,
  onConfirmar,
}: {
  valor: string;
  podeEditar: boolean;
  onConfirmar: (novo: string) => void;
}) {
  const [editando, setEditando] = useState(false);
  const [rascunho, setRascunho] = useState(valor);

  const abrir = () => {
    setRascunho(valor);
    setEditando(true);
  };
  const confirmar = () => {
    setEditando(false);
    onConfirmar(rascunho);
  };

  if (!editando) {
    return (
      <h1 className="display" style={{ display: "flex", alignItems: "center", gap: 8 }}>
        {valor}
        {podeEditar && (
          <button
            data-testid="renomear-pipeline"
            title="Renomear"
            onClick={abrir}
            style={{
              border: "none",
              background: "transparent",
              cursor: "pointer",
              color: "var(--ink-3)",
              fontSize: 16,
              padding: 2,
              lineHeight: 1,
            }}
          >
            ✎
          </button>
        )}
      </h1>
    );
  }

  return (
    <input
      aria-label="Nome do pipeline"
      data-testid="renomear-pipeline-input"
      autoFocus
      value={rascunho}
      onChange={(e) => setRascunho(e.target.value)}
      onBlur={confirmar}
      onKeyDown={(e) => {
        if (e.key === "Enter") confirmar();
        if (e.key === "Escape") setEditando(false);
      }}
      style={{
        fontFamily: '"Space Grotesk"',
        fontWeight: 600,
        fontSize: 28,
        border: "1px solid var(--hair)",
        borderRadius: 6,
        padding: "2px 8px",
        color: "var(--ink)",
        background: "var(--surface)",
        maxWidth: 480,
      }}
    />
  );
}

// "2026-08" deslocado de `meses` meses → "AAAA-MM-01". Só o EXEMPLO do texto de ajuda: quem calcula
// de verdade é a plataforma (`periodos.deslocar_competencia`), na execução.
function exemploDeslocado(meses: number | undefined): string | null {
  if (meses === undefined || !Number.isInteger(meses) || meses === 0) return null;
  const total = 2026 * 12 + 7 + meses; // agosto = mês de índice 7
  const ano = Math.floor(total / 12);
  const mes = total - ano * 12 + 1;
  if (ano < 1 || ano > 9999) return null;
  return `${String(ano).padStart(4, "0")}-${String(mes).padStart(2, "0")}-01`;
}

// ============================================================================
// CamposDerivacao — campos de um parâmetro DERIVADO (de outro via mapa, ou da competência
// deslocada).
// Um parâmetro com derivação não é mostrado ao operador; o Portal o resolve dos mapas.
// ============================================================================
interface CamposDerivacaoProps {
  d: DerivacaoParametro;
  conexoes: Conexao[];
  nos: any[]; // nós do grafo salvo (para a origem "no")
  parametros: ParametroPipeline[];
  nomeParam: string;
  onPatch: (patch: Partial<DerivacaoParametro>) => void;
}

function CamposDerivacao({ d, conexoes, nos, parametros, nomeParam, onPatch }: CamposDerivacaoProps) {
  const origem = d.origem ?? "arquivo";
  // Derivado de CALENDÁRIO: sem mapa — a competência `de` deslocada de `meses` meses.
  const ehCompetencia = origem === "competencia";
  // Parâmetros de ORIGEM elegíveis: os demais (não derivados, com nome) deste pipeline — e, no de
  // calendário, só os do tipo competência (a validação recusa qualquer outro).
  const elegivel = (x: ParametroPipeline) =>
    x.nome.trim() !== "" && x.nome !== nomeParam && !x.derivacao;
  const origens = parametros.filter(
    (x) => elegivel(x) && (!ehCompetencia || x.tipo === "competencia"),
  );
  const baseInput = { className: "nm", style: { fontWeight: 400 as const } };
  const trocarOrigem = (nova: NonNullable<DerivacaoParametro["origem"]>) => {
    if (nova !== "competencia") return onPatch({ origem: nova });
    const deValido = parametros.some(
      (x) => elegivel(x) && x.tipo === "competencia" && x.nome === d.de,
    );
    onPatch({ origem: nova, de: deValido ? d.de : "", meses: d.meses ?? -1 });
  };
  const exemplo = ehCompetencia ? exemploDeslocado(d.meses) : null;
  // Nós elegíveis como mapa: transformação/leitura (efeito/código ficam de fora — validado no back).
  const nosMapa = (nos ?? []).filter((n) =>
    ["fonte", "sql", "guiada", "comparacao", "saida"].includes(n?.tipo),
  );
  const rotuloNo = (n: any) => (n?.rotulo?.trim() ? `${n.rotulo} (${n.id})` : n.id);
  return (
    <div
      data-testid="campos-derivacao"
      className="sbox"
      style={{ padding: 10, display: "flex", flexDirection: "column", gap: 8, background: "var(--soft)" }}
    >
      <div className="rzhint" style={{ display: "block" }}>
        {ehCompetencia ? (
          <>
            Derivado da <b>competência</b>, calculado pela plataforma a cada execução: o 1º dia do
            mês deslocado
            {exemplo ? (
              <>
                {" "}
                (ex.: <code>2026-08</code> → <code>{exemplo}</code>)
              </>
            ) : null}
            . <b>Oculto ao operador</b> — quem dispara passa só a competência.
          </>
        ) : (
          <>
            Derivado de outro parâmetro via um mapa. <b>Oculto ao operador</b> — o Portal o calcula
            a partir do valor escolhido. O mapa pode vir de um <b>arquivo</b>, de uma <b>fonte com
            consulta</b> ou de um <b>nó</b> já filtrado nesta pipeline.
          </>
        )}
      </div>
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end" }}>
        <label style={{ display: "flex", flexDirection: "column", gap: 4, flex: "1 1 160px" }}>
          <span className="fld">{ehCompetencia ? "Competência de origem" : "Derivar de (parâmetro)"}</span>
          <select
            {...baseInput}
            data-testid="der-de"
            value={d.de}
            onChange={(e) => onPatch({ de: e.target.value })}
          >
            <option value="">
              {ehCompetencia ? "Selecione a competência" : "Selecione o parâmetro de origem"}
            </option>
            {origens.map((o) => (
              <option key={o.nome} value={o.nome}>
                {o.nome}
              </option>
            ))}
            {/* Mantém o valor salvo visível mesmo se a origem ainda não existir na lista. */}
            {d.de && !origens.some((o) => o.nome === d.de) && (
              <option value={d.de}>{d.de}</option>
            )}
          </select>
        </label>
        <label style={{ display: "flex", flexDirection: "column", gap: 4, flex: "1 1 160px" }}>
          <span className="fld">Como derivar</span>
          <select
            {...baseInput}
            data-testid="der-origem"
            value={origem}
            onChange={(e) =>
              trocarOrigem(e.target.value as NonNullable<DerivacaoParametro["origem"]>)
            }
          >
            <option value="arquivo">Mapa: arquivo</option>
            <option value="fonte">Mapa: fonte (consulta)</option>
            <option value="no">Mapa: nó da pipeline</option>
            <option value="competencia">Competência deslocada (meses)</option>
          </select>
        </label>
        {ehCompetencia && (
          <label style={{ display: "flex", flexDirection: "column", gap: 4, flex: "0 1 150px" }}>
            <span className="fld">Meses (negativo = antes)</span>
            <input
              {...baseInput}
              type="number"
              step={1}
              data-testid="der-meses"
              value={d.meses ?? ""}
              onChange={(e) =>
                onPatch({ meses: e.target.value === "" ? undefined : Number(e.target.value) })
              }
            />
          </label>
        )}
      </div>
      {/* Os campos de MAPA não se aplicam ao derivado de calendário — e não vão para a definição. */}
      {!ehCompetencia && (
        <>
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end" }}>
        {origem === "arquivo" && (
          <label style={{ display: "flex", flexDirection: "column", gap: 4, flex: "1 1 160px" }}>
            <span className="fld">Mapa (conexão arquivo)</span>
            <select
              {...baseInput}
              data-testid="der-mapa"
              value={d.mapa ?? ""}
              onChange={(e) => onPatch({ mapa: e.target.value })}
            >
              <option value="">Selecione o mapa</option>
              {conexoes.map((c) => (
                <option key={c.id} value={c.nome}>
                  {c.nome}
                </option>
              ))}
              {d.mapa && !conexoes.some((c) => c.nome === d.mapa) && (
                <option value={d.mapa}>{d.mapa}</option>
              )}
            </select>
          </label>
        )}
        {origem === "fonte" && (
          <>
            <label style={{ display: "flex", flexDirection: "column", gap: 4, flex: "1 1 160px" }}>
              <span className="fld">Conexão (fonte)</span>
              <select
                {...baseInput}
                data-testid="der-conexao"
                value={d.conexao_id ?? ""}
                onChange={(e) => onPatch({ conexao_id: e.target.value })}
              >
                <option value="">Selecione a conexão</option>
                {conexoes.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.nome}
                  </option>
                ))}
              </select>
            </label>
            <label style={{ display: "flex", flexDirection: "column", gap: 4, flex: "2 1 100%" }}>
              <span className="fld">Consulta SQL (opcional) — usa :parâmetros da pipeline</span>
              <input
                {...baseInput}
                data-testid="der-sql"
                value={d.sql ?? ""}
                placeholder="ex.: SELECT id, sistema FROM mapa WHERE ativo = 1"
                onChange={(e) => onPatch({ sql: e.target.value })}
              />
            </label>
          </>
        )}
        {origem === "no" && (
          <label style={{ display: "flex", flexDirection: "column", gap: 4, flex: "1 1 160px" }}>
            <span className="fld">Nó da pipeline (saída = mapa)</span>
            <select
              {...baseInput}
              data-testid="der-no"
              value={d.no_id ?? ""}
              onChange={(e) => onPatch({ no_id: e.target.value })}
            >
              <option value="">Selecione o nó</option>
              {nosMapa.map((n) => (
                <option key={n.id} value={n.id}>
                  {rotuloNo(n)}
                </option>
              ))}
              {d.no_id && !nosMapa.some((n) => n.id === d.no_id) && (
                <option value={d.no_id}>{d.no_id}</option>
              )}
            </select>
          </label>
        )}
      </div>
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end" }}>
        <label style={{ display: "flex", flexDirection: "column", gap: 4, flex: "1 1 160px" }}>
          <span className="fld">Coluna de filtro (campo_id)</span>
          <input
            {...baseInput}
            data-testid="der-campo-id"
            value={d.campo_id ?? ""}
            placeholder="ex.: Codigo"
            onChange={(e) => onPatch({ campo_id: e.target.value })}
          />
        </label>
        <label style={{ display: "flex", flexDirection: "column", gap: 4, flex: "1 1 160px" }}>
          <span className="fld">Coluna do valor (campo_valor)</span>
          <input
            {...baseInput}
            data-testid="der-campo-valor"
            value={d.campo_valor ?? ""}
            placeholder="ex.: Valor"
            onChange={(e) => onPatch({ campo_valor: e.target.value })}
          />
        </label>
      </div>
      <div style={{ display: "flex", gap: 16, flexWrap: "wrap", alignItems: "flex-end" }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 6, flex: "1 1 100%" }}>
          <span className="fld">Substituições (bruto → canônico) — ex.: v1 → V</span>
          {(d.substituicoes ?? []).map((s, si) => (
            <div key={si} style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <input
                {...baseInput}
                data-testid="der-sub-de"
                placeholder="bruto"
                value={s.de}
                onChange={(e) =>
                  onPatch({ substituicoes: (d.substituicoes ?? []).map((x, j) => (j === si ? { ...x, de: e.target.value } : x)) })
                }
              />
              <span aria-hidden>→</span>
              <input
                {...baseInput}
                data-testid="der-sub-para"
                placeholder="canônico"
                value={s.para}
                onChange={(e) =>
                  onPatch({ substituicoes: (d.substituicoes ?? []).map((x, j) => (j === si ? { ...x, para: e.target.value } : x)) })
                }
              />
              <button
                type="button"
                className="x"
                title="Remover"
                onClick={() => onPatch({ substituicoes: (d.substituicoes ?? []).filter((_, j) => j !== si) })}
              >
                ×
              </button>
            </div>
          ))}
          <div style={{ display: "flex", gap: 14, alignItems: "center" }}>
            <button
              type="button"
              className="lnk"
              data-testid="der-sub-add"
              onClick={() => onPatch({ substituicoes: [...(d.substituicoes ?? []), { de: "", para: "" }] })}
            >
              + substituição
            </button>
            <label style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 13 }}>
              <input
                type="checkbox"
                data-testid="der-caixa-alta"
                checked={!!d.caixa_alta}
                onChange={(e) => onPatch({ caixa_alta: e.target.checked })}
              />
              sem substituição → CAIXA ALTA
            </label>
          </div>
        </div>
        <label style={{ display: "flex", flexDirection: "column", gap: 4, flex: "0 1 200px" }}>
          <span className="fld">Juntar distintos (separador, opcional)</span>
          <input
            {...baseInput}
            data-testid="der-juntar"
            value={d.juntar ?? ""}
            placeholder="ex.: , (vírgula)"
            onChange={(e) => onPatch({ juntar: e.target.value === "" ? undefined : e.target.value })}
          />
        </label>
      </div>
        </>
      )}
    </div>
  );
}
