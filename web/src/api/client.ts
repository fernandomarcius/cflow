// Thin API client. The wire types are written by hand below, next to the calls that use them.
// All calls go to `/api/v1/...`, proxied by Vite (dev) and by nginx (image) to the backend; the URL
// comes from `apiUrl()`, which keeps the mount prefix when the app is served under a sub-path
// (see src/prefixo.ts).

import { apiUrl } from "../prefixo";

const TOKEN_KEY = "cflow_token";
// Token persistido em localStorage → sobrevive a refresh da página (sem relogar).
let token: string | null = typeof localStorage !== "undefined" ? localStorage.getItem(TOKEN_KEY) : null;

export function definirToken(t: string | null) {
  token = t;
  try {
    if (t) localStorage.setItem(TOKEN_KEY, t);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* localStorage indisponível — segue só em memória */
  }
}

// Callback disparado quando a sessão expira (401 numa chamada autenticada) — a UI o registra
// p/ voltar à tela de login automaticamente. Ver `registrarExpiracao`.
let aoExpirar: (() => void) | null = null;
export function registrarExpiracao(cb: (() => void) | null) {
  aoExpirar = cb;
}

async function req(metodo: string, caminho: string, corpo?: unknown): Promise<Response> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const r = await fetch(apiUrl(caminho), {
    method: metodo,
    headers,
    body: corpo === undefined ? undefined : JSON.stringify(corpo),
  });
  // Token expirado/ inválido numa chamada autenticada → limpa a sessão e avisa a UI (volta ao login).
  // O próprio login é exceção (401 ali = credencial errada, tratado pela tela).
  if (r.status === 401 && caminho !== "/auth/login") {
    definirToken(null);
    aoExpirar?.();
  }
  return r;
}

// Fetch autenticado reutilizável pelas superfícies (usa o token do módulo, setado no login).
export function apiFetch(metodo: string, caminho: string, corpo?: unknown): Promise<Response> {
  return req(metodo, caminho, corpo);
}

// O erro carrega `codigo`/`requestId` do envelope: a tela precisa distinguir "informe o código do
// MFA" e "bloqueado por tentativas" de "credencial inválida". Achatar tudo em uma frase fazia o 429
// aparecer como senha errada — e a pessoa insistia, prolongando o bloqueio.
export interface ErroApi extends Error {
  codigo?: string;
  requestId?: string;
  // `detalhes` do envelope, quando a tela precisa de um valor além da frase — ex.: o id da
  // execução em voo que um `409 execucao_em_andamento` aponta.
  detalhes?: { campo: string; mensagem: string }[];
}

function erroApi(mensagem: string, envelope: any): ErroApi {
  return Object.assign(new Error(mensagem), {
    codigo: envelope?.erro?.codigo as string | undefined,
    requestId: envelope?.erro?.request_id as string | undefined,
  });
}

export async function login(email: string, senha: string, codigo?: string): Promise<string> {
  const r = await req("POST", "/auth/login", { email, senha, codigo: codigo || null });
  if (!r.ok) {
    const j = await r.json().catch(() => null);
    if (j?.erro?.codigo === "mfa_requerido")
      throw erroApi("Informe o código do seu autenticador.", j);
    // Só a credencial inválida é genérica (não revela se o e-mail existe). Os outros códigos são
    // sobre o estado da tentativa, não sobre a existência da conta.
    if (j?.erro?.codigo && j.erro.codigo !== "credenciais_invalidas")
      throw erroApi(j.erro.mensagem ?? "Não foi possível entrar.", j);
    throw erroApi("E-mail ou senha inválidos.", j);
  }
  const j = await r.json();
  definirToken(j.token);
  return j.token;
}

export interface UsuarioAtual {
  id: string;
  nome: string;
  email: string;
  perfil: string;
  // Estado do 2º fator: sem ele a tela de conta só sabia OFERECER "configurar", inclusive a quem
  // já tinha configurado.
  mfa_habilitado?: boolean;
}

// Desliga o 2º fator do próprio usuário. Exige a senha atual (uma sessão esquecida aberta não pode
// derrubar a proteção da conta); a ação é auditada no servidor.
export async function mfaDesativar(senhaAtual: string): Promise<void> {
  const r = await req("POST", "/auth/mfa/desativar", { senha_atual: senhaAtual });
  if (!r.ok) {
    const j = await r.json().catch(() => null);
    throw erroApi(j?.erro?.mensagem ?? "Falha ao desligar a verificação em duas etapas.", j);
  }
}

// Usuário logado (nome/email/perfil) — para a barra de conta da sidebar e a página de Conta.
export async function meAtual(): Promise<UsuarioAtual> {
  const r = await req("GET", "/auth/me");
  if (!r.ok) throw new Error("Não foi possível carregar o usuário logado.");
  return r.json();
}

// Troca a PRÓPRIA senha (exige a senha atual). Erros vêm no envelope pt-BR.
export async function trocarSenha(senhaAtual: string, novaSenha: string): Promise<void> {
  const r = await req("POST", "/auth/senha", { senha_atual: senhaAtual, nova_senha: novaSenha });
  if (!r.ok) {
    const j = await r.json().catch(() => null);
    // `requestId` junto: sem ele a tela de Conta não tinha código de suporte para mostrar.
    throw erroApi(j?.erro?.mensagem ?? "Falha ao trocar a senha.", j);
  }
}

// Solicita e-mail de acesso/recuperação (1º acesso ou "esqueci a senha"). Resposta sempre
// genérica (202), exista ou não o e-mail — não revela cadastro. Devolve o status HTTP: com o login
// e-mail é enviado pelo servidor; o status chega à tela.
export async function solicitarRecuperacao(email: string): Promise<number> {
  const r = await req("POST", "/auth/recuperacao", { email });
  return r.status;
}

// Define a senha a partir do token recebido por e-mail (uso único). Erros vêm no envelope pt-BR.
export async function confirmarRecuperacao(token: string, novaSenha: string): Promise<void> {
  const r = await req("POST", "/auth/recuperacao/confirmar", { token, nova_senha: novaSenha });
  if (!r.ok) {
    const j = await r.json().catch(() => null);
    throw new Error(j?.erro?.mensagem ?? "Token inválido ou expirado. Solicite um novo acesso.");
  }
}

// Encerra a sessão no servidor e limpa o token local. Tolera falha de rede (limpa mesmo assim).
export async function logout(): Promise<void> {
  try {
    await req("POST", "/auth/logout");
  } catch {
    /* limpa o token de qualquer forma */
  }
  definirToken(null);
}

export interface Conexao {
  id: string;
  nome: string;
  tipo: string;
  config?: Record<string, unknown>;
  ativo?: boolean;
  // Habilita nós de efeito (escrita_sql/http) a usar esta conexão como destino. Só admin altera.
  permitir_escrita: boolean;
}

// Fonte de ingestão externa (010). Vem de /ingestao/fontes, que já é perfil de leitura
// (especialista/admin/auditor) — o mesmo público que abre o Studio.
//
// O MESMO recurso serve a dois usos, e é por isso que os campos além dos quatro primeiros são
// opcionais: a paleta do nó `fonte lake` só precisa de id/nome/dataset/classe, enquanto a aba
// Bronze da listagem mostra também cadência, marca de sistema, watermark e o desfecho da última
// execução. Tudo isso já vem da MESMA resposta (subselects em `repositorio.listar_fontes`), então
// a aba não faz uma segunda chamada por fonte.
export interface FonteLake {
  id: string;
  nome: string;
  dataset: string;
  classe: string;
  ativo?: boolean;
  // ATENÇÃO: `sistema` aqui NÃO é visibilidade. Em `ingestao_fontes` ele significa "fonte mantida
  // pela plataforma" (semeada do catálogo, para uma re-semeadura não sobrescrever cadastro feito à
  // mão) — a API não filtra a listagem por ele, ao contrário do `sistema` de pipeline/cubo/view.
  // Confundir os dois faria a aba esconder as 54 fontes de quem não é admin.
  sistema?: boolean;
  cadencia?: string;
  // Até onde o incremental já chegou (chave da última fatia ingerida).
  watermark?: string | null;
  watermark_em?: string | null;
  // Desfecho da última execução. `ultimo_status` usa o MESMO vocabulário de pipeline
  // (`executando | concluida | falha`), então `pillExecucao` serve sem tradutor.
  ultima_execucao_em?: string | null;
  ultimo_status?: string | null;
}

// Paleta do editor de grafo: a falha é ENGOLIDA de propósito. Sem as fontes do lake a paleta perde
// uma opção, mas o especialista continua montando o grafo — derrubar o editor por causa disso seria
// pior. Quem precisa do estado real da ingestão usa `listarFontesIngestao` (abaixo).
export async function listarFontesLake(): Promise<FonteLake[]> {
  const r = await req("GET", "/ingestao/fontes");
  if (!r.ok) return [];
  const corpo = await r.json();
  return corpo.fontes ?? [];
}

// Mesma rota, contrato de ERRO oposto: aqui a falha SOBE. A aba Bronze declara estado de ingestão
// ("nunca executado", "falha") — devolver `[]` em cima de um 500 faria a tela afirmar "nenhuma
// fonte de ingestão" para quem tem 54 cadastradas, que é a tela inventando um fato.
export async function listarFontesIngestao(): Promise<FonteLake[]> {
  const r = await req("GET", "/ingestao/fontes");
  if (!r.ok) throw await erroReq(r, "Falha ao carregar as fontes de ingestão");
  const corpo = await r.json();
  return corpo.fontes ?? [];
}

// Resposta do disparo. Com Temporal (padrão) volta `status: "executando"` e o acompanhamento é por
// GET /ingestao/execucoes/{id}; sem orquestrador roda em linha e volta `concluida` com `totais`.
export interface IngestaoDisparada {
  execucao_id: string;
  fonte_id: string;
  dataset: string;
  status: string;
  janela?: { inicio: string; fim: string };
  // Uma fatia por período da janela (a fonte diária entra com ~22 por rodada mensal).
  fatias?: unknown[];
  totais?: Record<string, number>;
}

// Dispara a ingestão incremental (do watermark até hoje). A rota exige especialista ou admin — o
// mesmo perfil que enxerga a aba; a borda reforça com 403.
export async function executarIngestao(fonteId: string): Promise<IngestaoDisparada> {
  const r = await req("POST", `/ingestao/fontes/${fonteId}/executar`);
  if (!r.ok) throw await erroReq(r, "Falha ao executar a ingestão");
  return r.json();
}

// Detalhe de UMA fonte, com o `contrato` completo (a listagem não traz — seria payload gordo para
// 54 linhas). É o que a tela de edição carrega antes de abrir o formulário.
export interface FonteIngestaoDetalhe extends FonteLake {
  contrato: Record<string, unknown>;
  destino_conexao_id?: string | null;
  proxima_em?: string | null;
}

export async function obterFonteIngestao(fonteId: string): Promise<FonteIngestaoDetalhe> {
  const r = await req("GET", `/ingestao/fontes/${fonteId}`);
  if (!r.ok) throw await erroReq(r, "Falha ao carregar a fonte");
  return r.json();
}

// Cadastro/edição/backfill/replay são admin-only na borda (RBAC não muda com a UI nova — só a
// superfície de acesso). Cadastrar/editar não tocam rede (só o contrato no banco); por isso não
// levam guardrail de "digite para confirmar" na tela — só `ConfirmarAcao` simples.
export async function criarFonteIngestao(body: {
  nome: string;
  contrato: Record<string, unknown>;
  destino_conexao_id?: string | null;
  cadencia?: string | null;
}): Promise<{ id: string }> {
  const r = await req("POST", "/ingestao/fontes", body);
  if (!r.ok) throw await erroReq(r, "Falha ao cadastrar a fonte");
  return r.json();
}

// `dataset` NÃO entra no corpo — o backend recusa a troca com 409 `dataset_imutavel`; o formulário
// trava o campo, e esta função nem oferece o parâmetro para não convidar a tentativa.
export async function editarFonteIngestao(
  fonteId: string,
  body: {
    contrato?: Record<string, unknown>;
    cadencia?: string | null;
    destino_conexao_id?: string | null;
    ativo?: boolean;
  },
): Promise<void> {
  const r = await req("PATCH", `/ingestao/fontes/${fonteId}`, body);
  if (!r.ok && r.status !== 204) throw await erroReq(r, "Falha ao editar a fonte");
}

// Valida o contrato SEM cadastrar — o par de "testar conexão". Não lança em 200; quem chama decide
// o que fazer com o erro (o formulário mostra a mensagem, não bloqueia silenciosamente).
export async function validarContratoIngestao(contrato: Record<string, unknown>): Promise<void> {
  const r = await req("POST", "/ingestao/fontes/validar", contrato);
  if (!r.ok) throw await erroReq(r, "Contrato inválido");
}

// O "plano" do backfill/replay: quantas fatias a janela pedida gera, SEM tocar a rede — mesma
// função que a execução de verdade usa por baixo (`plano_dry_run`/`contrato.fatias`), então o
// número é o que vai rodar de fato. É o que o modal de danger-zone mostra antes de liberar o clique.
export interface EstimativaIngestao {
  dataset: string;
  classe: string;
  janela: { inicio: string; fim: string };
  fatias: number;
}

export async function estimarFatiasIngestao(
  fonteId: string,
  inicio: string,
  fim: string,
): Promise<EstimativaIngestao> {
  const q = new URLSearchParams({ inicio, fim }).toString();
  const r = await req("GET", `/ingestao/fontes/${fonteId}/estimativa?${q}`);
  if (!r.ok) throw await erroReq(r, "Falha ao estimar a janela");
  return r.json();
}

// Refaz a janela DA ORIGEM — cada fatia é uma requisição real à fonte pública. Admin-only; a UI
// exige "digite o dataset para confirmar" antes de chamar isto (ver ConfirmarBackfillReplay).
export async function backfillFonteIngestao(
  fonteId: string,
  inicio: string,
  fim: string,
): Promise<IngestaoDisparada> {
  const r = await req("POST", `/ingestao/fontes/${fonteId}/backfill`, { inicio, fim });
  if (!r.ok) throw await erroReq(r, "Falha ao disparar o backfill");
  return r.json();
}

// Reconstrói o bronze só a partir da landing — sem rede. Mesmo guardrail admin-only do backfill,
// mas sem o campo de confirmação por texto (custo é local, não reputação de IP).
export async function replayFonteIngestao(
  fonteId: string,
  inicio: string,
  fim: string,
): Promise<IngestaoDisparada> {
  const r = await req("POST", `/ingestao/fontes/${fonteId}/replay`, { inicio, fim });
  if (!r.ok) throw await erroReq(r, "Falha ao disparar o replay");
  return r.json();
}

export async function listarConexoes(): Promise<Conexao[]> {
  const r = await req("GET", "/conexoes");
  if (!r.ok) return [];
  return r.json();
}

export async function criarConexao(body: {
  nome: string;
  tipo: string;
  config: Record<string, unknown>;
  segredo?: string;
}): Promise<{ id: string }> {
  const r = await req("POST", "/conexoes", body);
  if (!r.ok) throw await erroReq(r, "Falha ao criar conexão");
  return r.json();
}

export async function testarConexao(
  id: string,
): Promise<{ ok?: boolean; causa?: string; [k: string]: unknown }> {
  const r = await req("POST", `/conexoes/${id}/testar`);
  if (!r.ok) throw await erroReq(r, "Falha ao testar conexão");
  return r.json();
}

// Duplica a conexão com a MESMA credencial — copiada dentro do cofre, sem o segredo transitar. A
// cópia nasce sem permissão de escrita. Criador ou admin da origem.
export async function duplicarConexao(id: string, nome: string): Promise<{ id: string }> {
  const r = await req("POST", `/conexoes/${id}/duplicar`, { nome });
  if (!r.ok) throw await erroReq(r, "Falha ao duplicar conexão");
  return r.json();
}

// --- Promoção entre ambientes. Só admin. ---
// Sem ids, o pacote leva tudo o que está publicado; com ids, só o fecho deles.
export async function exportarPacote(pipelineIds: string[] = []): Promise<Record<string, unknown>> {
  const q = pipelineIds.map((id) => `pipeline=${encodeURIComponent(id)}`).join("&");
  const r = await req("GET", `/admin/exportar${q ? `?${q}` : ""}`);
  if (!r.ok) throw await erroReq(r, "Falha ao exportar o pacote");
  return r.json();
}

// `simular` devolve o plano sem gravar; sem ele, executa o MESMO plano numa transação.
export async function importarPacote(
  pacote: Record<string, unknown>,
  opcoes: { atualizar: boolean; simular: boolean },
): Promise<import("../cflow/surfaces/promocao").PlanoPromocao> {
  const r = await req("POST", "/admin/importar", { bundle: pacote, ...opcoes });
  if (!r.ok) {
    throw await erroReq(r, opcoes.simular ? "Falha ao simular a importação" : "Falha ao importar o pacote");
  }
  return r.json();
}

export async function uploadArquivo(
  file: File,
): Promise<{ caminho: string; nome_original: string; tamanho: number }> {
  const fd = new FormData();
  fd.append("file", file);
  // Multipart: não definir Content-Type — o navegador injeta o boundary.
  const headers: Record<string, string> = {};
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const r = await fetch(apiUrl("/arquivos"), { method: "POST", headers, body: fd });
  if (!r.ok) throw await erroReq(r, "Falha ao subir arquivo");
  return r.json();
}

export async function criarPipeline(nome: string): Promise<{ id: string; numero: number }> {
  const r = await req("POST", "/pipelines", { nome });
  if (!r.ok) throw new Error("Falha ao criar pipeline");
  return r.json();
}

// Camada do medalhão de um pipeline. `null` = sem camada.
// É ORTOGONAL a `sistema`: camada diz em que ABA o item aparece; `sistema` diz QUEM o vê.
export type Camada = "bronze" | "silver" | "gold";

// Pipeline group: the SET it belongs to (free-form label). `null` = no group.
// It is a column of its own, not a `Camada` value, because both can coexist.
export type Grupo = string;

export interface PipelineResumo {
  id: string;
  nome: string;
  sistema?: boolean; // item de sistema (só admin vê/edita) — presente nas listagens de todas as entidades
  // Só pipelines têm camada. O tipo é compartilhado com as listagens de cubos/views/funções
  // (`listarCubos`/`listarViews`/`listarFuncoes`), que nunca devolvem este campo — por isso opcional
  // aqui em vez de uma interface separada, que obrigaria a mexer em nove consumidores sem ganho.
  camada?: Camada | null;
  // Idem: só pipelines têm grupo.
  grupo?: Grupo | null;
  // Idem: só pipelines têm `ativo` (bloqueia EXECUÇÃO, não edição/publicação). Opcional pelo
  // mesmo motivo de `camada`/`grupo` — ausente nas listagens de cubos/views/funções.
  ativo?: boolean;
}

// Liga/desliga a flag `sistema` de uma entidade (admin-only; a borda reforça com 403). `colecao` é o
// prefixo da rota REST da entidade.
export async function definirSistema(
  colecao: "pipelines" | "cubos" | "views" | "paginas" | "funcoes",
  id: string,
  sistema: boolean,
): Promise<{ id: string; sistema: boolean }> {
  const r = await req("PATCH", `/${colecao}/${id}/sistema`, { sistema });
  if (!r.ok) throw await erroReq(r, "Falha ao alterar a marca de sistema");
  return r.json();
}

// Frescor de UMA fonte de ingestão (SLO por fonte, no bloco `lake` das métricas).
// `dentro_do_sla` é TRÊS estados, não dois: `true` no prazo, `false` atrasada e **`null` sem SLA
// aplicável** (cadência `manual` sem override no contrato, ou fonte inativa). Tratar `null` como
// `false` pintaria de vermelho as 54 fontes que hoje são `manual` — e alerta que grita sempre é
// alerta que ninguém lê. `atraso_horas` é `null` quando a fonte nunca fechou fatia nenhuma.
export interface FrescorFonte {
  fonte_id: string;
  nome: string;
  dataset: string;
  cadencia: string | null;
  ativa: boolean;
  watermark: string | null;
  watermark_em: string | null; // America/Sao_Paulo, offset explícito
  ultima_execucao_em: string | null;
  ultimo_status: string | null;
  referencia: "watermark" | "execucao" | null; // de onde saiu o atraso
  atraso_horas: number | null;
  sla_horas: number | null;
  dentro_do_sla: boolean | null;
}

// Bloco `lake` de `GET /admin/metricas` (o painel ignorava o data lake por completo).
export interface MetricasLake {
  fontes: { total: number; ativas: number; por_cadencia: Record<string, number> };
  execucoes_24h: Record<string, number>; // por status: executando | concluida | falha
  lotes: { total: number; bronze: number; bytes: number; linhas: number };
  quarentena: {
    total: number;
    ultimas_24h: number;
    fontes_afetadas: number;
    // Fatia que caiu na quarentena e NUNCA pousou no bronze: é "falta dado", não "houve erro".
    fatias_sem_lote_bom: number;
  };
  frescor: { fora_do_sla: number; sem_sla: number; fontes: FrescorFonte[] };
}

// O resto do painel (usuários, conexões, pipelines, auditoria) segue sem tipo: são contagens que
// a tela lê por nome. O índice de assinatura mantém isso compilando enquanto dá tipo ao `lake`.
export interface Metricas extends Record<string, any> {
  banco: string;
  lake?: MetricasLake;
}

export async function metricas(): Promise<Metricas | null> {
  const r = await req("GET", "/admin/metricas");
  return r.ok ? r.json() : null;
}

export async function saudeConexoes(): Promise<any[] | null> {
  const r = await req("GET", "/admin/conexoes/saude");
  return r.ok ? r.json() : null;
}

export async function listarPipelines(): Promise<PipelineResumo[]> {
  const r = await req("GET", "/pipelines");
  if (!r.ok) return [];
  return r.json();
}

// Define (ou remove, com `null`) a camada do medalhão de um pipeline. Admin-only; a borda reforça
// com 403. Sem `colecao` genérica de propósito: só `pipelines` tem camada, e um parâmetro que
// aceitasse "cubos" prometeria uma rota que não existe.
// A rota responde **204 sem corpo** — daí não haver `r.json()` aqui (diferente de `definirSistema`).
export async function definirCamada(id: string, camada: Camada | null): Promise<void> {
  const r = await req("PATCH", `/pipelines/${id}/camada`, { camada });
  if (!r.ok) throw await erroReq(r, "Falha ao alterar a camada");
}

// Ativa/desativa um pipeline// Ativa/desativa um pipeline. Admin-only; 204 sem corpo, como camada/grupo. Desativado bloqueia
// EXECUÇÃO em todo gatilho (agendador cron, reexecutar, disparo externo) — o backend checa no
// choke-point único de `criar_execucao`, não só na rota `/executar`; não impede editar/publicar.
export async function definirAtivo(id: string, ativo: boolean): Promise<void> {
  const r = await req("PATCH", `/pipelines/${id}/ativo`, { ativo });
  if (!r.ok) throw await erroReq(r, "Falha ao alterar o estado do pipeline");
}

// Derivação de um parâmetro a partir de OUTRO parâmetro, via um mapa (conexão `arquivo`).
// Quando presente, o parâmetro é COMPUTADO (o operador não o preenche; o Portal o resolve).
// A derivação mora na definição do parâmetro — num lugar só, não dividida com a página.
export interface DerivacaoParametro {
  de: string; // nome do parâmetro de ORIGEM (ex.: "entidade_id").
  // ORIGEM do mapa (default "arquivo", retrocompatível):
  //  • "arquivo" → lê a conexão `arquivo` inteira (`mapa` = nome da conexão);
  //  • "fonte"   → consulta uma conexão qualquer (`conexao_id` + `sql` opcional), com bind de params;
  //  • "no"      → usa a SAÍDA de um nó do próprio pipeline (`no_id`) como mapa.
  //  • "competencia" → SEM mapa: a competência `de` deslocada de `meses` meses, no dia 1
  //    ("2026-08" com -1 → "2026-07-01"), calculada pela plataforma. Os campos de mapa não vão.
  origem?: "arquivo" | "fonte" | "no" | "competencia";
  meses?: number; // origem "competencia": deslocamento em meses (ex.: -1 = a competência anterior).
  mapa?: string; // origem "arquivo": `nome` da conexão `arquivo` (mapa) consultada.
  conexao_id?: string; // origem "fonte": id da conexão consultada.
  sql?: string; // origem "fonte": SELECT opcional (usa params do pipeline por bind :nome).
  no_id?: string; // origem "no": id do nó do pipeline cuja saída vira o mapa.
  campo_id?: string; // (mapa) coluna com o id de origem (filtro = valor do param `de`).
  campo_valor?: string; // (mapa) coluna com o valor a derivar.
  // Substituições bruto→canônico, CONFIGURÁVEIS (ex.: TOTVSv1/2/3 → TOTVS). Sem mapa hardcoded.
  substituicoes?: { de: string; para: string }[];
  caixa_alta?: boolean; // se true, valor não-substituído vira CAIXA ALTA.
  juntar?: string; // se presente → junta os distintos não-vazios com este separador.
}

// Origem dinâmica das opções de um parâmetro `opcoes`: as opções nascem dos valores distintos de
// uma coluna de uma conexão (tipicamente um `arquivo`/mapa), em vez de uma lista digitada à mão.
// Mutuamente exclusivo com `opcoes` (lista estática).
export interface OpcoesFonte {
  // Origem das opções (default "conexao", retrocompatível):
  //  • "conexao" → conexão via `conexao_id` (+ `sql`/colunas);
  //  • "no"      → saída de um nó do próprio pipeline (`no_id`), resolvida em /pipelines/{id}/opcoes-no.
  origem?: "conexao" | "no";
  conexao_id: string; // origem "conexao" (vazio quando origem "no").
  no_id?: string; // origem "no": id do nó do pipeline cuja saída vira o dropdown.
  coluna?: string; // legado: valor = rótulo (lê a coluna distinta de um arquivo)
  // rótulo≠valor / fonte DB: mostra `coluna_rotulo` (apelido), envia `coluna_valor` (id).
  coluna_valor?: string;
  coluna_rotulo?: string;
  sql?: string; // SELECT p/ DB/gateway (ex.: "SELECT id AS valor, apelido AS rotulo FROM ...")
}

export interface Opcao {
  valor: string; // o que o parâmetro recebe
  rotulo: string; // o que o operador vê
}

export interface ParametroPipeline {
  // "competencia" = mês de competência ("YYYY-MM" | "atual" | "anterior"); o backend o EXPANDE
  // em :{nome}_inicio / :{nome}_fim (janela meio-aberta) para o SQL dos nós referenciar.
  // "as_of" = corte de tempo de SISTEMA (data_registro); injeta :{nome} = timestamp inclusivo
  // (token "hoje"/"2026-05", data ou timestamp). Filtra `DATA_REGISTRO <= :{nome}` — ver o passado.
  nome: string;
  tipo: "inteiro" | "texto" | "data" | "opcoes" | "competencia" | "as_of";
  obrigatorio?: boolean;
  default?: unknown;
  opcoes?: string[];
  derivacao?: DerivacaoParametro; // se presente → parâmetro derivado, oculto ao operador.
  opcoes_fonte?: OpcoesFonte; // se presente → opções dinâmicas (de conexão), exclui `opcoes`.
  // Metadados humanos (opcionais): rótulo/descrição p/ o form do operador, ordem de exibição,
  // e valor usado SÓ no preview do Studio (não afeta a execução real).
  rotulo?: string;
  descricao?: string;
  ordem?: number;
  valor_preview?: unknown;
  // Credencial (senha/token): o valor é usado na execução, mas o backend o REDIGE em
  // `execucoes.parametros`, na trilha de auditoria e nas respostas da API. Nome que aparenta
  // credencial exige esta marca — a definição é recusada sem ela.
  segredo?: boolean;
}

export interface PipelineDetalhe {
  nome: string;
  numero: number;
  ultimo?: number; // maior nº de versão (base do lock otimista); presente ao abrir por número
  // O Studio edita nós, arestas e parâmetros; o resto da definição (ex.: `montante_publica`) passa
  // por ele intacto — ver `cflow/definicao.ts`.
  definicao: {
    nos: any[];
    arestas: any[];
    parametros?: ParametroPipeline[];
    montante_publica?: boolean;
    [chave: string]: unknown;
  };
  ativo: boolean; // false = pipeline não EXECUTA (edição/publicação continuam liberadas)
}

// `numero` opcional → abre aquela versão específica (ver/rodar/publicar uma anterior); sem ele → a última.
export async function obterPipeline(id: string, numero?: number): Promise<PipelineDetalhe> {
  const url = numero != null ? `/pipelines/${id}?numero=${numero}` : `/pipelines/${id}`;
  const r = await req("GET", url);
  if (!r.ok) throw new Error("Falha ao abrir pipeline");
  return r.json();
}

// Parâmetro liberado ao operador numa página publicada: rótulo da página + declaração do pipeline.
export interface ParametroDaPagina {
  nome: string;
  rotulo: string;
  decl: ParametroPipeline;
}

// Declarações dos parâmetros que o operador preenche no Portal, na ordem da config da página.
// Caminho do Portal: `GET /pipelines/{id}` é restrito a especialista/admin — o operador lê os
// parâmetros pela PÁGINA publicada, que ele já tem direito de executar.
export async function parametrosDaPagina(paginaId: string): Promise<ParametroDaPagina[]> {
  const r = await req("GET", `/paginas/${paginaId}/parametros`);
  if (!r.ok) throw await erroReq(r, "Falha ao carregar os parâmetros do relatório");
  return r.json();
}

async function erroDe(r: Response, fallback: string): Promise<string> {
  const j = await r.json().catch(() => null);
  const e = j?.erro;
  if (!e) return fallback;
  const det = (e.detalhes ?? []).map((d: any) => d.mensagem).join(" · ");
  return det ? `${e.mensagem} — ${det}` : e.mensagem;
}

// Um `409 execucao_em_andamento` (mesma pipeline + mesmos parâmetros ainda rodando) aponta a
// execução em voo em `detalhes[0]`. Devolve o id, ou null para qualquer outro erro.
export function execucaoEmVoo(e: unknown): string | null {
  const err = e as ErroApi | undefined;
  if (err?.codigo !== "execucao_em_andamento") return null;
  const d = err.detalhes?.find((x) => x.campo === "execucao_id");
  return d?.mensagem ?? null;
}

// Erro do envelope COM `codigo` e `request_id` preservados. `erroDe` devolve só a frase, e por isso
// nenhuma tela conseguia exibir o código de suporte de uma falha — quem liga para o suporte não
// tinha o que informar, e o log do servidor tem o `request_id` de cada requisição.
async function erroReq(r: Response, fallback: string): Promise<ErroApi> {
  const j = await r.json().catch(() => null);
  const e = j?.erro;
  const det = (e?.detalhes ?? []).map((d: any) => d.mensagem).join(" · ");
  const msg = e ? (det ? `${e.mensagem} — ${det}` : e.mensagem) : fallback;
  return Object.assign(new Error(msg), {
    codigo: e?.codigo as string | undefined,
    detalhes: (e?.detalhes ?? undefined) as { campo: string; mensagem: string }[] | undefined,
    // O header vale como reserva: o envelope é a fonte, mas 502/timeout de proxy não tem corpo.
    requestId: (e?.request_id ?? r.headers.get("X-Request-Id") ?? undefined) as string | undefined,
    request_id: (e?.request_id ?? r.headers.get("X-Request-Id") ?? undefined) as string | undefined,
  });
}

// Persiste só a POSIÇÃO dos nós (layout) na versão atual, sem criar versão nova (mover ≠ editar).
export async function atualizarLayout(id: string, posicoes: Record<string, { x: number; y: number }>) {
  const r = await req("PATCH", `/pipelines/${id}/layout`, { posicoes });
  if (!r.ok && r.status !== 204) throw await erroReq(r, "Falha ao salvar layout");
}

export async function salvarRascunho(id: string, definicao: unknown, base_numero: number) {
  const r = await req("PUT", `/pipelines/${id}/rascunho`, { definicao, base_numero });
  if (r.status === 409) throw new Error("Conflito de versão — recarregue.");
  if (!r.ok) throw await erroReq(r, "Falha ao salvar rascunho");
  return r.json() as Promise<{ numero: number }>;
}

export async function publicar(id: string, base_numero: number, nota: string) {
  const r = await req("POST", `/pipelines/${id}/publicar`, { base_numero, nota });
  if (!r.ok) throw await erroReq(r, "Falha ao publicar");
  return r.json() as Promise<{ numero: number }>;
}

// Resumo de versão — comum a todas as entidades versionadas do Studio (pipeline/cubo/view/página/
// funções). `exec_portal` só vem no pipeline (execuções do operador).
export interface VersaoResumo {
  numero: number;
  status: string; // rascunho | publicada | arquivada
  nota_mudanca: string | null;
  criado_em: string | null;
  ativa: boolean; // é a versão publicada ativa
  exec_portal?: number; // execuções pelo Portal (só pipeline)
}
export type VersaoPipeline = VersaoResumo;

async function _listarVersoes(base: string, id: string): Promise<VersaoResumo[]> {
  const r = await req("GET", `${base}/${id}/versoes`);
  if (!r.ok) throw await erroReq(r, "Falha ao listar versões");
  return r.json();
}
export const listarVersoes = (id: string) => _listarVersoes("/pipelines", id);
export const listarVersoesCubo = (id: string) => _listarVersoes("/cubos", id);
export const listarVersoesView = (id: string) => _listarVersoes("/views", id);
export const listarVersoesPagina = (id: string) => _listarVersoes("/paginas", id);
export const listarVersoesFuncao = (id: string) => _listarVersoes("/funcoes", id);

// Reverte: cria uma NOVA versão publicada copiando a definição da versão `para_numero` (auditado).
export async function reverterPipeline(id: string, para_numero: number) {
  const r = await req("POST", `/pipelines/${id}/reverter`, { para_numero });
  if (!r.ok) throw await erroReq(r, "Falha ao reverter versão");
  return r.json() as Promise<{ numero: number }>;
}

// CRUD — exclusão de recursos (DELETE → 204). Auditado/autorizado no backend.
async function excluirRecurso(base: string, id: string): Promise<void> {
  const r = await req("DELETE", `${base}/${id}`);
  if (!r.ok && r.status !== 204) throw await erroReq(r, "Falha ao excluir");
}
// Amostra do snapshot de um nó de uma execução + log (status/erro/métricas) — drill em Execuções.
// `contagem` é o total de linhas do snapshot (não da amostra) → a UI diz "50 de 120 linha(s)".
export interface AmostraNoExecucao extends ExecNo {
  colunas: string[];
  amostra: Record<string, unknown>[];
  contagem: number | null;
}

// `limite` (1..200) = quantas linhas da amostra vêm; o backend faz clamp.
export async function previewNoExecucao(
  execId: string,
  noId: string,
  limite = 50,
): Promise<AmostraNoExecucao> {
  const r = await req(
    "GET",
    `/execucoes/${execId}/nos/${encodeURIComponent(noId)}/preview?limite=${limite}`,
  );
  if (!r.ok) throw await erroReq(r, "Falha ao pré-visualizar o nó da execução");
  return r.json();
}

export const excluirPipeline = (id: string) => excluirRecurso("/pipelines", id);
export const excluirCubo = (id: string) => excluirRecurso("/cubos", id);
export const excluirView = (id: string) => excluirRecurso("/views", id);
export const excluirPagina = (id: string) => excluirRecurso("/paginas", id);
export const excluirConexao = (id: string) => excluirRecurso("/conexoes", id);

// CRUD — renomear recurso (PATCH /<base>/{id} {nome} → 200 {id, nome}). Atualiza só o nome;
// autorizado (administrar / criador-ou-admin) e auditado (acao=editar) no backend.
async function renomearRecurso(base: string, id: string, nome: string): Promise<{ id: string; nome: string }> {
  const r = await req("PATCH", `${base}/${id}`, { nome });
  if (!r.ok) throw await erroReq(r, "Falha ao renomear");
  return r.json();
}
export const renomearPipeline = (id: string, nome: string) => renomearRecurso("/pipelines", id, nome);
export const renomearCubo = (id: string, nome: string) => renomearRecurso("/cubos", id, nome);
export const renomearView = (id: string, nome: string) => renomearRecurso("/views", id, nome);
export const renomearPagina = (id: string, nome: string) => renomearRecurso("/paginas", id, nome);
export const renomearConexao = (id: string, nome: string) => renomearRecurso("/conexoes", id, nome);

// Conexão — editar config (e, opcionalmente, rotacionar a credencial). PUT → 204.
// `credencial` ausente = mantém a atual; informada = grava/rotaciona no cofre. Segredo nunca
// volta nem entra na config. Auditado (sem segredo no diff) no backend.
export async function editarConexao(
  id: string,
  body: { config: Record<string, unknown>; credencial?: string },
): Promise<void> {
  const r = await req("PUT", `/conexoes/${id}`, body);
  if (!r.ok && r.status !== 204) throw await erroReq(r, "Falha ao editar conexão");
}

// Liga/desliga `permitir_escrita` — habilita nós de efeito (escrita_sql/http) a usar esta conexão
// como destino. Admin-only (borda no backend, `SoAdmin` → 403 genérico); auditado (diff da flag).
export async function definirEscritaConexao(
  id: string,
  permitir: boolean,
): Promise<{ id: string; permitir_escrita: boolean }> {
  const r = await req("PATCH", `/conexoes/${id}/escrita`, { permitir_escrita: permitir });
  if (!r.ok) throw await erroReq(r, "Falha ao alterar permissão de escrita");
  return r.json();
}

// Dry-run de um nó de efeito (escrita_sql/http/email): o preview NUNCA executa o efeito — só
// descreve o que aconteceria (SQL gerado, payloads, mensagem montada, destino, contagem). Presente
// só quando o nó pré-visualizado é de efeito. `aviso` deixa explícito que nada saiu.
export interface EfeitoPreview {
  tipo: "escrita_sql" | "http" | "email";
  modo: string;
  destino: string;
  sql_gerado?: string; // escrita_sql: o SQL que seria rodado no destino.
  payloads?: (string | null)[]; // http: corpos das requisições montadas sobre a amostra (≤20).
  metodo?: string; // http: método HTTP.
  contagem_alvo?: number; // http/email: nº de requisições/e-mails que sairiam.
  linhas_entrada?: number; // escrita_sql: nº de linhas da amostra de entrada.
  // email (009): a mensagem montada — a MESMA que a execução enviaria.
  assunto?: string;
  corpo?: string;
  corpo_tipo?: "html" | "texto";
  servidor?: string; // host da conexão SMTP (metadado; o dry-run não conecta).
  remetente?: string;
  anexos?: { nome: string; bytes: number; tipo_mime?: string }[];
  nao_resolvidos?: string[]; // placeholders que sairiam em branco.
  aviso: string;
}

export interface PreviewResultado {
  colunas: string[];
  tipos: string[];
  linhas: Record<string, unknown>[];
  contagem: number;
  efeito?: EfeitoPreview; // presente só no dry-run de um nó de efeito.
}

export async function colunasDisponiveis(id: string, noId: string, definicao: unknown): Promise<Record<string, string[]>> {
  const r = await req("POST", `/pipelines/${id}/nos/${noId}/colunas-disponiveis`, { definicao });
  if (!r.ok) return {};
  return r.json();
}

// --- Modelo (F4): cubo + consulta ---
export async function listarCubos(): Promise<PipelineResumo[]> {
  const r = await req("GET", "/cubos");
  return r.ok ? r.json() : [];
}

// Estado de um cubo para a LISTA (`GET /cubos?resumo=true`, uma consulta no servidor): contagens e
// fonte da ÚLTIMA versão, a publicada ativa, `ultimo` e quantas views visíveis o leem. Antes a
// lista pedia `GET /cubos/{id}` + `/versoes` por cubo e `GET /views/{id}` por view.
export interface ResumoCubo {
  dimensoes: number;
  medidas: number;
  pipeline_id: string | null;
  no_id: string | null;
  ultimo: number;
  publicada: PublicadaResumo | null;
  views: number;
}
export interface CuboComResumo extends PipelineResumo {
  resumo?: ResumoCubo; // ausente só se a API ainda não tiver o resumo
}
export async function listarCubosComResumo(): Promise<CuboComResumo[]> {
  const r = await req("GET", "/cubos?resumo=true");
  return r.ok ? r.json() : [];
}
export async function criarCubo(nome: string): Promise<{ id: string }> {
  const r = await req("POST", "/cubos", { nome });
  if (!r.ok) throw new Error("Falha ao criar cubo");
  return r.json();
}
export async function obterCubo(id: string, numero?: number): Promise<{ nome: string; numero: number; ultimo?: number; definicao: any }> {
  const r = await req("GET", numero != null ? `/cubos/${id}?numero=${numero}` : `/cubos/${id}`);
  if (!r.ok) throw new Error("Falha ao abrir cubo");
  return r.json();
}
export async function salvarCubo(id: string, definicao: unknown, base_numero: number): Promise<{ numero: number }> {
  const r = await req("PUT", `/cubos/${id}/rascunho`, { definicao, base_numero });
  if (!r.ok) throw await erroReq(r, "Falha ao salvar cubo");
  return r.json();
}
export async function publicarCubo(id: string, base_numero: number): Promise<{ numero: number }> {
  const r = await req("POST", `/cubos/${id}/publicar`, { base_numero, nota: "publicado pelo Studio" });
  if (!r.ok) throw await erroReq(r, "Falha ao publicar cubo");
  return r.json();
}
export async function consultarCubo(id: string, dimensoes: string[], medidas: string[]): Promise<any> {
  const r = await req("POST", `/cubos/${id}/consultar`, { dimensoes, medidas });
  if (!r.ok) throw await erroReq(r, "Falha ao consultar cubo");
  return r.json();
}

// --- Biblioteca de funções (F-Funções): MACROs DuckDB reutilizáveis, JSON versionado ---
// Mesma família CRUD versionado dos cubos (criar→{id}, rascunho PUT, publicar POST). O `numero`
// devolvido por obter/salvar/publicar é o `base_numero` do próximo salvamento (lock otimista).
export interface FuncaoDef {
  nome: string;
  args: string[];
  corpo: string;
  retorno: "scalar" | "struct";
  descricao?: string;
}
export interface BibliotecaDefinicao {
  nome?: string;
  descricao?: string;
  funcoes: FuncaoDef[];
}

export async function listarFuncoes(): Promise<PipelineResumo[]> {
  const r = await req("GET", "/funcoes");
  return r.ok ? r.json() : [];
}
export async function criarFuncao(nome: string, descricao?: string): Promise<{ id: string }> {
  const r = await req("POST", "/funcoes", { nome, descricao: descricao ?? null });
  if (!r.ok) throw await erroReq(r, "Falha ao criar biblioteca de funções");
  return r.json();
}
export async function obterFuncao(
  id: string,
  numero?: number,
): Promise<{ nome: string; numero: number; ultimo?: number; definicao: any }> {
  const r = await req("GET", numero != null ? `/funcoes/${id}?numero=${numero}` : `/funcoes/${id}`);
  if (!r.ok) throw await erroReq(r, "Falha ao abrir biblioteca de funções");
  return r.json();
}
export async function salvarFuncao(
  id: string,
  definicao: unknown,
  base_numero: number,
): Promise<{ numero: number }> {
  const r = await req("PUT", `/funcoes/${id}/rascunho`, { definicao, base_numero });
  if (r.status === 409) throw new Error("Conflito de versão — recarregue.");
  if (!r.ok) throw await erroReq(r, "Falha ao salvar rascunho da biblioteca");
  return r.json();
}
export async function publicarFuncao(
  id: string,
  base_numero: number,
  nota?: string,
): Promise<{ numero: number }> {
  const r = await req("POST", `/funcoes/${id}/publicar`, {
    base_numero,
    nota: nota ?? "publicado pelo Studio",
  });
  if (!r.ok) throw await erroReq(r, "Falha ao publicar biblioteca de funções");
  return r.json();
}
export const excluirFuncao = (id: string) => excluirRecurso("/funcoes", id);
export const renomearFuncao = (id: string, nome: string) => renomearRecurso("/funcoes", id, nome);

// Avalia uma chamada de macro contra valores de amostra num DuckDB efêmero (auxílio de edição).
// `definicao` (opcional) sobrepõe a versão salva — usa o rascunho EM EDIÇÃO sem precisar salvar.
export async function avaliarFuncao(
  id: string,
  chamada: string,
  definicao?: unknown,
  argumentos?: Record<string, unknown>,
): Promise<{ resultado: string | null }> {
  const r = await req("POST", `/funcoes/${id}/avaliar`, {
    chamada,
    definicao: definicao ?? null,
    argumentos: argumentos ?? null,
  });
  if (!r.ok) throw await erroReq(r, "Falha ao avaliar a função");
  return r.json();
}

// view + página (publicar pelo front)
async function publicarRecurso(base: string, nome: string, definicao: unknown): Promise<string> {
  const cr = await req("POST", base, { nome });
  if (!cr.ok) throw new Error(await erroDe(cr, "Falha ao criar"));
  const id = (await cr.json()).id;
  const sr = await req("PUT", `${base}/${id}/rascunho`, { definicao, base_numero: 0 });
  if (!sr.ok) throw new Error(await erroDe(sr, "Falha ao salvar"));
  const pr = await req("POST", `${base}/${id}/publicar`, { base_numero: 1, nota: "publicado pelo front" });
  if (!pr.ok) throw new Error(await erroDe(pr, "Falha ao publicar"));
  return id;
}
export const publicarView = (nome: string, definicao: unknown) => publicarRecurso("/views", nome, definicao);
export const publicarPagina = (nome: string, definicao: unknown) => publicarRecurso("/paginas", nome, definicao);
export async function listarViews(): Promise<PipelineResumo[]> {
  const r = await req("GET", "/views");
  return r.ok ? r.json() : [];
}

// Versão publicada ativa, como o resumo das listas a devolve.
export interface PublicadaResumo {
  numero: number;
  criado_em: string | null;
}

// Estado de uma view para a LISTA (`GET /views?resumo=true`, uma consulta no servidor): contagens e
// cubo da ÚLTIMA versão, a publicada ativa, `ultimo` (maior número — há rascunho à frente quando
// passa a publicada) e em quantas páginas publicadas ela aparece. `paginas_nao_verificadas` =
// páginas de sistema cuja definição este perfil não lê (não entram em `paginas`). Antes a lista
// pedia `GET /views/{id}` + `/versoes` por view e `GET /paginas/{id}` por página.
export interface ResumoView {
  cubo_id: string | null;
  linhas: number;
  colunas: number;
  valores: number;
  filtros_liberados: number;
  ultimo: number;
  publicada: PublicadaResumo | null;
  paginas: number;
  paginas_nao_verificadas: number;
}
export interface ViewComResumo extends PipelineResumo {
  // Ausente só se a API ainda não tiver o resumo (deploy do front antes do da API).
  resumo?: ResumoView;
}
// Mesma régua de `listarViews`: falha HTTP vira lista vazia.
export async function listarViewsComResumo(): Promise<ViewComResumo[]> {
  const r = await req("GET", "/views?resumo=true");
  return r.ok ? r.json() : [];
}

// --- Portal (F5): catálogo + página + relatório ---

// Configuração de execução de uma PÁGINA parametrizada: o pipeline que a alimenta e QUAIS dos
// parâmetros declarados no pipeline o operador preenche (cada um com um rótulo amigável).
//
// Modelo SIMPLES: a página NÃO redefine tipo, opções ou derivação dos parâmetros — tudo isso vem da
// definição do parâmetro no próprio pipeline (`definicao.parametros[]`: `tipo`, `opcoes`,
// `opcoes_fonte`, `derivacao`). A aba Execução só escolhe quais params o operador vê e o rótulo.
// Parâmetros derivados (com `derivacao` no pipeline) são resolvidos automaticamente — não entram aqui.
export interface ExecParam {
  param: string; // nome do parâmetro declarado no pipeline.
  rotulo?: string; // rótulo amigável exibido ao operador (cai p/ o `param`).
}

export interface ExecConfig {
  pipeline_id: string;
  // OPCIONAL de propósito: a API devolve `execucao` sem `operador` quando o pipeline da página não
  // tem parâmetro para o operador preencher. Declarar como obrigatório fez o `!` de quem consumia
  // parecer seguro e derrubou o Portal inteiro com "Cannot read properties of undefined".
  operador?: ExecParam[] | null;
}

export interface CatalogoItem {
  id: string;
  nome: string;
  sistema?: boolean; // página de sistema (só admin abre no Studio; Portal segue consumindo)
  execucao?: ExecConfig | null;
}

export async function catalogo(): Promise<CatalogoItem[]> {
  const r = await req("GET", "/catalogo");
  // Erro HTTP era engolido como lista vazia: o Portal dizia "nada publicado" quando a API tinha
  // falhado — o operador concluía que o relatório dele havia sumido.
  if (!r.ok) throw await erroReq(r, "Falha ao carregar o catálogo de relatórios");
  return r.json();
}

// Poll de uma execução até sair de "executando" (concluída/falha) ou estourar o limite.
// Mesma cadência do fluxo do Check (1,5s, ~40 tentativas). Retorna o último estado lido.
export async function aguardarExecucao(
  execucaoId: string,
  tentativas = 40,
  intervaloMs = 1500,
): Promise<any> {
  let estado = await consultarExecucao(execucaoId);
  let i = 0;
  while (estado?.status === "executando" && i < tentativas) {
    await new Promise((r) => setTimeout(r, intervaloMs));
    estado = await consultarExecucao(execucaoId);
    i += 1;
  }
  return estado;
}

// Consulta os blocos de uma PÁGINA publicada (POST /paginas/{id}/consultar {filtros}).
// Usada pelo Portal (catálogo read-only) e pela superfície Check (painel de conferência
// com comparação detalhada).
// `filtros` ausente/null → visão completa da página.
// `execucaoId` (opcional) → consulta o snapshot DAQUELA execução (a que o operador disparou),
// não a última qualquer (evita "último run vence" com execuções concorrentes).
export async function consultarPagina(
  paginaId: string,
  filtros: any[] | null = null,
  execucaoId?: string | null,
): Promise<any> {
  const r = await req("POST", `/paginas/${paginaId}/consultar`, { filtros: filtros ?? null, execucao_id: execucaoId ?? null });
  if (!r.ok) throw await erroReq(r, "Falha ao consultar página");
  return r.json();
}
export async function gerarRelatorio(id: string, filtros?: any[], execucaoId?: string | null): Promise<any> {
  const r = await req("POST", `/paginas/${id}/relatorio`, { filtros: filtros ?? null, execucao_id: execucaoId ?? null });
  if (!r.ok) throw await erroReq(r, "Falha ao gerar relatório");
  return r.json();
}

// --- Relatórios carimbados (E10): listar / abrir / exportar Excel ---

export interface RelatorioResumo {
  id: string;
  pagina: string;
  versao: number;
  criado_em: string | null; // ISO em America/Sao_Paulo
  criado_por: string | null;
}

export async function listarRelatorios(): Promise<RelatorioResumo[]> {
  const r = await req("GET", "/relatorios");
  // Mesmo defeito que `catalogo()` tinha: erro de HTTP virava lista vazia, e a tela dizia "nenhum
  // relatório" quando na verdade a chamada falhou.
  if (!r.ok) throw await erroReq(r, "Falha ao carregar os relatórios");
  return r.json();
}

export async function obterRelatorio(id: string): Promise<any> {
  const r = await req("GET", `/relatorios/${id}`);
  if (!r.ok) throw await erroReq(r, "Falha ao abrir relatório");
  return r.json();
}

// Download autenticado de um .xlsx (Bearer no header → Blob; a superfície dispara o download).
async function baixarXlsx(metodo: string, caminho: string, corpo?: unknown): Promise<Blob> {
  const headers: Record<string, string> = {};
  if (corpo !== undefined) headers["Content-Type"] = "application/json";
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const r = await fetch(apiUrl(caminho), {
    method: metodo,
    headers,
    body: corpo === undefined ? undefined : JSON.stringify(corpo),
  });
  if (!r.ok) throw await erroReq(r, "Falha ao exportar Excel");
  return r.blob();
}

export function baixarRelatorioXlsx(id: string): Promise<Blob> {
  return baixarXlsx("GET", `/relatorios/${id}/exportar.xlsx`);
}

export function exportarPaginaXlsx(paginaId: string, filtros?: any[], execucaoId?: string | null): Promise<Blob> {
  return baixarXlsx("POST", `/paginas/${paginaId}/exportar.xlsx`, { filtros: filtros ?? null, execucao_id: execucaoId ?? null });
}

export async function executarPipeline(
  id: string,
  parametros?: Record<string, unknown>,
  paginaId?: string,
  versaoNumero?: number | null,
): Promise<any> {
  // Sem parâmetros → corpo omitido (mantém o contrato dos pipelines sem parâmetros).
  // Com parâmetros (checks) → POST { parametros: {<nome>: <valor>} }.
  // `paginaId` registra de qual página o operador disparou; `versaoNumero` roda uma versão
  // específica (a selecionada no Studio) SEM criar outra.
  let corpo: Record<string, unknown> | undefined;
  if (parametros !== undefined || paginaId || versaoNumero != null) {
    corpo = {};
    if (parametros !== undefined) corpo.parametros = parametros;
    if (paginaId) corpo.pagina_id = paginaId;
    if (versaoNumero != null) corpo.versao_numero = versaoNumero;
  }
  const r = await req("POST", `/pipelines/${id}/executar`, corpo);
  if (!r.ok) throw await erroReq(r, "Falha ao executar");
  return r.json();
}

// ── Execuções: histórico (lista) e trilha (detalhe) ────────────────────────────────────────
// `status` do banco (enum status_execucao). "concluida" sem cedilha — é o valor do enum, não copy.
export type StatusExecucao = "executando" | "concluida" | "falha";

// Como a execução foi disparada. `reexecucao` é o "rodar de novo" — execução NOVA, ligada à
// original por `reexecucao_de` (a original nunca é reaberta: ela é a evidência do que aconteceu).
export type OrigemExecucao = "manual" | "agendado" | "reexecucao";

// Uma linha do histórico. `pipeline_nome` é o que torna possível a visão "todos os pipelines".
export interface ExecucaoItem {
  id: string;
  pipeline_id: string;
  pipeline_nome: string;
  versao: number;
  status: StatusExecucao;
  erro: string | null; // truncado no backend (300 chars) — é dica de coluna, não o erro completo
  iniciada_em: string | null;
  concluida_em: string | null;
  // Início REAL: o primeiro nó que começou (nulo = nenhum começou). Na corrida, `iniciada_em` é o
  // instante do disparo para todas (pré-criação); medir a filha por ele soma a fila das
  // dependências. Ver `tempoNaCorrida` em `cflow/surfaces/cadeias.ts`.
  comecou_em: string | null;
  origem: OrigemExecucao;
  agendamento_id: string | null;
  agendamento_nome: string | null;
  pagina_id: string | null;
  pagina_nome: string | null;
  ator_nome: string | null;
  // Corrida a que esta execução pertence: um pipeline com fonte tipo `pipeline` dispara a cadeia
  // inteira, e as N execuções irmãs compartilham o `cadeia_id`. `cadeia_raiz` marca qual foi a
  // PEDIDA — as outras são dependências que rodaram por tabela.
  cadeia_id: string | null;
  cadeia_raiz: boolean;
  reexecucao_de: string | null;
  // Se ESTA linha casa os filtros atuais. A mãe vem mesmo sem casar, para ser o cabeçalho do
  // grupo; sem essa marca, filtrar "falha" mostraria a raiz concluída como se ela fosse a falha.
  casa_filtro: boolean;
}

// Uma CORRIDA: a execução pedida (`raiz`) e as dependências que rodaram no mesmo disparo.
// Execução fora de cadeia é um grupo de uma só, com `filhas: []`.
export interface GrupoExecucoes {
  chave: string;
  raiz: ExecucaoItem;
  filhas: ExecucaoItem[];
  total_no_grupo: number;
  casam_filtro: number; // quantas do grupo casam os filtros — o "N de M" do acordeão
}

// Filtros da lista — todos opcionais e combináveis. `ate` é EXCLUSIVO (janela meio-aberta, igual
// a `resolverJanela`); ambos em ISO com offset (o front resolve a janela em America/Sao_Paulo).
export interface FiltrosExecucoes {
  pipelineId?: string | null;
  status?: StatusExecucao | null;
  origem?: OrigemExecucao | null;
  de?: string | null;
  ate?: string | null;
  // Texto livre: pipeline, agendamento, página, quem disparou, erro, parâmetros e id (prefixo).
  // Cada palavra tem de casar; `chave=valor` casa um parâmetro exato (`entidade_id=188`).
  busca?: string | null;
  // Aba da classificação do PIPELINE — o mesmo contrato das abas da listagem (`camadas.ts`).
  aba?: AbaExecucoes | null;
  limite?: number;
  offset?: number;
}

export type AbaExecucoes =
  | "geral"
  | "sistema"
  | "bronze"
  | "silver"
  | "gold";

export interface PaginaExecucoes {
  // Uma CORRIDA por item, não uma execução: paginar por execução partiria uma cadeia entre duas
  // páginas — a mãe numa, as dependências na outra, sem nada dizendo que vieram do mesmo clique.
  itens: GrupoExecucoes[];
  total: number; // corridas (é o que a paginação percorre), não execuções
  limite: number;
  offset: number;
  // Quantas execuções há de cada status DENTRO dos filtros atuais, ignorando o filtro de status —
  // é o número que a UI mostra em cada chip. Vem na mesma consulta (e transação) da página, para
  // não custar 3 requisições nem divergir entre si.
  totais: { todas: number; concluida: number; falha: number; executando: number } | null;
  // Execuções por aba (`todas` + uma chave por aba), com todos os filtros MENOS a aba — status
  // incluído. É o número de cada aba da régua.
  totais_abas?: ({ todas: number } & Record<AbaExecucoes, number>) | null;
}

// GET /execucoes — histórico de TODOS os pipelines (ou de um, com `pipelineId`), paginado.
// Execução de pipeline marcado `sistema` só aparece para admin (filtrado no backend).
export async function listarExecucoes(f: FiltrosExecucoes = {}): Promise<PaginaExecucoes> {
  const q = new URLSearchParams();
  if (f.pipelineId) q.set("pipeline_id", f.pipelineId);
  if (f.status) q.set("status", f.status);
  if (f.origem) q.set("origem", f.origem);
  if (f.de) q.set("de", f.de);
  if (f.ate) q.set("ate", f.ate);
  if (f.busca?.trim()) q.set("busca", f.busca.trim());
  if (f.aba) q.set("aba", f.aba);
  q.set("limite", String(f.limite ?? 25));
  q.set("offset", String(f.offset ?? 0));
  const r = await req("GET", `/execucoes?${q.toString()}`);
  if (!r.ok) throw await erroReq(r, "Falha ao listar execuções");
  return r.json();
}

// Um nó DENTRO de uma execução — só nós que EXECUTARAM vêm na resposta (ver ExecucaoDetalhe).
export interface ExecNo {
  no_id: string;
  // TRÊS estados, não dois: o worker grava `executando` no nó ANTES de rodá-lo (`engine.py`,
  // `_marcar_no_iniciado`) — é o que permite à tela dizer QUAL nó está em andamento, e também
  // registrar a janela em que um efeito externo pode ter acontecido sem confirmação. Se o worker
  // morre nessa janela, a linha fica `executando` para sempre — e o front tipava isso como
  // "concluida".
  status: "concluida" | "falha" | "executando" | string;
  linhas?: number | null;
  hash?: string | null;
  snapshot?: string | null;
  duracao_ms?: number | null;
  reaproveitado?: boolean | null;
  efeito_disparado?: boolean | null;
  erro?: string | null;
  // Quando o nó COMEÇOU (ISO com offset). É o que dá o "há quanto tempo" do nó em andamento —
  // sem isso a tela sabe que algo roda, mas não se roda há 10 segundos ou há 40 minutos.
  iniciado_em?: string | null;
}

// GET /execucoes/{id}. `nos` traz SÓ os nós executados; a tela cruza com a definição da versão
// executada para descobrir os que não rodaram. `parametros` já vem redigido do backend.
export interface ExecucaoDetalhe {
  id: string;
  pipeline_id: string;
  versao: number;
  status: StatusExecucao;
  erro: string | null;
  iniciada_em: string | null;
  concluida_em: string | null;
  origem: OrigemExecucao;
  agendamento_id: string | null;
  agendamento_nome: string | null;
  parametros: Record<string, unknown>;
  pagina_id: string | null;
  pagina_nome: string | null;
  ator_nome: string | null;
  cadeia_id: string | null;
  cadeia_raiz: boolean;
  // De onde esta execução veio, quando é uma reexecução — e de que nó ela partiu.
  reexecucao_de: string | null;
  reexecucao_desde_no: string | null;
  nos: ExecNo[];
}

export async function consultarExecucao(eid: string): Promise<ExecucaoDetalhe> {
  const r = await req("GET", `/execucoes/${eid}`);
  if (!r.ok) throw await erroReq(r, "Falha ao consultar execução");
  return r.json();
}

// ── Mapa da corrida ───────────────────────────────────────────────────────────────────────────
// `pipeline` = fonte tipo pipeline (lê o resultado do outro); `publicado` = lê o que o outro
// publica no lake e, na mesma corrida, espera ele publicar.
export type TipoArestaCorrida = "pipeline" | "publicado";

// Aresta entre EXECUÇÕES da corrida: `para` dependeu de `de`.
export interface ArestaCorrida {
  de: string;
  para: string;
  tipo: TipoArestaCorrida;
}

// GET /corridas/{cadeia_id}. As arestas são reconstruídas das definições das versões que rodaram
// (o disparo não as grava). `raiz_id` nulo = a raiz é de pipeline `sistema` e quem vê não é admin.
export interface CorridaMapa {
  cadeia_id: string;
  raiz_id: string | null;
  execucoes: ExecucaoItem[];
  arestas: ArestaCorrida[];
}

export async function obterCorrida(cadeiaId: string): Promise<CorridaMapa> {
  const r = await req("GET", `/corridas/${cadeiaId}`);
  if (!r.ok) throw await erroReq(r, "Falha ao abrir a corrida");
  return r.json();
}

// ── Reexecutar ────────────────────────────────────────────────────────────────────────────────
// Um efeito do trecho a reexecutar. `idempotente: false` = repetir DUPLICA (e-mail reenviado,
// POST refeito, INSERT append repetido) — é o que a confirmação precisa dizer com todas as letras.
export interface EfeitoPrevisto {
  no_id: string;
  tipo: string;
  idempotente: boolean;
}

export interface PlanoReexecucao {
  execucao_id: string;
  pipeline_id: string;
  pipeline_nome: string;
  versao: number;
  desde_no: string | null;
  nos_a_executar: string[];
  nos_reaproveitados: string[];
  efeitos: EfeitoPrevisto[];
  // As SAÍDAS do trecho, nomeadas: republicar não duplica nada, mas move o `_atual.json` que outro
  // consumidor lê — quem clica precisa ver QUAL ponteiro vai mudar.
  publicacoes?: PublicacaoPrevista[];
  // Montante de cadeia cuja raiz declara `montante_publica: false`: recalcula sem publicar.
  sem_publicar?: boolean;
  sem_publicar_motivo?: string | null;
  // NOMES dos parâmetros de credencial — nunca os valores (o persistido é a marca de redação).
  // Quem reexecuta tem de informá-los de novo.
  parametros_segredo: string[];
  parametros: Record<string, unknown>;
}

export interface PublicacaoPrevista {
  no_id: string;
  destino: string; // arquivo | lake | iceberg
  alvo: string | null; // dataset (lake/iceberg) ou nome do arquivo
  camada: string | null; // só no lake: silver | gold
  publica: boolean;
}

// O que uma reexecução FARIA, sem fazer — alimenta a confirmação. Existe separado do POST porque
// e-mail reenviado e escrita repetida não têm desfazer: a lista tem de ser vista antes do clique.
export async function planoReexecucao(eid: string, desdeNo?: string | null): Promise<PlanoReexecucao> {
  const q = desdeNo ? `?desde_no=${encodeURIComponent(desdeNo)}` : "";
  const r = await req("GET", `/execucoes/${eid}/reexecucao/plano${q}`);
  if (!r.ok) throw await erroReq(r, "Falha ao montar o plano de reexecução");
  return r.json();
}

// POST /execucoes/{id}/reexecutar → cria uma execução NOVA e devolve `{id, status, versao}`.
// `parametros` serve SÓ para repor os de credencial; os demais vêm da execução original (mudar
// parâmetro não é reexecutar — para isso existe o Executar do editor).
export async function reexecutar(
  eid: string,
  opts: { desdeNo?: string | null; parametros?: Record<string, unknown> } = {},
): Promise<{ id: string; status: StatusExecucao; versao?: number; erro?: string }> {
  const corpo: Record<string, unknown> = {};
  if (opts.desdeNo) corpo.desde_no = opts.desdeNo;
  if (opts.parametros && Object.keys(opts.parametros).length) corpo.parametros = opts.parametros;
  const r = await req("POST", `/execucoes/${eid}/reexecutar`, corpo);
  if (!r.ok) throw await erroReq(r, "Falha ao reexecutar");
  return r.json();
}

// --- Carimbo de obsoleto do ponteiro de escopo (admin) ---
// O escopo viaja como lista `chave=valor` NA ORDEM do caminho: o endereço do ponteiro é posicional.
// A decisão (pode/não pode e por quê) é do servidor; `bloqueio.mensagem` é o que a tela mostra.
export interface PlanoEscopo {
  camada: string;
  dataset: string;
  particao: string[];
  caminho: string;
  estado: "vigente" | "obsoleto" | "ausente";
  layout_vigente: string[] | null;
  produtores: { pipeline_id: string; nome: string; chaves: string[] }[];
  aponta: {
    execucao_id?: string;
    pipeline_id?: string;
    momento?: string;
    linhas?: number;
    caminho?: string;
  } | null;
  carimbo: { chaves?: string[]; desde?: string; motivo?: string } | null;
  pode_carimbar: boolean;
  pode_descarimbar: boolean;
  bloqueio: { codigo: string; mensagem: string } | null;
  // Só na resposta de carimbar/descarimbar: `mudou: false` = já estava no estado pedido.
  mudou?: boolean;
  simulado?: boolean;
}

function rotaEscopo(camada: string, dataset: string): string {
  return `/publicados/${encodeURIComponent(camada)}/${encodeURIComponent(dataset)}/escopo`;
}

// Na gold o ambiente (`dev`|`prd`) é a raiz física do ponteiro e o servidor o EXIGE — sem default,
// porque em branco não pode cair em produção. Fora da gold não vai.
function qsAmbiente(ambiente?: string | null): string {
  return ambiente ? `ambiente=${encodeURIComponent(ambiente)}` : "";
}

// O ponteiro de UM escopo e o que carimbar/descarimbar fariam com ele. Não grava nada.
export async function consultarEscopo(
  camada: string,
  dataset: string,
  particao: string[],
  ambiente?: string | null,
): Promise<PlanoEscopo> {
  const q = [...particao.map((p) => `particao=${encodeURIComponent(p)}`), qsAmbiente(ambiente)]
    .filter(Boolean)
    .join("&");
  const r = await req("GET", `${rotaEscopo(camada, dataset)}?${q}`);
  if (!r.ok) throw await erroReq(r, "Falha ao consultar o escopo");
  return r.json();
}

// Os escopos de layout ANTERIOR de um dataset, com o plano de cada ponteiro. Sai das execuções das
// versões anteriores (e do índice imutável de cada corrida) — vale também depois da primeira
// corrida do layout novo, quando o índice do dataset já não os lista. Não grava nada.
export interface DescobertaEscopos {
  camada: string;
  dataset: string;
  layout_vigente: string[] | null;
  layouts_anteriores: {
    chaves: string[];
    versoes: { pipeline_id: string; nome: string; numeros: number[] }[];
    execucoes: number;
    sem_indice: number;
  }[];
  escopos: PlanoEscopo[];
  ausentes: number;
  truncado: boolean;
}

export async function descobrirEscopos(
  camada: string,
  dataset: string,
  ambiente?: string | null,
): Promise<DescobertaEscopos> {
  const q = qsAmbiente(ambiente);
  const r = await req(
    "GET",
    `/publicados/${encodeURIComponent(camada)}/${encodeURIComponent(dataset)}/escopos-anteriores${q ? `?${q}` : ""}`,
  );
  if (!r.ok) throw await erroReq(r, "Falha ao descobrir os escopos do layout anterior");
  return r.json();
}

// Carimba (ou desfaz o carimbo de) um escopo. Auditado no servidor; o motivo é obrigatório.
export async function mudarCarimboEscopo(
  acao: "carimbar" | "descarimbar",
  camada: string,
  dataset: string,
  corpo: { particao: string[]; motivo: string; simular?: boolean },
  ambiente?: string | null,
): Promise<PlanoEscopo> {
  const q = qsAmbiente(ambiente);
  const r = await req("POST", `${rotaEscopo(camada, dataset)}/${acao}${q ? `?${q}` : ""}`, corpo);
  if (!r.ok)
    throw await erroReq(
      r,
      acao === "carimbar" ? "Falha ao carimbar o escopo" : "Falha ao desfazer o carimbo",
    );
  return r.json();
}

// Execução concluída de HOJE com os mesmos parâmetros (cache do dia: manual ou agendado).
export interface ExecDoDia {
  id: string;
  iniciada_em: string | null;
  concluida_em: string | null;
  origem: "manual" | "agendado";
  agendamento_id: string | null;
  ator_nome: string | null;
}

// Procura execuções de hoje desta página+pipeline com os MESMOS parâmetros (não executa nada).
export async function execucoesDoDia(
  id: string,
  parametros: Record<string, unknown>,
  paginaId: string,
): Promise<ExecDoDia[]> {
  const r = await req("POST", `/pipelines/${id}/execucoes-do-dia`, {
    parametros,
    pagina_id: paginaId,
  });
  if (!r.ok) throw await erroReq(r, "Falha ao buscar relatórios do dia");
  return r.json();
}

// --- Agendamento (recorrência amigável + vigência + alvo pipeline/página) ---
export interface AgendaSpec {
  tipo: "horaria" | "diaria" | "semanal" | "mensal";
  a_cada_horas?: number | null; // horária
  horario?: string | null; // "HH:MM" (diária/semanal/mensal)
  dias_semana?: number[] | null; // semanal: 0=domingo..6=sábado
  dia_mes?: number | null; // mensal: 1..31
}

export interface AgendamentoBody {
  agenda: AgendaSpec;
  nome?: string | null;
  pagina_id?: string | null;
  parametros?: Record<string, unknown> | null;
  vigencia_inicio?: string | null; // ISO yyyy-mm-dd
  vigencia_fim?: string | null;
}

export interface Agendamento {
  id: string;
  nome: string | null;
  pipeline_id?: string; // presente na lista global (GET /agendamentos)
  pipeline_nome?: string | null;
  pagina_id: string | null;
  pagina_nome: string | null;
  parametros: Record<string, unknown>;
  agenda: AgendaSpec;
  ativo: boolean;
  vigencia_inicio: string | null;
  vigencia_fim: string | null;
  proxima_em: string | null;
  ultimo_status: string | null;
  // Instante do último disparo (America/Sao_Paulo no servidor). "falhou" sem data não diz se o
  // problema é de agora ou de junho.
  ultimo_disparo_em?: string | null;
  ultima_execucao_id: string | null;
}

export async function listarAgendamentos(pipelineId: string): Promise<Agendamento[]> {
  const r = await req("GET", `/pipelines/${pipelineId}/agendamentos`);
  if (!r.ok) throw await erroReq(r, "Falha ao listar agendamentos");
  return r.json();
}

export async function criarAgendamento(pipelineId: string, body: AgendamentoBody): Promise<{ id: string }> {
  const r = await req("POST", `/pipelines/${pipelineId}/agendamentos`, body);
  if (!r.ok) throw await erroReq(r, "Falha ao criar agendamento");
  return r.json();
}

export async function editarAgendamento(agendamentoId: string, body: AgendamentoBody): Promise<void> {
  const r = await req("PATCH", `/agendamentos/${agendamentoId}`, body);
  if (!r.ok) throw await erroReq(r, "Falha ao editar agendamento");
}

export async function ativarAgendamento(agendamentoId: string): Promise<void> {
  const r = await req("POST", `/agendamentos/${agendamentoId}/ativar`);
  if (!r.ok) throw await erroReq(r, "Falha ao ativar agendamento");
}

export async function desativarAgendamento(agendamentoId: string): Promise<void> {
  const r = await req("POST", `/agendamentos/${agendamentoId}/desativar`);
  if (!r.ok) throw await erroReq(r, "Falha ao desativar agendamento");
}

export async function executarAgoraAgendamento(
  agendamentoId: string,
): Promise<{ status: string; execucao_id: string | null; execucoes?: number }> {
  const r = await req("POST", `/agendamentos/${agendamentoId}/executar-agora`);
  if (!r.ok) throw await erroReq(r, "Falha ao executar agendamento");
  return r.json();
}

// --- O que um disparo roda JUNTO ---
// Agendar (ou "executar agora") um pipeline com fonte tipo pipeline roda os upstreams, com todos os
// efeitos deles. A tela mostra isto ANTES do clique; a decisão é a mesma função do disparo.
export interface MontanteCadeia {
  pipeline_id: string;
  nome: string;
  versao: number;
  nivel: number;
  efeitos: { no_id: string; tipo: string }[];
  publicacoes: PublicacaoPrevista[];
}

export interface PlanoCadeia {
  pipeline_id: string;
  nome: string;
  versao: number | null;
  montantes: MontanteCadeia[];
  // A raiz declara `montante_publica: false`: a montante calcula sem publicar.
  sem_publicar: boolean;
  // A recusa que o disparo daria (ex.: montante com efeito sob a marca) — como dado.
  recusa: { codigo: string; mensagem: string } | null;
}

export async function planoCadeia(pipelineId: string): Promise<PlanoCadeia> {
  const r = await req("GET", `/pipelines/${pipelineId}/cadeia`);
  if (!r.ok) throw await erroReq(r, "Falha ao montar a cadeia do pipeline");
  return r.json();
}

// Lista GLOBAL de agendamentos (com pipeline_nome) — tela de Agendamentos no padrão das entidades.
export async function listarAgendamentosTodos(): Promise<Agendamento[]> {
  const r = await req("GET", "/agendamentos");
  if (!r.ok) throw await erroReq(r, "Falha ao listar agendamentos");
  return r.json();
}

// Histórico de execuções sumarizado por dia (aba Histórico).
export interface ResumoDia {
  dia: string | null;
  total: number;
  concluidas: number;
  falhas: number;
  executando: number;
  agendadas: number;
  manuais: number;
  ultima: string | null;
}
export async function resumoExecucoes(
  pipelineId: string,
  agendamentoId?: string,
): Promise<{ dias: ResumoDia[]; total: number }> {
  const qs = agendamentoId ? `?agendamento_id=${agendamentoId}` : "";
  const r = await req("GET", `/pipelines/${pipelineId}/execucoes-resumo${qs}`);
  if (!r.ok) throw await erroReq(r, "Falha ao carregar o histórico");
  return r.json();
}

// Execuções de um agendamento (cada run) — drill de um dia do histórico.
export interface ExecAgendamento {
  id: string;
  iniciada_em: string | null;
  concluida_em: string | null;
  status: string;
  parametros: Record<string, unknown>;
}
export async function listarExecucoesAgendamento(
  agendamentoId: string,
  dia?: string,
): Promise<ExecAgendamento[]> {
  const qs = dia ? `?dia=${dia}` : "";
  const r = await req("GET", `/agendamentos/${agendamentoId}/execucoes${qs}`);
  if (!r.ok) throw await erroReq(r, "Falha ao carregar as execuções");
  return r.json();
}

export async function previewNo(id: string, noId: string, definicao: unknown): Promise<PreviewResultado> {
  const r = await req("POST", `/pipelines/${id}/nos/${noId}/preview`, { definicao });
  // `erroDe` anexa os `detalhes` (ex.: a causa do DuckDB) — sem isso a falha do nó ficava só no log.
  if (!r.ok) throw new Error(await erroDe(r, "Falha no preview"));
  return r.json();
}

// Lê todas as linhas de uma conexão `arquivo` (mapa de referência). Endpoint dedicado
// (POST /conexoes/{id}/amostra) — sem o hack de chamar o preview de um pipeline-dummy.
export async function lerConexaoArquivo(conexaoId: string): Promise<Record<string, any>[]> {
  const r = await req("POST", `/conexoes/${conexaoId}/amostra`, { sql: "", limite: 2000 });
  if (!r.ok) throw await erroReq(r, "Falha ao ler o mapa de referência");
  const resp = await r.json();
  return (resp.linhas ?? []) as Record<string, any>[];
}

// Resolve as OPÇÕES DINÂMICAS de um parâmetro a partir de uma conexão: lê as linhas (mesmo caminho
// Opções dinâmicas de um dropdown a partir de `opcoes_fonte`. Resolve no backend
// (POST /conexoes/{id}/opcoes) → funciona p/ DB (SELECT) e arquivo, com rótulo≠valor (apelido↔id).
// Retorna `{valor, rotulo}[]` DISTINTOS/ordenados. Acessível ao operador (auth-só no backend).
export async function opcoesDinamicas(
  fonte: OpcoesFonte,
  pipelineId?: string,
): Promise<Opcao[]> {
  // Origem "no": resolve pela saída de um nó do pipeline (exige pipelineId do chamador).
  if (fonte.origem === "no") {
    if (!pipelineId) throw new Error("opções por nó exigem o pipeline de origem.");
    const r = await req("POST", `/pipelines/${pipelineId}/opcoes-no`, {
      no_id: fonte.no_id,
      coluna_valor: fonte.coluna_valor ?? fonte.coluna,
      coluna_rotulo: fonte.coluna_rotulo ?? null,
    });
    if (!r.ok) throw await erroReq(r, "Falha ao carregar as opções");
    return (await r.json()) as Opcao[];
  }
  const r = await req("POST", `/conexoes/${fonte.conexao_id}/opcoes`, {
    sql: fonte.sql ?? null,
    coluna_valor: fonte.coluna_valor ?? fonte.coluna, // legado: coluna → valor
    coluna_rotulo: fonte.coluna_rotulo ?? null,
    limite: 5000,
  });
  if (!r.ok) throw await erroReq(r, "Falha ao carregar as opções");
  return (await r.json()) as Opcao[];
}

// ── Trilha de auditoria (LEITURA) ────────────────────────────────────────────
// A trilha é append-only (só SELECT/INSERT) e a consulta não é auditada; a borda exige perfil
// auditor ou admin. Ela vive no cliente compartilhado porque é a ÚNICA fonte de duas respostas que
// a API ainda não expõe direto: "que permissão já está concedida" e "quando fulano entrou pela
// última vez". Ler dali é derivar — a tela que usa isto tem de dizer que é derivado.

export interface RegistroAuditoria {
  id: string;
  ator_id: string | null;
  ator_tipo: string;
  acao: string;
  recurso_tipo: string;
  recurso_id: string | null;
  antes: Record<string, unknown> | null;
  depois: Record<string, unknown> | null;
  request_id: string | null;
  ip: string | null;
  ocorrido_em: string; // ISO já convertido para America/Sao_Paulo pela borda
}

export interface FiltroAuditoria {
  acao?: string;
  recurso_tipo?: string;
  recurso_id?: string;
  ator_id?: string;
  de?: string; // ISO com fuso
  ate?: string;
  pagina?: number;
  tamanho?: number; // teto do backend: 500
}

export async function consultarAuditoria(f: FiltroAuditoria): Promise<RegistroAuditoria[]> {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(f)) {
    if (v !== undefined && v !== null && v !== "") q.set(k, String(v));
  }
  const r = await req("GET", `/auditoria?${q.toString()}`);
  if (!r.ok) throw await erroReq(r, "Falha ao consultar a trilha de auditoria");
  return (await r.json()) as RegistroAuditoria[];
}

/** Percorre TODAS as páginas da trilha para um filtro.
 *
 *  A rota ordena por `ocorrido_em` ASCENDENTE: quem reconstrói estado atual (permissão concedida,
 *  último login) precisa reprocessar os eventos NESSA ordem, e por isso precisa de todas as
 *  páginas — parar no meio devolve o retrato mais VELHO, não o mais novo. `completo:false` diz que
 *  o teto de páginas foi atingido: aí a tela mostra o que leu dizendo que a leitura é parcial, em
 *  vez de passar um retrato incompleto por retrato.
 */
export async function consultarAuditoriaTudo(
  f: FiltroAuditoria,
  maxPaginas = 20,
): Promise<{ registros: RegistroAuditoria[]; completo: boolean }> {
  const tamanho = Math.min(Math.max(f.tamanho ?? 500, 1), 500);
  const registros: RegistroAuditoria[] = [];
  for (let pagina = 1; pagina <= maxPaginas; pagina++) {
    const lote = await consultarAuditoria({ ...f, pagina, tamanho });
    registros.push(...lote);
    if (lote.length < tamanho) return { registros, completo: true };
  }
  return { registros, completo: false };
}
