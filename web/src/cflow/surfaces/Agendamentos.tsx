import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import * as api from "../../api/client";
import { rota } from "../../prefixo";
import { AvisoCadeia } from "../execucao/AvisoCadeia";
import { ConfirmarAcao, GridLista, Icon, type ItemCadastro, MenuAcoes} from "../ui";
import { SeletorPeriodo } from "../SeletorPeriodo";
import { paresDeData, resolverCorte, resolverJanela, rotuloPeriodo } from "../periodos";
import {
  diaSP,
  fmtDataExtenso,
  fmtDuracao,
  fmtHora,
  fmtMomento,
  fmtMomentoCurto,
  num,
  pillExecucao,
  rotuloDia,
} from "../execucao/formato";

// Agendamentos (E5/E12): lista global (mesmo data grid das outras entidades) → detalhe com abas
// Configuração + Histórico. Recorrência amigável + vigência + alvo (pipeline × página) +
// filtros do operador (lookup multi-seleção = fan-out; período). Agendar uma PÁGINA pré-aquece o
// cache do dia do Portal. Cliente da API; vocabulário do Temporal não aparece.
//
// Momentos, durações e status de execução saem TODOS de `cflow/execucao/formato` — esta tela e as
// telas de Execuções não podem divergir no vocabulário (regra de frontend).

const DIAS = ["Dom", "Seg", "Ter", "Qua", "Qui", "Sex", "Sáb"]; // 0=domingo..6=sábado (BR)
const DIAS_LONGO = ["domingo", "segunda", "terça", "quarta", "quinta", "sexta", "sábado"];

// Recorrência em português corrido. O horário é sempre America/Sao_Paulo (dito uma vez no
// cabeçalho da coluna e no subtítulo da tela, não repetido em cada linha).
function resumoAgenda(a: api.AgendaSpec | null | undefined): string {
  if (!a || !a.tipo) return "—";
  const hora = a.horario || "00:00";
  if (a.tipo === "horaria") {
    const h = a.a_cada_horas ?? 1;
    return h === 1 ? "a cada hora" : `a cada ${h} horas`;
  }
  if (a.tipo === "diaria") return `todos os dias às ${hora}`;
  if (a.tipo === "semanal") {
    const dias = [...(a.dias_semana ?? [])].sort((x, y) => x - y);
    if (dias.length === 0) return `semanal às ${hora} — nenhum dia escolhido`;
    if (dias.length === 7) return `todos os dias às ${hora}`;
    if (dias.length === 5 && dias.join() === "1,2,3,4,5") return `de seg a sex às ${hora}`;
    if (dias.length === 1) return `toda ${DIAS_LONGO[dias[0]]} às ${hora}`;
    const rot = dias.map((d) => DIAS[d]).filter(Boolean);
    return `${rot.slice(0, -1).join(", ")} e ${rot[rot.length - 1]} às ${hora}`;
  }
  if (a.tipo === "mensal") return `todo dia ${a.dia_mes ?? 1} às ${hora}`;
  return "—";
}

// Data (YYYY-MM-DD, já resolvida em São Paulo no backend) → DD/MM/AAAA. Fatiar o texto, nunca
// `new Date`: "2026-07-30" é meia-noite UTC e voltaria 29/07 em São Paulo.
function diaBR(iso: string | null | undefined): string {
  if (!iso) return "—";
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  return m ? `${m[3]}/${m[2]}/${m[1]}` : iso;
}

function hojeSP(): string {
  return new Date().toLocaleDateString("en-CA", { timeZone: "America/Sao_Paulo" });
}

// `<input type="date">` e `type="time"` são desenhados pelo NAVEGADOR, no locale DELE: num Chrome
// em inglês o campo que define quando o agendamento roda aparece como "mm/dd/yyyy" e "09:00 AM",
// dentro de um produto que escreve "às 09:00" na linha de cima. O atributo `lang` não muda isso e
// biblioteca de calendário está fora de cogitação (air-gapped). O que a tela pode garantir é a
// LEITURA em pt-BR ao lado do campo: é ela que diz o valor que vale.
function leituraData(iso: string, vazio: string): string {
  return iso ? diaBR(iso) : vazio;
}

// Estilo da leitura em pt-BR do campo. Ela vem ACIMA do controle, nunca abaixo: o formato do
// navegador ("mm/dd/yyyy", "09:00 AM") não pode ser a primeira coisa lida numa tela que decide
// quando o pipeline roda sozinho.
const LIDO: React.CSSProperties = {
  fontSize: 12,
  color: "var(--ink)",
  fontWeight: 600,
  fontVariantNumeric: "tabular-nums",
  whiteSpace: "nowrap",
};

// Campo com a leitura em cima e o controle embaixo.
//
// A leitura NÃO serve de nome acessível: dentro do `<label>` ela era o nome inteiro do campo
// ("de desde já", "às 09:00 (GMT-3)") — um valor no lugar do nome, que muda a cada digitação e
// nunca diz QUE campo é aquele. Cada controle nativo passa a ter nome estável (`aria-label`, que
// vence o `<label>` que o envolve) e a leitura entra pela DESCRIÇÃO (`aria-describedby`), como já
// é feito no formulário do operador no Portal.
const CAMPO_EMPILHADO: React.CSSProperties = {
  display: "inline-flex",
  flexDirection: "column",
  alignItems: "flex-start",
  gap: 4,
};

// ── Parâmetro de TEMPO no agendamento ────────────────────────────────────────
// Um agendamento roda sozinho, várias vezes: o normal é o valor ser RESOLVIDO a cada disparo, não
// congelado. `as_of`/`competencia` aceitam o token direto (o backend resolve em
// `pipelines.definicao.vincular`); `data` só aceita string literal, então o relativo dela passa
// por `@periodo` ({token, param_inicio}) — o mesmo mecanismo do par início/fim.
const TOKENS_TEMPO: Record<string, { valor: string; rotulo: string }[]> = {
  as_of: [
    { valor: "hoje", rotulo: "o dia do disparo (até o fim do dia)" },
    { valor: "ontem", rotulo: "o dia anterior ao disparo" },
  ],
  competencia: [
    { valor: "atual", rotulo: "o mês do disparo" },
    { valor: "anterior", rotulo: "o mês anterior ao disparo" },
  ],
  data: [
    { valor: "hoje", rotulo: "o dia do disparo" },
    { valor: "ontem", rotulo: "o dia anterior ao disparo" },
  ],
};

// O que "em branco" faz de verdade. Com `default` declarado, o backend usa o DEFAULT
// (`definicao.vincular`) — escrever "em branco = sem filtro" ali era falso: o parâmetro ia com
// valor, só não com o que a tela dizia.
function rotuloEmBranco(p: api.ParametroPipeline): string {
  if (p.default != null && String(p.default) !== "")
    return `em branco = padrão «${String(p.default)}»`;
  if (p.obrigatorio) return "obrigatório";
  return "em branco = sem filtro";
}

// O que o token daria SE o disparo fosse hoje — resolvido pelo mesmo espelho do backend
// (`periodos.ts`). Dizer "resolvido a cada disparo" sem mostrar um exemplo deixa a pessoa
// adivinhando; mostrar só o exemplo faria parecer valor fixo. A frase diz as duas coisas.
function exemploToken(tipo: string, token: string): string {
  try {
    if (tipo === "as_of") {
      const corte = resolverCorte(token);
      return `${fmtDataExtenso(corte.slice(0, 10))}, até ${corte.slice(11, 19)}`;
    }
    const j = resolverJanela(tipo === "competencia" ? (token === "atual" ? "mes_atual" : "mes_anterior") : token);
    if (tipo === "competencia") return fmtDataExtenso(j.data_inicio.slice(0, 7));
    return fmtDataExtenso(j.data_inicio);
  } catch {
    return token;
  }
}

function resumoVigencia(a: api.Agendamento): string | null {
  if (!a.vigencia_inicio && !a.vigencia_fim) return null;
  if (a.vigencia_inicio && a.vigencia_fim)
    return `vigência ${diaBR(a.vigencia_inicio)} a ${diaBR(a.vigencia_fim)}`;
  if (a.vigencia_inicio) return `a partir de ${diaBR(a.vigencia_inicio)}`;
  return `até ${diaBR(a.vigencia_fim)}`;
}

// `ultimo_status` é o resultado do DISPARO (o agendador conseguiu enfileirar?), não o resultado da
// execução: os valores crus são "disparado", "disparado (N)", "erro" e "vazio". A coluna se chama
// "Último disparo" e o texto é traduzido — "erro" pelado, sob um cabeçalho "Último", lia como
// "a última execução falhou", que é outra afirmação.
function rotuloDisparo(
  status: string | null,
): { cls: string; label: string; detalhe: string; dica: string } | null {
  if (!status) return null;
  if (status === "erro")
    return {
      cls: "falha",
      label: "falhou",
      detalhe: "nenhuma execução foi enfileirada",
      dica: "O agendador não conseguiu enfileirar nenhuma execução (veja o histórico).",
    };
  if (status === "vazio")
    return {
      cls: "skip",
      label: "nada a disparar",
      detalhe: "o filtro não devolveu valor",
      dica: "O filtro do agendamento não devolveu nenhum valor — nada foi executado.",
    };
  const ad = /^adiado \((\d+)\/(\d+)\)$/.exec(status);
  if (ad)
    return {
      cls: "skip",
      label: "adiado",
      detalhe: `tentativa ${ad[1]} de ${ad[2]}`,
      dica:
        "A execução anterior ainda estava em andamento com os mesmos parâmetros; " +
        "nova tentativa em 5 minutos.",
    };
  const parcial = /^disparado \((\d+) de (\d+)\)$/.exec(status);
  if (parcial)
    return {
      cls: "ok",
      label: "disparado",
      detalhe: `${parcial[1]} de ${parcial[2]} execuções`,
      dica: "Parte das operações não disparou (já em andamento ou com erro); veja o histórico.",
    };
  const m = /^disparado \((\d+)\)$/.exec(status);
  if (m)
    return {
      cls: "ok",
      label: "disparado",
      detalhe: `${m[1]} execuções`,
      dica: "Uma execução por valor do filtro (fan-out).",
    };
  if (status === "disparado")
    return {
      cls: "ok",
      label: "disparado",
      detalhe: "",
      dica: "Execução enfileirada com sucesso.",
    };
  return { cls: "skip", label: status, detalhe: "", dica: "" };
}

// Resumo curto dos parâmetros de uma run (genérico, sem nomes de domínio): mostra até 3 valores
// escalares, ignorando chaves internas (@...) e limites de data (ISO yyyy-mm-dd, ruidosos).
function resumoParam(p: Record<string, unknown>): string {
  const ehData = (v: unknown) => typeof v === "string" && /^\d{4}-\d{2}-\d{2}$/.test(v);
  const partes: string[] = [];
  for (const [k, v] of Object.entries(p)) {
    if (k.startsWith("@") || v === null || v === undefined || v === "" || ehData(v)) continue;
    if (typeof v === "object") continue;
    partes.push(`${k} ${v}`);
    if (partes.length >= 3) break;
  }
  return partes.join(" · ") || "—";
}

interface ErroTela {
  mensagem: string;
  requestId?: string;
}
function erroDe(e: unknown, fallback: string): ErroTela {
  // `ErroApi` expõe `requestId`; parte do cliente ainda anexa `request_id` (snake). Ler só um dos
  // dois deixava o código de suporte fora da tela justamente nos erros do outro caminho.
  const err = e as { message?: string; requestId?: string; request_id?: string } | null;
  const req = err?.requestId ?? err?.request_id;
  return {
    mensagem: err?.message || fallback,
    requestId: typeof req === "string" ? req : undefined,
  };
}

export default function Agendamentos() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [pipelines, setPipelines] = useState<api.PipelineResumo[]>([]);
  const [paginas, setPaginas] = useState<api.CatalogoItem[]>([]);
  const [todos, setTodos] = useState<api.Agendamento[]>([]);
  const [carregandoLista, setCarregandoLista] = useState(true);
  const [erro, setErro] = useState<ErroTela | null>(null);
  const [msg, setMsg] = useState<{ texto: string; execucaoId?: string | null } | null>(null);

  // Navegação: null = lista; "novo" = criar; <id> = editar. Aba do detalhe.
  const [aberto, setAberto] = useState<string | null>(null);
  const [aba, setAba] = useState<"config" | "historico">("config");
  const [resumo, setResumo] = useState<{ dias: api.ResumoDia[]; total: number } | null>(null);
  const [carregandoResumo, setCarregandoResumo] = useState(false);
  const [erroResumo, setErroResumo] = useState<string | null>(null);
  const [diaAberto, setDiaAberto] = useState<string | null>(null);
  const [execsDia, setExecsDia] = useState<api.ExecAgendamento[] | null>(null);
  const [erroDia, setErroDia] = useState<string | null>(null);
  // Ação que espera confirmação (executar agora / desativar / sair com edição pendente) — modal do
  // design system, nunca `window.confirm`.
  const [pedindo, setPedindo] = useState<
    | { tipo: "executar" | "desativar"; a: api.Agendamento }
    | { tipo: "sair" }
    | null
  >(null);

  // --- formulário ---
  const [editId, setEditId] = useState<string | null>(null);
  const [pid, setPid] = useState("");
  const [detalhe, setDetalhe] = useState<api.PipelineDetalhe | null>(null);
  const [nome, setNome] = useState("");
  const [alvo, setAlvo] = useState<"pipeline" | "pagina">("pipeline");
  const [paginaId, setPaginaId] = useState("");
  const [tipo, setTipo] = useState<api.AgendaSpec["tipo"]>("diaria");
  const [aCadaHoras, setACadaHoras] = useState(6);
  const [horario, setHorario] = useState("06:00");
  const [diasSemana, setDiasSemana] = useState<number[]>([1]);
  const [diaMes, setDiaMes] = useState(1);
  const [vigInicio, setVigInicio] = useState("");
  const [vigFim, setVigFim] = useState("");
  const [periodo, setPeriodo] = useState("mes_anterior");
  const [paramVals, setParamVals] = useState<Record<string, string>>({});
  // Parâmetro de tempo em modo "valor fixo" (o usuário escolheu congelar em vez de resolver a cada
  // disparo). Sem entrada no mapa, o modo é DEDUZIDO do valor guardado — assim abrir um
  // agendamento existente não precisa esperar a definição do pipeline para acertar o controle.
  const [modoFixo, setModoFixo] = useState<Record<string, boolean>>({});
  const [opcoesOp, setOpcoesOp] = useState<{ id: string; rotulo: string }[]>([]);
  // "carregando" ≠ "vazio" ≠ "erro": ler a fonte de opções pode falhar, e devolver lista vazia no
  // `catch` fazia a tela AFIRMAR "sem opções no mapa" — com "em branco = todos" logo abaixo
  // prometendo um disparo por valor que não aconteceria.
  const [faseOpcoes, setFaseOpcoes] = useState<"carregando" | "ok" | "erro">("ok");
  const [recargaOpcoes, setRecargaOpcoes] = useState(0);
  const [operacoesSel, setOperacoesSel] = useState<string[]>([]);
  const [buscaOp, setBuscaOp] = useState("");
  const [salvando, setSalvando] = useState(false);
  // Edição pendente: sair do detalhe descartava a configuração em silêncio (é a tela que decide
  // QUANDO o pipeline roda sozinho). `onChange` sobe de qualquer campo do formulário — React
  // propaga o evento de mudança dos controles de formulário.
  const [sujo, setSujo] = useState(false);

  // Filtros da lista na URL (linkável, sobrevive ao voltar do detalhe).
  const busca = params.get("q") ?? "";
  const estado = params.get("e") ?? ""; // "" | ativo | inativo
  const soFalha = params.get("f") === "1";
  const temFiltro = !!(busca || estado || soFalha);
  const trocarFiltro = useCallback(
    (chave: string, valor: string) => {
      const p = new URLSearchParams(params);
      if (valor) p.set(chave, valor);
      else p.delete(chave);
      setParams(p, { replace: true });
    },
    [params, setParams],
  );
  const limparFiltros = useCallback(
    () => setParams(new URLSearchParams(), { replace: true }),
    [setParams],
  );

  const recarregar = useCallback(async () => {
    setCarregandoLista(true);
    try {
      setTodos(await api.listarAgendamentosTodos());
      setErro(null);
    } catch (e) {
      setErro(erroDe(e, "Falha ao listar agendamentos."));
    } finally {
      setCarregandoLista(false);
    }
  }, []);

  useEffect(() => {
    api.listarPipelines().then(setPipelines).catch(() => setPipelines([]));
    api.catalogo().then(setPaginas).catch(() => setPaginas([]));
    void recarregar();
  }, [recarregar]);

  // Carrega a definição do pipeline selecionado (para os campos do form).
  useEffect(() => {
    if (!pid) {
      setDetalhe(null);
      return;
    }
    let vivo = true;
    api.obterPipeline(pid).then((d) => vivo && setDetalhe(d)).catch(() => vivo && setDetalhe(null));
    return () => {
      vivo = false;
    };
  }, [pid]);

  // Histórico SÓ deste agendamento (escopo por agendamento_id) — carrega ao abrir a aba (lazy).
  useEffect(() => {
    if (aba !== "historico" || !pid || !editId) {
      setResumo(null);
      return;
    }
    let vivo = true;
    setDiaAberto(null);
    setExecsDia(null);
    setErroResumo(null);
    setCarregandoResumo(true);
    api
      .resumoExecucoes(pid, editId)
      .then((r) => {
        if (!vivo) return;
        setResumo(r);
      })
      .catch((e) => {
        if (!vivo) return;
        setResumo(null);
        setErroResumo(erroDe(e, "Falha ao carregar o histórico.").mensagem);
      })
      .finally(() => vivo && setCarregandoResumo(false));
    return () => {
      vivo = false;
    };
  }, [aba, pid, editId]);

  // Drill: abre/fecha um dia → lista as execuções (runs) daquele dia.
  async function abrirDia(dia: string | null) {
    if (!editId || !dia) return;
    if (diaAberto === dia) {
      setDiaAberto(null);
      return;
    }
    setDiaAberto(dia);
    setExecsDia(null);
    setErroDia(null);
    try {
      setExecsDia(await api.listarExecucoesAgendamento(editId, dia));
    } catch (e) {
      setExecsDia([]);
      setErroDia(erroDe(e, "Falha ao carregar as execuções deste dia.").mensagem);
    }
  }

  const paginaSel = useMemo(() => paginas.find((p) => p.id === paginaId), [paginas, paginaId]);
  const execPagina = alvo === "pagina" ? (paginaSel?.execucao ?? null) : null;
  // Página: pid = pipeline da página (carrega os params declarados em `detalhe`).
  useEffect(() => {
    if (alvo === "pagina" && execPagina?.pipeline_id) setPid(execPagina.pipeline_id);
  }, [alvo, execPagina]);

  // Nomes dos params que o operador preenche: na PÁGINA vêm da config (execucao.operador); no
  // PIPELINE são todos os não-derivados.
  const operNomes = useMemo(
    () => (alvo === "pagina" ? new Set((execPagina?.operador ?? []).map((o) => o.param)) : null),
    [alvo, execPagina],
  );
  const declaradosTodos = useMemo<api.ParametroPipeline[]>(() => {
    const ps = (detalhe?.definicao?.parametros ?? []).filter((p) => !p.derivacao);
    return operNomes ? ps.filter((p) => operNomes.has(p.nome)) : ps;
  }, [detalhe, operNomes]);
  // Parâmetro de CREDENCIAL não pode ser agendado: o agendamento guardaria o valor (o backend
  // recusa — `agendamento_com_segredo`). O campo nem aparece; a tela explica em vez de deixar a
  // pessoa preencher e levar um 400.
  const segredos = useMemo(() => declaradosTodos.filter((p) => p.segredo), [declaradosTodos]);
  const declarados = useMemo(() => declaradosTodos.filter((p) => !p.segredo), [declaradosTodos]);
  // Filtro de fan-out (multi-seleção; vazio = todos): 1º param com opções dinâmicas (opcoes_fonte).
  const opOperador = useMemo(() => declarados.find((p) => p.opcoes_fonte) ?? null, [declarados]);
  const opParam = opOperador?.nome;
  const datasTodas = useMemo(() => declarados.filter((p) => p.tipo === "data"), [declarados]);
  const par = useMemo(() => paresDeData(datasTodas.map((p) => p.nome)), [datasTodas]);
  // Parâmetro de tempo que o seletor de Período NÃO cobre: `paresDeData` só resolve um par
  // início/fim (exatamente dois `data`), e `as_of`/`competencia` nunca entram nele. Sem campo
  // próprio, esses parâmetros não apareciam na tela NEM iam no `parametros` do agendamento — a
  // execução automática rodava sem o parâmetro de escopo, em silêncio, e "em branco = tudo" lia a
  // base inteira. É o pior desfecho possível aqui: resultado plausível e errado.
  const tempo = useMemo(
    () =>
      declarados.filter(
        (p) =>
          p.nome !== opParam &&
          (p.tipo === "as_of" ||
            p.tipo === "competencia" ||
            (p.tipo === "data" && (!par || (p.nome !== par.inicio && p.nome !== par.fim)))),
      ),
    [declarados, opParam, par],
  );
  const naoData = useMemo(
    () =>
      declarados.filter(
        (p) =>
          p.tipo !== "data" &&
          p.tipo !== "as_of" &&
          p.tipo !== "competencia" &&
          p.nome !== opParam,
      ),
    [declarados, opParam],
  );
  // Modo atual do parâmetro de tempo: fixo (valor congelado) ou relativo (token).
  const ehFixo = useCallback(
    (p: api.ParametroPipeline): boolean => {
      const explicito = modoFixo[p.nome];
      if (explicito !== undefined) return explicito;
      const v = (paramVals[p.nome] ?? "").trim();
      return v !== "" && !(TOKENS_TEMPO[p.tipo] ?? []).some((t) => t.valor === v);
    },
    [modoFixo, paramVals],
  );

  // Opções do filtro de fan-out (multi-seleção; vazio = todos), lidas da fonte dinâmica do param.
  useEffect(() => {
    let vivo = true;
    async function carregarOpcoes() {
      if (!opOperador?.opcoes_fonte) {
        setOpcoesOp([]);
        setFaseOpcoes("ok");
        return;
      }
      setOpcoesOp([]);
      setFaseOpcoes("carregando");
      try {
        const valores = await api.opcoesDinamicas(opOperador.opcoes_fonte);
        if (!vivo) return;
        setOpcoesOp(
          valores
            .map((o) => ({ id: o.valor, rotulo: o.rotulo }))
            .sort((a, b) => a.rotulo.localeCompare(b.rotulo, undefined, { numeric: true })),
        );
        setFaseOpcoes("ok");
      } catch {
        if (!vivo) return;
        setOpcoesOp([]);
        setFaseOpcoes("erro");
      }
    }
    void carregarOpcoes();
    return () => {
      vivo = false;
    };
  }, [opOperador, recargaOpcoes]);

  function limparForm() {
    setEditId(null);
    setPid("");
    setNome("");
    setAlvo("pipeline");
    setPaginaId("");
    setTipo("diaria");
    setACadaHoras(6);
    setHorario("06:00");
    setDiasSemana([1]);
    setDiaMes(1);
    setVigInicio("");
    setVigFim("");
    setPeriodo("mes_anterior");
    setParamVals({});
    setModoFixo({});
    setOperacoesSel([]);
    setBuscaOp("");
    setSujo(false);
  }

  function abrirNovo() {
    limparForm();
    setResumo(null);
    setErro(null);
    setMsg(null);
    setAba("config");
    setAberto("novo");
  }

  function abrirAgendamento(a: api.Agendamento) {
    setEditId(a.id);
    setPid(a.pipeline_id ?? "");
    setNome(a.nome ?? "");
    setAlvo(a.pagina_id ? "pagina" : "pipeline");
    setPaginaId(a.pagina_id ?? "");
    setTipo(a.agenda?.tipo ?? "diaria");
    setACadaHoras(a.agenda?.a_cada_horas ?? 6);
    setHorario(a.agenda?.horario ?? "06:00");
    setDiasSemana(a.agenda?.dias_semana ?? [1]);
    setDiaMes(a.agenda?.dia_mes ?? 1);
    setVigInicio(a.vigencia_inicio ?? "");
    setVigFim(a.vigencia_fim ?? "");
    const p: Record<string, unknown> = { ...(a.parametros ?? {}) };
    // `@periodo` pode ser UM spec (o par início/fim) ou uma LISTA — o backend aceita as duas formas
    // (`agendador._aplicar_periodos`) e é lista quando há também parâmetro de data solto relativo.
    // Ler só o objeto perdia o período do par nesses casos.
    const bruto = p["@periodo"];
    const specs = (Array.isArray(bruto) ? bruto : bruto ? [bruto] : []) as {
      token?: string;
      param_inicio?: string;
      param_fim?: string;
    }[];
    delete p["@periodo"];
    const vals: Record<string, string> = {};
    let tokenPar = "mes_anterior";
    for (const s of specs) {
      if (s?.param_inicio && s?.param_fim) tokenPar = s.token ?? tokenPar;
      else if (s?.param_inicio && s?.token) vals[s.param_inicio] = s.token;
    }
    setPeriodo(tokenPar);
    const ops = p["@operacoes"];
    setOperacoesSel(Array.isArray(ops) ? ops.map((x) => String(x)) : []);
    setBuscaOp("");
    delete p["@operacoes"];
    for (const [k, v] of Object.entries(p)) vals[k] = String(v ?? "");
    setParamVals(vals);
    setModoFixo({});
    setResumo(null);
    setErro(null);
    setMsg(null);
    setSujo(false);
    setAba("config");
    setAberto(a.id);
  }

  // `forcar` = a pessoa já confirmou descartar a edição pendente.
  function voltar(forcar = false) {
    if (sujo && !forcar) {
      setPedindo({ tipo: "sair" });
      return;
    }
    setSujo(false);
    setAberto(null);
    setErro(null);
    setMsg(null);
    void recarregar();
  }

  function montarAgenda(): api.AgendaSpec {
    if (tipo === "horaria") return { tipo, a_cada_horas: aCadaHoras };
    if (tipo === "semanal") return { tipo, horario, dias_semana: diasSemana };
    if (tipo === "mensal") return { tipo, horario, dia_mes: diaMes };
    return { tipo: "diaria", horario };
  }

  function montarParametros(): Record<string, unknown> {
    const out: Record<string, unknown> = {};
    const periodos: { token: string; param_inicio?: string; param_fim?: string }[] = [];
    for (const p of naoData) {
      const v = (paramVals[p.nome] ?? "").trim();
      if (!v) continue;
      // `Number("abc")` é NaN, e NaN vira `null` no JSON: o valor digitado desaparecia e o backend
      // caía no default (ou no "sem valor"). `salvar` recusa antes de chegar aqui.
      out[p.nome] = p.tipo === "inteiro" ? Number(v) : v;
    }
    for (const p of tempo) {
      const v = (paramVals[p.nome] ?? "").trim();
      if (!v) continue;
      const token = (TOKENS_TEMPO[p.tipo] ?? []).find((t) => t.valor === v);
      // `data` só aceita string literal no bind (`definicao.vincular`): o relativo dela tem de
      // passar por `@periodo`, que o agendador resolve no instante do disparo. `as_of` e
      // `competencia` entendem o token direto.
      if (token && p.tipo === "data") periodos.push({ token: v, param_inicio: p.nome });
      else out[p.nome] = v;
    }
    if (par) periodos.push({ token: periodo, param_inicio: par.inicio, param_fim: par.fim });
    if (periodos.length === 1) out["@periodo"] = periodos[0];
    else if (periodos.length > 1) out["@periodo"] = periodos;
    // Valor da opção vai como TEXTO. `Number(id)` virava NaN em fonte de opções não-numérica
    // (código de obra, sigla) e o JSON levava `null` — o fan-out disparava para a operação "None".
    // O backend já normaliza com `str(x)`, então texto serve para os dois casos.
    if (opOperador) out["@operacoes"] = operacoesSel.map((id) => String(id));
    return out;
  }

  async function salvar() {
    setErro(null);
    setMsg(null);
    if (alvo === "pagina" && !paginaId) {
      setErro({ mensagem: "Escolha a página que deve ser gerada." });
      return;
    }
    if (!pid) {
      setErro({
        mensagem:
          alvo === "pagina"
            ? "A página escolhida não tem execução configurada (sem pipeline) — configure-a na tela de Página."
            : "Escolha o pipeline que deve rodar.",
      });
      return;
    }
    if (tipo === "semanal" && diasSemana.length === 0) {
      setErro({ mensagem: "Escolha ao menos um dia da semana." });
      return;
    }
    if (vigInicio && vigFim && vigFim < vigInicio) {
      setErro({ mensagem: "A vigência termina antes de começar — revise as datas." });
      return;
    }
    // Parâmetro de ESCOPO em branco produziria execução plausível e errada (leria a base inteira):
    // o agendamento roda sozinho, então o que é obrigatório tem de estar decidido aqui. Obrigatório
    // COM `default` é exceção: em branco o backend usa o default (`definicao.vincular`), então
    // cobrá-lo aqui seria travar um agendamento que funcionaria.
    const faltando = [...naoData, ...tempo].filter(
      (p) => p.obrigatorio && p.default == null && !(paramVals[p.nome] ?? "").trim(),
    );
    if (faltando.length) {
      setErro({
        mensagem: `Preencha ${faltando.length === 1 ? "o parâmetro obrigatório" : "os parâmetros obrigatórios"}: ${faltando
          .map((p) => p.rotulo?.trim() || p.nome)
          .join(", ")}. O agendamento roda sozinho — em branco a execução falha ou lê tudo.`,
      });
      return;
    }
    // Inteiro com texto não-numérico virava NaN → `null` no JSON: o agendamento salvava SEM o
    // parâmetro e o disparo usava o default. Erro visível é melhor que valor que se apaga.
    const naoInteiro = naoData.filter(
      (p) => p.tipo === "inteiro" && /\S/.test(paramVals[p.nome] ?? "") && !/^-?\d+$/.test((paramVals[p.nome] ?? "").trim()),
    );
    if (naoInteiro.length) {
      setErro({
        mensagem: `Valor não numérico em ${naoInteiro
          .map((p) => p.rotulo?.trim() || p.nome)
          .join(", ")} — o parâmetro é do tipo inteiro.`,
      });
      return;
    }
    const body: api.AgendamentoBody = {
      agenda: montarAgenda(),
      nome: nome.trim() || null,
      pagina_id: alvo === "pagina" ? paginaId : null,
      parametros: montarParametros(),
      vigencia_inicio: vigInicio || null,
      vigencia_fim: vigFim || null,
    };
    setSalvando(true);
    try {
      if (editId) await api.editarAgendamento(editId, body);
      else await api.criarAgendamento(pid, body);
      setSujo(false);
      voltar(true);
    } catch (e) {
      setErro(erroDe(e, "Falha ao salvar agendamento."));
    } finally {
      setSalvando(false);
    }
  }

  async function alternarAtivo(a: api.Agendamento) {
    setErro(null);
    setMsg(null);
    try {
      if (a.ativo) await api.desativarAgendamento(a.id);
      else await api.ativarAgendamento(a.id);
      setMsg({
        texto: a.ativo
          ? `«${a.nome || "Agendamento"}» desativado — não roda mais no horário.`
          : `«${a.nome || "Agendamento"}» ativado — volta a rodar no horário.`,
      });
      await recarregar();
    } catch (e) {
      setErro(erroDe(e, "Falha ao alterar o agendamento."));
    }
  }

  async function executarAgora(a: api.Agendamento) {
    setErro(null);
    setMsg(null);
    try {
      const r = await api.executarAgoraAgendamento(a.id);
      const n = r.execucoes ?? 1;
      setMsg({
        texto: `Disparado agora: ${n === 1 ? "1 execução" : `${num(n)} execuções`}.`,
        execucaoId: r.execucao_id,
      });
      await recarregar();
    } catch (e) {
      setErro(erroDe(e, "Falha ao disparar o agendamento."));
    }
  }

  const emEdicao = todos.find((a) => a.id === editId);

  const visiveis = useMemo(() => {
    const q = busca.trim().toLowerCase();
    return todos.filter((a) => {
      if (estado === "ativo" && !a.ativo) return false;
      if (estado === "inativo" && a.ativo) return false;
      if (soFalha && a.ultimo_status !== "erro") return false;
      if (!q) return true;
      return (
        (a.nome ?? "").toLowerCase().includes(q) ||
        (a.pipeline_nome ?? "").toLowerCase().includes(q) ||
        (a.pagina_nome ?? "").toLowerCase().includes(q)
      );
    });
  }, [todos, busca, estado, soFalha]);

  const ativos = useMemo(() => todos.filter((a) => a.ativo).length, [todos]);
  const comFalha = useMemo(() => todos.filter((a) => a.ultimo_status === "erro").length, [todos]);

  // ---------------------------------------------------------------- LISTA
  if (aberto === null) {
    const total = todos.length;
    const contagem = temFiltro
      ? `${visiveis.length} de ${total} ${total === 1 ? "agendamento" : "agendamentos"} (filtrados)`
      : total === 1
        ? "1 agendamento"
        : `${total} agendamentos`;
    return (
      <div className="page agd">
        {pedindo?.tipo === "executar" && (
          <ConfirmarAcao
            titulo="Executar agora"
            corpo={
              <>
                Dispara <b>{pedindo.a.nome || "este agendamento"}</b> imediatamente, fora do horário,
                com os parâmetros salvos. Se houver filtro de fan-out em branco, é <b>uma execução
                por valor</b> — e o próximo horário agendado continua valendo.
                <AvisoCadeia pipelineId={pedindo.a.pipeline_id} />
              </>
            }
            rotuloConfirmar="Executar agora"
            onConfirmar={() => {
              void executarAgora(pedindo.a);
              setPedindo(null);
            }}
            onCancelar={() => setPedindo(null)}
          />
        )}
        {pedindo?.tipo === "desativar" && (
          <ConfirmarAcao
            titulo="Desativar agendamento"
            corpo={
              <>
                <b>{pedindo.a.nome || "Este agendamento"}</b> deixa de rodar no horário
                {pedindo.a.pagina_nome ? (
                  <>
                    {" "}
                    e o relatório da página <b>{pedindo.a.pagina_nome}</b> deixa de ficar pronto no
                    Portal
                  </>
                ) : null}
                . Ninguém é avisado — dá para reativar depois.
              </>
            }
            rotuloConfirmar="Desativar"
            perigo
            onConfirmar={() => {
              void alternarAtivo(pedindo.a);
              setPedindo(null);
            }}
            onCancelar={() => setPedindo(null)}
          />
        )}

        <div className="page-head">
          <h1 className="display">Agendamentos</h1>
          <p className="sub">
            Rode um pipeline ou uma página automaticamente — agendar uma página deixa o relatório
            pronto (pré-aquece o cache do dia no Portal). Todos os horários em America/Sao_Paulo
            (GMT-3).
          </p>
          {total > 0 && (
            <p className="exec-resumo-hoje" data-testid="agd-resumo">
              <b>
                {ativos === 0
                  ? "nenhum ativo"
                  : ativos === 1
                    ? "1 ativo"
                    : `${num(ativos)} ativos`}
              </b>
              {total - ativos > 0 && (
                <> · {total - ativos === 1 ? "1 inativo" : `${num(total - ativos)} inativos`}</>
              )}
              {comFalha > 0 && (
                <>
                  {" · "}
                  <button
                    type="button"
                    className="lk exec-resumo-falha"
                    data-testid="agd-resumo-falhas"
                    onClick={() => trocarFiltro("f", soFalha ? "" : "1")}
                  >
                    {comFalha === 1
                      ? "1 falhou no último disparo"
                      : `${num(comFalha)} falharam no último disparo`}
                  </button>
                </>
              )}
            </p>
          )}
        </div>
        <div className="pad">
          {erro && (
            <div className="aviso erro" role="alert">
              {erro.mensagem}
              {erro.requestId && (
                <span style={{ display: "block", fontSize: 11.5 }}>
                  Código para suporte: {erro.requestId}
                </span>
              )}
            </div>
          )}
          {msg && (
            <div className="aviso ok" role="status">
              {msg.texto}
              {msg.execucaoId && (
                <>
                  {" "}
                  <button
                    type="button"
                    className="lk"
                    onClick={() => navigate(`/execucoes/${msg.execucaoId}`)}
                  >
                    ver a execução →
                  </button>
                </>
              )}
            </div>
          )}
          <div className="lc-bar">
            <span data-testid="agd-contagem">{contagem}</span>
            <button className="btn-stamp" data-testid="agd-novo" onClick={abrirNovo}>
              <Icon name="relogio" cls="ic" /> Novo agendamento
            </button>
          </div>

          {(total > 1 || temFiltro) && (
            <div
              data-testid="agd-filtros"
              style={{
                display: "flex",
                alignItems: "flex-end",
                gap: 14,
                flexWrap: "wrap",
                marginBottom: 14,
              }}
            >
              <label style={{ display: "flex", flexDirection: "column", gap: 5 }}>
                <span className="eyebrow">Buscar</span>
                <input
                  className="nm"
                  data-testid="agd-busca"
                  type="search"
                  value={busca}
                  placeholder="nome, pipeline ou página"
                  style={{ width: 240 }}
                  onChange={(e) => trocarFiltro("q", e.target.value)}
                />
              </label>
              <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
                <span className="eyebrow" id="agd-rot-estado">
                  Estado
                </span>
                <div
                  role="group"
                  aria-labelledby="agd-rot-estado"
                  style={{ display: "flex", gap: 6 }}
                >
                  {[
                    { v: "", r: "Todos" },
                    { v: "ativo", r: "Ativos" },
                    { v: "inativo", r: "Inativos" },
                  ].map((c) => (
                    <button
                      key={c.v || "todos"}
                      type="button"
                      className={"chip" + (estado === c.v ? " on" : "")}
                      aria-pressed={estado === c.v}
                      data-testid={`agd-filtro-estado-${c.v || "todos"}`}
                      style={{ height: 37 }}
                      onClick={() => trocarFiltro("e", c.v)}
                    >
                      {c.r}
                    </button>
                  ))}
                </div>
              </div>
              <button
                type="button"
                className={"chip" + (soFalha ? " on" : "")}
                aria-pressed={soFalha}
                data-testid="agd-filtro-falha"
                style={{ height: 37 }}
                onClick={() => trocarFiltro("f", soFalha ? "" : "1")}
              >
                Falha no último disparo
              </button>
              {temFiltro && (
                <button
                  type="button"
                  className="pag-btn"
                  data-testid="agd-limpar-filtros"
                  style={{ height: 37 }}
                  onClick={limparFiltros}
                >
                  Limpar filtros
                </button>
              )}
            </div>
          )}

          {carregandoLista && total === 0 ? (
            <div className="card lc-empty" role="status">
              <div className="big">Carregando agendamentos…</div>
            </div>
          ) : total === 0 ? (
            <div className="card lc-empty">
              <div className="big">Nenhum agendamento ainda</div>
              <div>
                Clique <b>+ Novo agendamento</b> para rodar um pipeline ou uma página no horário.
              </div>
            </div>
          ) : (
            // A rolagem horizontal (notebook 1180px, coluna de ações fora da tela) já vem do
            // `.dgrid-wrap` do próprio GridLista. Havia um segundo invólucro `role="region"` em
            // volta: dois tab stops para a MESMA tabela, e o de dentro ficava sem nome acessível
            // porque `rotulo` não era passado. Uma região, nomeada, é o certo.
            <GridLista
              erro={!!erro}
              rotulo="Agendamentos cadastrados"
              itens={visiveis.map((a): ItemCadastro => {
                const disp = rotuloDisparo(a.ultimo_status);
                const vig = resumoVigencia(a);
                const expirado = !!a.vigencia_fim && a.vigencia_fim < hojeSP();
                // `diaSP` devolve null em timestamp ilegível — `rotuloDia(null!)` derrubava a
                // linha inteira. Sem carimbo legível, a célula DIZ que não há, não inventa data.
                const diaDisparo = diaSP(a.ultimo_disparo_em);
                return {
                  id: a.id,
                  nome: a.nome || "(sem nome)",
                  // Alvo: a PÁGINA já implica o pipeline — repetir os dois quebrava a linha em duas.
                  meta: a.pagina_id
                    ? `página · ${a.pagina_nome ?? "—"}`
                    : `pipeline · ${a.pipeline_nome ?? "—"}`,
                  campos: [
                    {
                      rotulo: "Quando",
                      valor: (
                        <span>
                          {resumoAgenda(a.agenda)}
                          {vig && (
                            <span
                              className="dg-sub"
                              style={{ display: "block", fontSize: 11.5, color: "var(--ink-2)" }}
                            >
                              {vig}
                              {expirado && a.ativo ? " · vigência encerrada" : ""}
                            </span>
                          )}
                        </span>
                      ),
                    },
                    {
                      rotulo: "Próxima (GMT-3)",
                      valor: a.ativo ? (
                        <span title={fmtMomento(a.proxima_em)}>
                          {fmtMomentoCurto(a.proxima_em)}
                        </span>
                      ) : (
                        <span style={{ color: "var(--ink-2)" }}>não roda (inativo)</span>
                      ),
                    },
                    {
                      rotulo: "Último disparo",
                      valor: disp ? (
                        <span>
                          <span className={"statuspill " + disp.cls} title={disp.dica}>
                            <span className="d" />
                            {disp.label}
                          </span>
                          <span
                            style={{
                              display: "block",
                              fontSize: 11.5,
                              marginTop: 3,
                              color: "var(--ink-2)",
                            }}
                          >
                            {diaDisparo ? (
                              <span title={fmtMomento(a.ultimo_disparo_em)}>
                                {rotuloDia(diaDisparo)} {fmtHora(a.ultimo_disparo_em)} (GMT-3)
                              </span>
                            ) : (
                              // Dizer só "sem horário registrado" lia como a TELA ter esquecido a
                              // data. O motivo é do dado: este disparo é anterior à coluna que
                              // guarda o horário — e isso não se conserta para trás.
                              <span title="Este disparo é anterior à coluna que guarda o horário do disparo: a data não foi gravada e não é recuperável. Os próximos disparos aparecem com dia e hora.">
                                sem horário registrado (disparo antigo)
                              </span>
                            )}
                            {disp.detalhe ? " · " : ""}
                            {disp.detalhe}
                            {disp.detalhe && a.ultima_execucao_id ? " · " : ""}
                            {a.ultima_execucao_id && (
                              <button
                                type="button"
                                className="lk"
                                style={{ fontSize: 11.5 }}
                                onClick={() => navigate(`/execucoes/${a.ultima_execucao_id}`)}
                              >
                                ver a execução →
                              </button>
                            )}
                          </span>
                        </span>
                      ) : (
                        <span style={{ color: "var(--ink-2)" }}>ainda não rodou</span>
                      ),
                    },
                  ],
                  status: a.ativo ? "ativo" : "inativo",
                  // `off` = tom neutro (a regra existe no CSS, depois de `.skip`). Inativo NÃO é
                  // âmbar de "atenção": desligado de propósito não é um alerta.
                  scls: a.ativo ? "ok" : "off",
                  testid: `agd-linha-${a.id.slice(0, 8)}`,
                };
              })}
              abrirLabel="Abrir"
              onAbrir={(id) => {
                const a = todos.find((x) => x.id === id);
                if (a) abrirAgendamento(a);
              }}
              acoes={(it) => {
                const a = todos.find((x) => x.id === it.id);
                if (!a) return null;
                return (
                  <>
                    {/* Só a ação de abrir fica fora; disparar de verdade e ligar/desligar o
                        agendamento são raras e consequentes — vão para o menu. */}
                    <button onClick={() => abrirAgendamento(a)}>Abrir</button>
                    <MenuAcoes rotulo={`Mais ações de ${a.nome ?? "agendamento"}`}>
                      <button
                        data-testid={`agd-executar-${a.id.slice(0, 8)}`}
                        onClick={() => setPedindo({ tipo: "executar", a })}
                      >
                        Executar agora
                      </button>
                      <button
                        data-testid={`agd-alternar-${a.id.slice(0, 8)}`}
                        onClick={() =>
                          a.ativo ? setPedindo({ tipo: "desativar", a }) : void alternarAtivo(a)
                        }
                      >
                        {a.ativo ? "Desativar agendamento" : "Ativar agendamento"}
                      </button>
                    </MenuAcoes>
                  </>
                );
              }}
              vazio={
                <div className="card lc-empty">
                  <div className="big">Nenhum agendamento com esses filtros</div>
                  <div>
                    Ajuste a busca ou o estado.{" "}
                    <button
                      type="button"
                      className="lk"
                      data-testid="agd-vazio-limpar"
                      onClick={limparFiltros}
                    >
                      Limpar filtros
                    </button>
                  </div>
                </div>
              }
            />
          )}
        </div>
      </div>
    );
  }

  // ---------------------------------------------------------------- DETALHE
  const agendaAtual = resumoAgenda(montarAgenda());
  return (
    <div className="page agd">
      {pedindo?.tipo === "executar" && (
        <ConfirmarAcao
          titulo="Executar agora"
          corpo={
            <>
              Dispara <b>{pedindo.a.nome || "este agendamento"}</b> imediatamente, fora do horário,
              com os parâmetros <b>salvos</b> (alterações não salvas não entram). Se houver filtro de
              fan-out em branco, é uma execução por valor.
            </>
          }
          rotuloConfirmar="Executar agora"
          onConfirmar={() => {
            void executarAgora(pedindo.a);
            setPedindo(null);
          }}
          onCancelar={() => setPedindo(null)}
        />
      )}
      {pedindo?.tipo === "desativar" && (
        <ConfirmarAcao
          titulo="Desativar agendamento"
          corpo={
            <>
              <b>{pedindo.a.nome || "Este agendamento"}</b> deixa de rodar no horário. Dá para
              reativar depois.
            </>
          }
          rotuloConfirmar="Desativar"
          perigo
          onConfirmar={() => {
            void alternarAtivo(pedindo.a);
            setPedindo(null);
          }}
          onCancelar={() => setPedindo(null)}
        />
      )}
      {pedindo?.tipo === "sair" && (
        <ConfirmarAcao
          titulo={aberto === "novo" ? "Descartar este agendamento?" : "Sair sem salvar?"}
          corpo={
            aberto === "novo" ? (
              <>
                O agendamento <b>ainda não foi criado</b> — nada roda no horário. Sair agora descarta
                o que você preencheu.
              </>
            ) : (
              <>
                Há alterações que ainda <b>não foram salvas</b> — a recorrência, a vigência e os
                parâmetros do agendamento continuam como estavam. Sair agora descarta o que você
                mudou.
              </>
            )
          }
          rotuloConfirmar={aberto === "novo" ? "Descartar" : "Sair e descartar"}
          perigo
          onConfirmar={() => {
            setPedindo(null);
            voltar(true);
          }}
          onCancelar={() => setPedindo(null)}
        />
      )}

      <div className="page-head page-head--compacta">
        <button className="link-back" data-testid="agd-voltar" onClick={() => voltar()}>
          ← Agendamentos
        </button>
        <h1 className="display">
          {aberto === "novo" ? "Novo agendamento" : nome.trim() || "Agendamento"}
        </h1>
        <p className="sub">
          {agendaAtual} · horário de America/Sao_Paulo (GMT-3)
          {emEdicao && emEdicao.ativo && (
            <> · próxima {fmtMomentoCurto(emEdicao.proxima_em)}</>
          )}
          {emEdicao && !emEdicao.ativo && <> · inativo (não roda)</>}
          {sujo && (
            <>
              {" · "}
              <b data-testid="agd-sujo">
                {aberto === "novo" ? "ainda não criado" : "alterações não salvas"}
              </b>
            </>
          )}
        </p>
      </div>

      <div className="pad">
        <div className="cfg-tabs" data-testid="agd-abas" >
          <button
            className={aba === "config" ? "on" : ""}
            aria-pressed={aba === "config"}
            onClick={() => setAba("config")}
          >
            Configuração
          </button>
          <button
            className={aba === "historico" ? "on" : ""}
            aria-pressed={aba === "historico"}
            onClick={() => setAba("historico")}
            disabled={!editId}
            title={editId ? "" : "Disponível após salvar o agendamento"}
          >
            Histórico
          </button>
        </div>

        {erro && (
          <div className="aviso erro" role="alert">
            {erro.mensagem}
            {erro.requestId && (
              <span style={{ display: "block", fontSize: 11.5 }}>
                Código para suporte: {erro.requestId}
              </span>
            )}
          </div>
        )}
        {msg && (
          <div className="aviso ok" role="status">
            {msg.texto}
            {msg.execucaoId && (
              <>
                {" "}
                <button
                  type="button"
                  className="lk"
                  onClick={() => navigate(`/execucoes/${msg.execucaoId}`)}
                >
                  ver a execução →
                </button>
              </>
            )}
          </div>
        )}

        {/* ---------- ABA CONFIGURAÇÃO ---------- */}
        {aba === "config" && (
          // `onChange` na seção: o evento de mudança dos controles de formulário SOBE no React, e
          // marcar "sujo" em cada handler deixaria um campo esquecido de fora — justamente o que
          // perderia trabalho em silêncio ao sair.
          <section
            className="card agd-form"
            onChange={(e) => {
              // Filtrar a lista de valores não é configuração: sem esta exceção, digitar na busca
              // marcava "alterações não salvas" e cobrava confirmação para sair.
              if ((e.target as HTMLElement)?.getAttribute?.("data-testid") === "agd-op-busca") return;
              setSujo(true);
            }}
          >
            <div className="agd-campo">
              <label className="agd-rot" htmlFor="agd-pipeline">
                Pipeline
              </label>
              {editId ? (
                <div className="agd-fixo">{emEdicao?.pipeline_nome ?? detalhe?.nome ?? "—"}</div>
              ) : (
                <select
                  id="agd-pipeline"
                  className="agd-in"
                  data-testid="agd-pipeline"
                  value={pid}
                  disabled={alvo === "pagina"}
                  onChange={(e) => setPid(e.target.value)}
                >
                  <option value="">Selecione um pipeline…</option>
                  {pipelines.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.nome}
                    </option>
                  ))}
                </select>
              )}
              {!editId && alvo === "pagina" && (
                <span className="hint" style={{ fontSize: 12, color: "var(--ink-2)" }}>
                  Definido pela página escolhida abaixo.
                </span>
              )}
              {/* Agendar um pipeline com montante agenda a cadeia inteira: o que roda
                  junto e o que isso faz fora da plataforma aparecem aqui, antes de salvar. */}
              <AvisoCadeia pipelineId={pid || null} />
            </div>

            <div className="agd-campo">
              <label className="agd-rot" htmlFor="agd-nome">
                Nome
              </label>
              <input
                id="agd-nome"
                className="agd-in"
                data-testid="agd-nome"
                value={nome}
                placeholder="ex.: Carga diária 6h"
                onChange={(e) => setNome(e.target.value)}
              />
            </div>

            <div className="agd-campo">
              <span className="agd-rot" id="agd-rot-alvo">
                Rodar
              </span>
              <div className="agd-radio" role="radiogroup" aria-labelledby="agd-rot-alvo">
                <label>
                  <input
                    type="radio"
                    name="alvo"
                    checked={alvo === "pipeline"}
                    onChange={() => setAlvo("pipeline")}
                  />{" "}
                  Só o pipeline
                </label>
                <label>
                  <input
                    type="radio"
                    name="alvo"
                    checked={alvo === "pagina"}
                    onChange={() => setAlvo("pagina")}
                  />{" "}
                  Página (gera relatório)
                </label>
              </div>
            </div>

            {alvo === "pagina" && (
              <div className="agd-campo">
                <label className="agd-rot" htmlFor="agd-pagina">
                  Página
                </label>
                <select
                  id="agd-pagina"
                  className="agd-in"
                  data-testid="agd-pagina"
                  value={paginaId}
                  onChange={(e) => {
                    setPaginaId(e.target.value);
                    setOperacoesSel([]);
                    setBuscaOp("");
                  }}
                >
                  <option value="">Selecione a página…</option>
                  {paginas.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.nome}
                    </option>
                  ))}
                </select>
                {paginaId && !execPagina?.pipeline_id && (
                  <span className="exec-erro" style={{ fontSize: 12 }} role="alert">
                    Esta página não tem execução configurada — configure-a na tela de Página antes de
                    agendar.
                  </span>
                )}
              </div>
            )}

            <div className="agd-campo">
              <span className="agd-rot" id="agd-rot-freq">
                Frequência
              </span>
              {/* `align-items:flex-end` (o CSS da classe centraliza): com a leitura em cima, o
                  campo empilhado ficou mais alto que o seletor ao lado e as duas bases desalinhavam. */}
              <div
                className="agd-freq"
                role="group"
                aria-labelledby="agd-rot-freq"
                style={{ alignItems: "flex-end" }}
              >
                <select
                  data-testid="agd-tipo"
                  aria-label="Tipo de recorrência"
                  value={tipo}
                  onChange={(e) => setTipo(e.target.value as api.AgendaSpec["tipo"])}
                >
                  <option value="horaria">A cada N horas</option>
                  <option value="diaria">Diária</option>
                  <option value="semanal">Semanal</option>
                  <option value="mensal">Mensal</option>
                </select>
                {tipo === "horaria" && (
                  <label>
                    a cada{" "}
                    <input
                      type="number"
                      min={1}
                      max={24}
                      aria-label="A cada quantas horas"
                      value={aCadaHoras}
                      onChange={(e) => setACadaHoras(Math.max(1, Number(e.target.value) || 1))}
                      style={{ width: 64 }}
                    />{" "}
                    h
                  </label>
                )}
                {tipo !== "horaria" && (
                  <label style={CAMPO_EMPILHADO} htmlFor="agd-horario">
                    {/* Leitura em 24h ANTES do campo: ele pode estar mostrando "09:00 AM". */}
                    <span id="agd-horario-lido" className="agd-lido" style={LIDO}>
                      às{" "}
                      <span data-testid="agd-horario-lido">
                        {horario ? `${horario} (GMT-3)` : "sem horário"}
                      </span>
                    </span>
                    <input
                      id="agd-horario"
                      type="time"
                      data-testid="agd-horario"
                      // Nome ESTÁVEL (vence o `<label>` que o envolve, cujo texto é a leitura) +
                      // leitura em pt-BR como DESCRIÇÃO: sem isto o campo se chamava "às 09:00
                      // (GMT-3)" — um valor no lugar do nome, trocado a cada digitação.
                      aria-label="Horário do disparo, em America/Sao_Paulo"
                      aria-describedby="agd-horario-lido"
                      value={horario}
                      onChange={(e) => setHorario(e.target.value)}
                    />
                  </label>
                )}
                {tipo === "mensal" && (
                  <label>
                    dia{" "}
                    <input
                      type="number"
                      min={1}
                      max={31}
                      aria-label="Dia do mês em que roda"
                      value={diaMes}
                      onChange={(e) => setDiaMes(Math.min(31, Math.max(1, Number(e.target.value) || 1)))}
                      style={{ width: 64 }}
                    />
                  </label>
                )}
                {tipo === "semanal" && (
                  <span className="agd-dias">
                    {DIAS.map((d, i) => (
                      <label key={i} className={diasSemana.includes(i) ? "on" : ""}>
                        <input
                          type="checkbox"
                          checked={diasSemana.includes(i)}
                          onChange={(e) =>
                            setDiasSemana((s) =>
                              e.target.checked ? [...s, i].sort() : s.filter((x) => x !== i),
                            )
                          }
                        />
                        {d}
                      </label>
                    ))}
                  </span>
                )}
              </div>
              {/* Lê de volta em português o que os controles montaram — a recorrência é o que mais
                  se erra nesta tela, e "todos os dias às 06:00" confere melhor que 4 campos. */}
              <span
                className="hint"
                data-testid="agd-resumo-agenda"
                style={{ fontSize: 12.5, color: "var(--ink-2)" }}
              >
                Roda {agendaAtual} (GMT-3).
              </span>
            </div>

            <div className="agd-campo">
              <span className="agd-rot" id="agd-rot-vig">
                Vigência
              </span>
              <div
                className="agd-vig"
                role="group"
                aria-labelledby="agd-rot-vig"
                style={{ alignItems: "flex-end" }}
              >
                <label style={CAMPO_EMPILHADO} htmlFor="agd-vig-inicio">
                  {/* Leitura em DD/MM/AAAA ANTES do campo: ele pode estar mostrando "mm/dd/yyyy". */}
                  <span id="agd-vig-inicio-lido" className="agd-lido" style={LIDO}>
                    de{" "}
                    <span data-testid="agd-vig-inicio-lido">
                      {leituraData(vigInicio, "desde já")}
                    </span>
                  </span>
                  <input
                    id="agd-vig-inicio"
                    type="date"
                    data-testid="agd-vig-inicio"
                    aria-label="Início da vigência (dia/mês/ano)"
                    aria-describedby="agd-vig-inicio-lido"
                    value={vigInicio}
                    max={vigFim || undefined}
                    onChange={(e) => setVigInicio(e.target.value)}
                  />
                </label>
                <label style={CAMPO_EMPILHADO} htmlFor="agd-vig-fim">
                  <span id="agd-vig-fim-lido" className="agd-lido" style={LIDO}>
                    até{" "}
                    <span data-testid="agd-vig-fim-lido">{leituraData(vigFim, "sem fim")}</span>
                  </span>
                  <input
                    id="agd-vig-fim"
                    type="date"
                    data-testid="agd-vig-fim"
                    aria-label="Fim da vigência (dia/mês/ano)"
                    aria-describedby="agd-vig-fim-lido"
                    value={vigFim}
                    min={vigInicio || undefined}
                    onChange={(e) => setVigFim(e.target.value)}
                  />
                </label>
                <span className="hint">
                  opcional — em branco = sem limite. A data escolhida aparece por extenso acima de cada campo, em America/Sao_Paulo (o calendário do navegador pode usar outro formato).
                </span>
              </div>
              {vigFim && vigFim < hojeSP() && (
                // `.exec-erro` (não `#a5333b` inline nem `.agd-falha`): as duas outras opções dão
                // 2,42:1 no tema escuro — MEDIDO. `.exec-erro` é a única com override escuro.
                <span className="exec-erro" style={{ fontSize: 12 }} role="alert">
                  A vigência já terminou: o agendamento não vai rodar.
                </span>
              )}
            </div>

            {par && (
              <div className="agd-campo">
                <span className="agd-rot">Período</span>
                <SeletorPeriodo periodo={periodo} onChange={setPeriodo} testid="agd-periodo" />
                <span className="hint" style={{ fontSize: 12, color: "var(--ink-2)" }}>
                  Resolvido a cada disparo — «{rotuloPeriodo(periodo)}» é sempre relativo ao dia da
                  execução.
                </span>
              </div>
            )}

            {/* Parâmetro de TEMPO fora do par início/fim (`as_of`, `competência` e `data` solta).
                Antes não tinha campo nenhum: não aparecia na tela e não ia no `parametros` — o
                disparo automático rodava sem o parâmetro de escopo, em silêncio. */}
            {tempo.length > 0 && (
              <div className="agd-campo">
                <span className="agd-rot">Corte de tempo</span>
                <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                  {tempo.map((p) => {
                    const tokens = TOKENS_TEMPO[p.tipo] ?? [];
                    const val = (paramVals[p.nome] ?? "").trim();
                    const fixo = ehFixo(p);
                    const idSel = `agd-tempo-${p.nome}`;
                    const idFixo = `agd-tempo-fixo-${p.nome}`;
                    const idLido = `agd-tempo-lido-${p.nome}`;
                    const tokenAtivo = !fixo ? tokens.find((t) => t.valor === val) : undefined;
                    const mes = p.tipo === "competencia";
                    return (
                      <div key={p.nome} style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                        <label
                          htmlFor={idSel}
                          style={{ fontSize: 12, color: "var(--ink-2)", fontWeight: 600 }}
                        >
                          {p.rotulo?.trim() || p.nome}
                          {p.obrigatorio && p.default == null && (
                            <span className="exec-param-obg" title="obrigatório">
                              {" *"}
                            </span>
                          )}
                          {/* O `:nome` só entra quando existe rótulo humano — sem ele o cabeçalho
                              já É o nome do parâmetro, e repetir dava "data_registro :data_registro". */}
                          {p.rotulo?.trim() && (
                            <code
                              style={{
                                marginLeft: 6,
                                fontFamily: "ui-monospace,Menlo,monospace",
                                fontSize: 11,
                                color: "var(--ink-3)",
                              }}
                            >
                              :{p.nome}
                            </code>
                          )}
                          <span style={{ marginLeft: 6, fontWeight: 400, color: "var(--ink-2)" }}>
                            {p.tipo === "as_of"
                              ? "· corte de registro (as-of)"
                              : mes
                                ? "· mês de competência"
                                : "· data"}
                          </span>
                        </label>
                        <div style={{ display: "flex", gap: 10, alignItems: "flex-end", flexWrap: "wrap" }}>
                          <select
                            id={idSel}
                            className="agd-in"
                            data-testid={`agd-tempo-${p.nome}`}
                            style={{ width: "auto", minWidth: 280 }}
                            value={fixo ? "@fixo" : val}
                            onChange={(e) => {
                              const v = e.target.value;
                              setModoFixo((m) => ({ ...m, [p.nome]: v === "@fixo" }));
                              setParamVals((m) => ({ ...m, [p.nome]: v === "@fixo" ? "" : v }));
                            }}
                          >
                            <option value="">{rotuloEmBranco(p)}</option>
                            {tokens.map((t) => (
                              <option key={t.valor} value={t.valor}>
                                {t.rotulo}
                              </option>
                            ))}
                            <option value="@fixo">{mes ? "um mês fixo…" : "uma data fixa…"}</option>
                          </select>
                          {fixo && (
                            <label style={CAMPO_EMPILHADO} htmlFor={idFixo}>
                              <span id={idLido} className="agd-lido" style={LIDO}>
                                {val
                                  ? fmtDataExtenso(val)
                                  : mes
                                    ? "nenhum mês escolhido"
                                    : "nenhuma data escolhida"}
                              </span>
                              <input
                                id={idFixo}
                                type={mes ? "month" : "date"}
                                data-testid={`agd-tempo-fixo-${p.nome}`}
                                aria-label={`${p.rotulo?.trim() || p.nome} — valor fixo`}
                                aria-describedby={idLido}
                                value={val}
                                onChange={(e) =>
                                  setParamVals((m) => ({ ...m, [p.nome]: e.target.value }))
                                }
                              />
                            </label>
                          )}
                        </div>
                        <span style={{ fontSize: 11.5, color: "var(--ink-2)" }}>
                          {tokenAtivo
                            ? `Resolvido a cada disparo — se fosse hoje: ${exemploToken(p.tipo, tokenAtivo.valor)}.`
                            : fixo
                              ? `Valor FIXO: todo disparo usa ${val ? fmtDataExtenso(val) : "o mesmo valor"}, hoje e daqui a um ano.`
                              : p.default != null
                                ? `Em branco o disparo usa o padrão do pipeline («${String(p.default)}»), resolvido na hora.`
                                : "Em branco o parâmetro não é enviado — o pipeline decide o que fazer sem ele."}
                        </span>
                        {p.descricao && (
                          <span style={{ fontSize: 11.5, color: "var(--ink-2)" }}>{p.descricao}</span>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            {naoData.length > 0 && (
              <div className="agd-campo">
                <span className="agd-rot">Parâmetros</span>
                <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                  {naoData.map((p) => (
                    <label key={p.nome} style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                      <span style={{ fontSize: 12, color: "var(--ink-2)", fontWeight: 600 }}>
                        {p.rotulo?.trim() || p.nome}
                        {p.obrigatorio && (
                          <span className="exec-param-obg" title="obrigatório">
                            {" *"}
                          </span>
                        )}
                        {p.rotulo?.trim() && (
                          <code
                            style={{
                              marginLeft: 6,
                              fontFamily: "ui-monospace,Menlo,monospace",
                              fontSize: 11,
                              color: "var(--ink-3)",
                            }}
                          >
                            :{p.nome}
                          </code>
                        )}
                      </span>
                      {p.opcoes?.length ? (
                        <select
                          className="agd-in"
                          data-testid={`agd-param-${p.nome}`}
                          value={paramVals[p.nome] ?? ""}
                          onChange={(e) =>
                            setParamVals((v) => ({ ...v, [p.nome]: e.target.value }))
                          }
                        >
                          <option value="">{rotuloEmBranco(p)}</option>
                          {p.opcoes.map((o) => (
                            <option key={o} value={o}>
                              {o}
                            </option>
                          ))}
                        </select>
                      ) : (
                        <input
                          className="agd-in"
                          data-testid={`agd-param-${p.nome}`}
                          inputMode={p.tipo === "inteiro" ? "numeric" : undefined}
                          value={paramVals[p.nome] ?? ""}
                          placeholder={rotuloEmBranco(p)}
                          onChange={(e) =>
                            setParamVals((v) => ({ ...v, [p.nome]: e.target.value }))
                          }
                        />
                      )}
                      {p.descricao && (
                        <span style={{ fontSize: 11.5, color: "var(--ink-2)" }}>{p.descricao}</span>
                      )}
                    </label>
                  ))}
                </div>
              </div>
            )}

            {segredos.length > 0 && (
              <div className="agd-campo">
                <span className="agd-rot">Credencial</span>
                {/* O agendamento roda sozinho: guardar o valor é o que a plataforma se proíbe de
                    fazer (o backend recusa com `agendamento_com_segredo`). */}
                <div className="exec-aviso" role="note">
                  Este pipeline pede {segredos.length === 1 ? "o parâmetro" : "os parâmetros"} de
                  credencial{" "}
                  <b>{segredos.map((p) => p.rotulo?.trim() || p.nome).join(", ")}</b> — que não pode
                  ser agendado, porque o agendamento guardaria o valor. Guarde a credencial no cofre
                  da conexão e o disparo automático funciona.
                </div>
              </div>
            )}

            {opOperador && (
              <div className="agd-campo">
                <label className="agd-rot" htmlFor="agd-op-busca">
                  {opOperador.rotulo?.trim() || "Filtro"}
                </label>
                <div className="agd-multi">
                  <input
                    id="agd-op-busca"
                    className="agd-in"
                    data-testid="agd-op-busca"
                    type="search"
                    placeholder="Buscar… (vazio = todos)"
                    value={buscaOp}
                    onChange={(e) => setBuscaOp(e.target.value)}
                  />
                  <div className="agd-multi-lista">
                    {faseOpcoes === "carregando" ? (
                      <span className="agd-multi-vazio" role="status">
                        lendo os valores da fonte…
                      </span>
                    ) : faseOpcoes === "erro" ? (
                      <span className="exec-erro" role="alert" style={{ padding: 6, fontSize: 12 }}>
                        Não foi possível ler os valores desta fonte. Sem eles «em branco = todos» não
                        tem como se cumprir — o disparo sairia vazio.{" "}
                        <button
                          type="button"
                          className="lk"
                          data-testid="agd-op-tentar"
                          onClick={() => setRecargaOpcoes((n) => n + 1)}
                        >
                          tentar de novo
                        </button>
                      </span>
                    ) : opcoesOp.length === 0 ? (
                      <span className="agd-multi-vazio">a fonte não devolveu nenhum valor</span>
                    ) : (
                      (() => {
                        const filtradas = opcoesOp.filter(
                          (o) =>
                            !buscaOp.trim() ||
                            o.rotulo.toLowerCase().includes(buscaOp.trim().toLowerCase()),
                        );
                        if (filtradas.length === 0)
                          return (
                            <span className="agd-multi-vazio">
                              nenhum valor com «{buscaOp.trim()}» — a seleção acima continua valendo
                            </span>
                          );
                        return filtradas.map((o) => (
                          <label key={o.id} className={operacoesSel.includes(o.id) ? "on" : ""}>
                            <input
                              type="checkbox"
                              checked={operacoesSel.includes(o.id)}
                              onChange={(e) =>
                                setOperacoesSel((s) =>
                                  e.target.checked ? [...s, o.id] : s.filter((x) => x !== o.id),
                                )
                              }
                            />
                            {o.rotulo}
                          </label>
                        ));
                      })()
                    )}
                  </div>
                  <span className="hint">
                    {/* "Vazio = todos os 0 valores (1 execução por valor)" prometia execuções que não
                        existiriam. Sem lista, o disparo sai VAZIO — e é isso que a linha diz. */}
                    {operacoesSel.length > 0
                      ? `${num(operacoesSel.length)} selecionado(s) — 1 execução por valor`
                      : faseOpcoes === "carregando"
                        ? "Lendo os valores para saber quantas execuções «em branco» dispara."
                        : faseOpcoes === "erro"
                          ? "Sem os valores da fonte não é possível dizer quantas execuções sairiam."
                          : opcoesOp.length === 0
                            ? "Nenhum valor na fonte: em branco, o disparo não gera execução nenhuma."
                            : `Vazio = todos os ${num(opcoesOp.length)} valores (1 execução por valor)`}
                    {operacoesSel.length > 0 && (
                      <button type="button" className="lk" onClick={() => setOperacoesSel([])}>
                        limpar
                      </button>
                    )}
                  </span>
                </div>
              </div>
            )}

            <div className="agd-acoes">
              {editId && emEdicao && (
                <>
                  <button
                    className="pag-btn"
                    data-testid="agd-executar-agora"
                    onClick={() => setPedindo({ tipo: "executar", a: emEdicao })}
                  >
                    Executar agora
                  </button>
                  <button
                    className="pag-btn"
                    data-testid="agd-alternar"
                    onClick={() =>
                      emEdicao.ativo
                        ? setPedindo({ tipo: "desativar", a: emEdicao })
                        : void alternarAtivo(emEdicao)
                    }
                  >
                    {emEdicao.ativo ? "Desativar" : "Ativar"}
                  </button>
                </>
              )}
              <button
                className="btn-stamp"
                data-testid="agd-salvar"
                disabled={salvando}
                onClick={salvar}
              >
                <Icon name="stamp" cls="ic" />{" "}
                {salvando ? "Salvando…" : editId ? "Salvar alterações" : "Criar agendamento"}
              </button>
            </div>
          </section>
        )}

        {/* ---------- ABA HISTÓRICO ---------- */}
        {aba === "historico" && (
          <section className="card agd-form">
            <div className="card-h">
              <h3>Histórico de execução</h3>
              <span className="tag">
                {carregandoResumo
                  ? "carregando…"
                  : `${num(resumo?.total ?? 0)} execuç${(resumo?.total ?? 0) === 1 ? "ão" : "ões"} · por dia`}
              </span>
            </div>
            {carregandoResumo ? (
              <p className="vazio" role="status" style={{ padding: "16px 18px" }}>
                Carregando o histórico deste agendamento…
              </p>
            ) : erroResumo ? (
              <div className="aviso erro" role="alert" style={{ margin: "12px 0" }}>
                {erroResumo}
              </div>
            ) : !resumo || resumo.dias.length === 0 ? (
              <p className="vazio" style={{ padding: "16px 18px" }}>
                Nenhuma execução deste agendamento ainda. Use <b>Executar agora</b> na aba
                Configuração para testar sem esperar o horário.
              </p>
            ) : (
              <table className="agd-lista">
                <thead>
                  <tr>
                    <th scope="col">Dia</th>
                    <th scope="col" className="num">Total</th>
                    <th scope="col" className="num">Concluídas</th>
                    <th scope="col" className="num">Falhas</th>
                    <th scope="col" className="num">Agendadas</th>
                    <th scope="col" className="num">Manuais</th>
                    <th scope="col">Última (GMT-3)</th>
                  </tr>
                </thead>
                <tbody>
                  {resumo.dias.map((d) => (
                    <Fragment key={d.dia}>
                      <tr className="agd-dia">
                        <td>
                          {/* Botão de verdade: como `tr onClick` o drill era inalcançável por
                              teclado e o leitor de tela não anunciava aberto/fechado. O marcador
                              fica FORA do botão para não entrar no sublinhado do link. */}
                          <span className="agd-exp" aria-hidden="true">
                            {diaAberto === d.dia ? "▾" : "▸"}
                          </span>{" "}
                          <button
                            type="button"
                            className="lk"
                            aria-expanded={diaAberto === d.dia}
                            data-testid={`agd-dia-${d.dia ?? "sem-dia"}`}
                            onClick={() => void abrirDia(d.dia)}
                          >
                            {diaBR(d.dia)}
                          </button>
                        </td>
                        <td className="num">{num(d.total)}</td>
                        <td className="num">{num(d.concluidas)}</td>
                        <td className="num">
                          {d.falhas > 0 ? (
                            <span className="exec-erro" style={{ fontSize: "inherit" }}>
                              {num(d.falhas)}
                            </span>
                          ) : (
                            0
                          )}
                        </td>
                        <td className="num">{num(d.agendadas)}</td>
                        <td className="num">{num(d.manuais)}</td>
                        <td title={fmtMomento(d.ultima)}>{fmtMomentoCurto(d.ultima)}</td>
                      </tr>
                      {diaAberto === d.dia && (
                        <tr className="agd-dia-exp">
                          <td colSpan={7}>
                            {erroDia ? (
                              <span className="exec-erro" role="alert">
                                {erroDia}
                              </span>
                            ) : execsDia === null ? (
                              <span className="agd-multi-vazio" role="status">
                                carregando as execuções do dia…
                              </span>
                            ) : execsDia.length === 0 ? (
                              <span className="agd-multi-vazio">
                                nenhuma execução registrada neste dia
                              </span>
                            ) : (
                              <table className="agd-runs">
                                <thead>
                                  <tr>
                                    <th scope="col" className="num">#</th>
                                    <th scope="col">Hora (GMT-3)</th>
                                    <th scope="col">Parâmetros</th>
                                    <th scope="col">Status</th>
                                    <th scope="col" className="num">Duração</th>
                                    <th scope="col"></th>
                                  </tr>
                                </thead>
                                <tbody>
                                  {execsDia.map((e, i) => {
                                    const pill = pillExecucao(e.status);
                                    return (
                                      <tr key={e.id}>
                                        <td className="num">{execsDia.length - i}</td>
                                        <td title={fmtMomento(e.iniciada_em)}>
                                          {fmtHora(e.iniciada_em)}
                                        </td>
                                        <td>{resumoParam(e.parametros)}</td>
                                        <td>
                                          <span className={"statuspill " + pill.cls}>
                                            <span className="d" />
                                            {pill.label}
                                          </span>
                                        </td>
                                        <td className="num">
                                          {fmtDuracao(e.iniciada_em, e.concluida_em, "em curso")}
                                        </td>
                                        <td>
                                          <a
                                            className="lk"
                                            href={rota(`/execucoes/${e.id}`)}
                                            onClick={(ev) => {
                                              if (
                                                ev.metaKey ||
                                                ev.ctrlKey ||
                                                ev.shiftKey ||
                                                ev.button !== 0
                                              )
                                                return;
                                              ev.preventDefault();
                                              navigate(`/execucoes/${e.id}`);
                                            }}
                                          >
                                            ver detalhes →
                                          </a>
                                        </td>
                                      </tr>
                                    );
                                  })}
                                </tbody>
                              </table>
                            )}
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        )}
      </div>
    </div>
  );
}
