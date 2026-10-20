// Formulário de CONTRATO de fonte de ingestão — cadastro e edição.
//
// Molde: `NovaConexao` em `Conexoes.tsx` — `FormContrato` achatado (`ingestao.ts`) +
// `contratoDeForm`/`formDeContrato` (conversão bidirecional) + blocos condicionais por `classe`.
// A diferença de risco para Conexões: aqui salvar NÃO toca rede (só grava o contrato no banco), daí
// a confirmação ser simples (`ConfirmarAcao` sem `perigo`) — o guardrail de verdade fica em
// `ConfirmarBackfillReplay`, que é quem de fato dispara requisição à origem pública.
import { useEffect, useState } from "react";

import * as api from "../../api/client";
import { ConfirmarAcao, Icon } from "../ui";
import {
  CLASSES_FONTE,
  contratoDeForm,
  estadoInicialContrato,
  formDeContrato,
  type FormContrato,
} from "./ingestao";

interface ErroTela {
  mensagem: string;
  requestId?: string;
}

function erroDe(e: unknown, fallback: string): ErroTela {
  const err = e as { requestId?: string; request_id?: string } | null;
  const req = err?.requestId ?? err?.request_id;
  return {
    mensagem: e instanceof Error && e.message ? e.message : fallback,
    requestId: typeof req === "string" ? req : undefined,
  };
}

function Aviso({ erro }: { erro: ErroTela }) {
  return (
    <div className="exec-banner-falha" role="alert" style={{ marginBottom: 14 }}>
      <span className="dot" />
      <span>
        {erro.mensagem}
        {erro.requestId && (
          <span style={{ display: "block", fontSize: 11.5, opacity: 0.9 }}>
            Código para suporte: {erro.requestId}
          </span>
        )}
      </span>
    </div>
  );
}

function Campo({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label style={{ display: "block" }}>
      <span className="fld">{label}</span>
      {children}
    </label>
  );
}

interface FormaContratoFonteProps {
  onVoltar: () => void;
  onSalvo: () => void;
  // Presente = modo EDIÇÃO: busca a fonte (com o `contrato` completo, que a listagem não traz) antes
  // de abrir o formulário. Ausente = cadastro novo.
  editarId?: string;
}

export default function FormaContratoFonte({ onVoltar, onSalvo, editarId }: FormaContratoFonteProps) {
  const editando = !!editarId;

  const [carregandoFonte, setCarregandoFonte] = useState(editando);
  const [erroCarregar, setErroCarregar] = useState<ErroTela | null>(null);
  const [fonteId, setFonteId] = useState<string | null>(editarId ?? null);
  const [nome, setNome] = useState("");
  const [ativo, setAtivo] = useState(true);
  const [form, setForm] = useState<FormContrato>(estadoInicialContrato());
  const [conexoesObjeto, setConexoesObjeto] = useState<api.Conexao[]>([]);
  const [avancado, setAvancado] = useState(false); // accordion do grão — fechado por default

  const [salvando, setSalvando] = useState(false);
  const [validando, setValidando] = useState(false);
  const [validacao, setValidacao] = useState<{ ok: boolean; texto: string } | null>(null);
  const [erroForm, setErroForm] = useState<ErroTela | null>(null);
  const [confirmando, setConfirmando] = useState(false);
  const [salvo, setSalvo] = useState(false);

  useEffect(() => {
    api
      .listarConexoes()
      .then((cs) => setConexoesObjeto(cs.filter((c) => c.tipo === "objeto")))
      .catch(() => setConexoesObjeto([]));
  }, []);

  useEffect(() => {
    if (!editarId) return;
    let vivo = true;
    setCarregandoFonte(true);
    api
      .obterFonteIngestao(editarId)
      .then((f) => {
        if (!vivo) return;
        setNome(f.nome);
        setAtivo(f.ativo !== false);
        setForm({ ...formDeContrato(f.contrato ?? {}), destinoConexaoId: f.destino_conexao_id ?? "" });
      })
      .catch((e) => vivo && setErroCarregar(erroDe(e, "Falha ao carregar a fonte.")))
      .finally(() => vivo && setCarregandoFonte(false));
    return () => {
      vivo = false;
    };
  }, [editarId]);

  function set<K extends keyof FormContrato>(campo: K, valor: FormContrato[K]) {
    setForm((f) => ({ ...f, [campo]: valor }));
  }

  function trocarClasse(classe: string) {
    // Troca de classe reinicia os campos de `acesso` (mesma regra de `trocarTipo` em Conexões): o
    // formulário sujo trocando de classe não pode vazar um campo da classe anterior no contrato.
    setForm((f) => ({ ...estadoInicialContrato(classe), dataset: f.dataset, cadencia: f.cadencia }));
    setValidacao(null);
    setErroForm(null);
  }

  async function validar() {
    setErroForm(null);
    setValidacao(null);
    setValidando(true);
    try {
      await api.validarContratoIngestao(contratoDeForm(form));
      setValidacao({ ok: true, texto: "Contrato válido." });
    } catch (e) {
      const erro = erroDe(e, "Contrato inválido.");
      setValidacao({ ok: false, texto: erro.mensagem });
    } finally {
      setValidando(false);
    }
  }

  async function salvar() {
    setErroForm(null);
    if (!nome.trim()) {
      setErroForm({ mensagem: "Informe um nome para a fonte." });
      return;
    }
    if (!form.dataset.trim()) {
      setErroForm({ mensagem: "Informe o dataset (a chave que o nó 'fonte lake' referencia)." });
      return;
    }
    setSalvando(true);
    try {
      const contrato = contratoDeForm(form);
      const destino_conexao_id = form.destinoConexaoId || null;
      if (editando && fonteId) {
        await api.editarFonteIngestao(fonteId, { contrato, destino_conexao_id });
        setSalvo(true);
      } else {
        const { id } = await api.criarFonteIngestao({ nome: nome.trim(), contrato, destino_conexao_id });
        setFonteId(id);
        setSalvo(true);
        onSalvo();
      }
    } catch (e) {
      setErroForm(erroDe(e, editando ? "Falha ao editar a fonte." : "Falha ao cadastrar a fonte."));
    } finally {
      setSalvando(false);
    }
  }

  async function alternarAtivo(novo: boolean) {
    if (!fonteId) return;
    setErroForm(null);
    try {
      await api.editarFonteIngestao(fonteId, { ativo: novo });
      setAtivo(novo);
    } catch (e) {
      setErroForm(erroDe(e, "Falha ao alterar o estado da fonte."));
    }
  }

  if (carregandoFonte) {
    return (
      <div className="pad">
        <div className="card lc-empty" role="status">
          <div className="big">Carregando fonte…</div>
        </div>
      </div>
    );
  }

  return (
    <>
      {confirmando && (
        <ConfirmarAcao
          titulo={editando ? "Salvar alterações do contrato" : "Cadastrar fonte de ingestão"}
          corpo={
            <>
              Isto {editando ? "atualiza" : "cria"} o contrato de <b>{form.dataset || "(sem dataset)"}</b>
              . {editando
                ? "A próxima recarga (incremental, backfill ou replay) usa a regra nova."
                : "Depois de criada, o dataset não muda mais — é a chave que o nó 'fonte lake' referencia."}
            </>
          }
          rotuloConfirmar="Salvar"
          onConfirmar={() => {
            setConfirmando(false);
            void salvar();
          }}
          onCancelar={() => setConfirmando(false)}
        />
      )}

      <div className="page-head">
        <button className="link-back" onClick={onVoltar}>
          ← Fontes de ingestão
        </button>
        <h1 className="display">{editando ? "Editar contrato" : "Nova fonte de ingestão"}</h1>
        <p className="sub">
          {editando
            ? "O dataset não muda. Contrato editado vale a partir da próxima recarga."
            : "URL, formato e janela vão para o contrato declarativo — não para código."}
        </p>
      </div>

      <div className="pad">
        {erroCarregar && <Aviso erro={erroCarregar} />}
        {erroForm && <Aviso erro={erroForm} />}

        {salvo && (
          <div className="run-banner" role="status" style={{ marginTop: 0, marginBottom: 14 }}>
            <span className="dot" />
            <span>
              {editando ? "Alterações salvas." : "Fonte cadastrada."} Valide o contrato ou volte à
              lista.
            </span>
          </div>
        )}

        <div className="card shelf" style={{ marginBottom: 14, padding: "13px 15px" }}>
          <h4 id="rot-fonte-nome">Nome</h4>
          <input
            className="nm"
            data-testid="fonte-nome"
            aria-labelledby="rot-fonte-nome"
            value={nome}
            onChange={(e) => setNome(e.target.value)}
            placeholder="Ex.: CVM — informe diário FI"
          />
        </div>

        <div className="card shelf" style={{ marginBottom: 14, padding: "13px 15px" }}>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 160px", gap: 12 }}>
            <Campo label="Dataset">
              <input
                className="nm"
                data-testid="fonte-dataset"
                value={form.dataset}
                disabled={editando}
                onChange={(e) => set("dataset", e.target.value.trim())}
                placeholder="cvm_inf_diario_fi"
              />
            </Campo>
            <Campo label="Cadência">
              <select
                className="nm"
                data-testid="fonte-cadencia"
                value={form.cadencia}
                onChange={(e) => set("cadencia", e.target.value)}
              >
                <option value="manual">Só manual</option>
                <option value="diaria">Diária</option>
                <option value="semanal">Semanal</option>
                <option value="mensal">Mensal</option>
                <option value="anual">Anual</option>
              </select>
            </Campo>
          </div>
          <p style={{ fontSize: 12, color: "var(--ink-2)", marginTop: 6 }}>
            {editando
              ? "O dataset não muda depois de criado — é a chave que o nó 'fonte lake' referencia."
              : "Slug estável (letras minúsculas, dígitos, _). Não muda depois de criado."}
          </p>

          <Campo label="Descrição (opcional)">
            <input
              className="nm"
              value={form.descricao}
              onChange={(e) => set("descricao", e.target.value)}
              placeholder="O que esta fonte traz"
            />
          </Campo>

          <Campo label="Destino no lake">
            <select
              className="nm"
              data-testid="fonte-destino"
              value={form.destinoConexaoId}
              onChange={(e) => set("destinoConexaoId", e.target.value)}
            >
              <option value="">Armazenamento local (padrão)</option>
              {conexoesObjeto.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.nome}
                </option>
              ))}
            </select>
          </Campo>
          {editando && fonteId && (
            <p style={{ fontSize: 12, color: "var(--ink-2)", marginTop: 6 }}>
              {ativo ? "Entra na recarga automática." : "Desativada — não entra na recarga automática."}{" "}
              <button
                type="button"
                className="lk"
                data-testid="fonte-alternar-ativo"
                onClick={() => void alternarAtivo(!ativo)}
              >
                {ativo ? "Desativar" : "Reativar"}
              </button>
            </p>
          )}
        </div>

        <div className="card shelf" style={{ marginBottom: 14, padding: "13px 15px" }}>
          <h4>Classe de acesso</h4>
          <select
            className="nm"
            data-testid="fonte-classe"
            value={form.classe}
            disabled={editando}
            onChange={(e) => trocarClasse(e.target.value)}
          >
            {CLASSES_FONTE.map((c) => (
              <option key={c.valor} value={c.valor}>
                {c.rotulo} — {c.meta}
              </option>
            ))}
          </select>
          {editando && (
            <p style={{ fontSize: 12, color: "var(--ink-2)", marginTop: 6 }}>
              A classe não muda depois de criada — fonte que precisa de outra classe é cadastro novo.
            </p>
          )}

          <div style={{ display: "grid", gridTemplateColumns: "1fr", gap: 12, marginTop: 12 }}>
            {form.classe !== "portal_download" && (
              <Campo label="URL padrão">
                <input
                  className="nm"
                  data-testid="fonte-url"
                  value={form.urlPadrao}
                  onChange={(e) => set("urlPadrao", e.target.value)}
                  placeholder="https://dados.origem.gov.br/arquivo_{ano}{mes}.zip"
                />
              </Campo>
            )}
            <p style={{ fontSize: 11.5, color: "var(--ink-2)", margin: 0 }}>
              Placeholders por fatia: <code>{"{ano}"}</code> <code>{"{ano_curto}"}</code>{" "}
              <code>{"{mes}"}</code> <code>{"{dia}"}</code> <code>{"{ano_mes}"}</code>{" "}
              <code>{"{data}"}</code>.
            </p>

            {form.classe === "http_file" && (
              <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <input
                  type="checkbox"
                  checked={form.condicional}
                  onChange={(e) => set("condicional", e.target.checked)}
                />
                Requisição condicional (If-None-Match) — pula quando a origem não mudou
              </label>
            )}

            {form.classe === "rest_api" && (
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12 }}>
                <Campo label="Parâmetro de início">
                  <input
                    className="nm"
                    value={form.paramInicio}
                    onChange={(e) => set("paramInicio", e.target.value)}
                    placeholder="dataInicial"
                  />
                </Campo>
                <Campo label="Parâmetro de fim">
                  <input
                    className="nm"
                    value={form.paramFim}
                    onChange={(e) => set("paramFim", e.target.value)}
                    placeholder="dataFinal"
                  />
                </Campo>
                <Campo label="Formato da data">
                  <input
                    className="nm"
                    value={form.formatoData}
                    onChange={(e) => set("formatoData", e.target.value)}
                    placeholder="%d/%m/%Y"
                  />
                </Campo>
              </div>
            )}

            {form.classe === "odata" && (
              <>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12 }}>
                  <Campo label="Itens por página ($top)">
                    <input
                      className="nm"
                      inputMode="numeric"
                      value={form.top}
                      onChange={(e) => set("top", e.target.value)}
                      placeholder="1000"
                    />
                  </Campo>
                  <Campo label="Teto de páginas">
                    <input
                      className="nm"
                      inputMode="numeric"
                      value={form.maxPaginas}
                      onChange={(e) => set("maxPaginas", e.target.value)}
                      placeholder="200"
                    />
                  </Campo>
                  <Campo label="Campo com os itens">
                    <input
                      className="nm"
                      value={form.campoItens}
                      onChange={(e) => set("campoItens", e.target.value)}
                      placeholder="value"
                    />
                  </Campo>
                </div>
                <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <input
                    type="checkbox"
                    checked={form.paginar}
                    onChange={(e) => set("paginar", e.target.checked)}
                  />
                  Paginar com $skip (desligue para serviço que rejeita $skip, ex.: PixLiquidadosAtual)
                </label>
              </>
            )}

            {form.classe === "portal_download" && (
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                <Campo label="Página-índice">
                  <input
                    className="nm"
                    value={form.paginaIndice}
                    onChange={(e) => set("paginaIndice", e.target.value)}
                    placeholder="https://portal.origem.gov.br/…"
                  />
                </Campo>
                <Campo label="Padrão do link (regex)">
                  <input
                    className="nm"
                    value={form.padraoLink}
                    onChange={(e) => set("padraoLink", e.target.value)}
                    placeholder="\.(csv|txt|zip)$"
                  />
                </Campo>
                <Campo label="Teto de arquivos">
                  <input
                    className="nm"
                    inputMode="numeric"
                    value={form.maxArquivos}
                    onChange={(e) => set("maxArquivos", e.target.value)}
                    placeholder="20"
                  />
                </Campo>
              </div>
            )}
          </div>
        </div>

        <div className="card shelf" style={{ marginBottom: 14, padding: "13px 15px" }}>
          <h4>Janela</h4>
          <div style={{ display: "grid", gridTemplateColumns: "160px 1fr", gap: 12 }}>
            <Campo label="Tipo">
              <select
                className="nm"
                data-testid="fonte-janela-tipo"
                value={form.janelaTipo}
                onChange={(e) => set("janelaTipo", e.target.value)}
              >
                <option value="diaria">Diária</option>
                <option value="mensal">Mensal</option>
                <option value="anual">Anual</option>
                <option value="periodo">Período (passo de dias)</option>
              </select>
            </Campo>
            <Campo label="Início (piso do backfill)">
              <input
                className="nm"
                value={form.janelaInicio}
                onChange={(e) => set("janelaInicio", e.target.value)}
                placeholder="2024-01 (ou AAAA / AAAA-MM-DD)"
              />
            </Campo>
          </div>
          {form.janelaTipo === "periodo" && (
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
              <Campo label="Passo máximo (dias)">
                <input
                  className="nm"
                  inputMode="numeric"
                  value={form.passoMaxDias}
                  onChange={(e) => set("passoMaxDias", e.target.value)}
                  placeholder="3650"
                />
              </Campo>
              <label style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 22 }}>
                <input
                  type="checkbox"
                  checked={form.alinharMes}
                  onChange={(e) => set("alinharMes", e.target.checked)}
                />
                Alinhar ao fim do mês (evita duplicar o ponto de série mensal)
              </label>
            </div>
          )}
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            <Campo label="Atraso da publicação (dias, opcional)">
              <input
                className="nm"
                inputMode="numeric"
                value={form.atrasoDias}
                onChange={(e) => set("atrasoDias", e.target.value)}
                placeholder="em branco = sem atraso"
              />
            </Campo>
            <label style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 22 }}>
              <input
                type="checkbox"
                checked={form.fatiaCompetencia}
                onChange={(e) => set("fatiaCompetencia", e.target.checked)}
              />
              A chave da fatia é a competência do dado
            </label>
          </div>
        </div>

        <div className="card shelf" style={{ marginBottom: 14, padding: "13px 15px" }}>
          <h4>Formato do arquivo</h4>
          <div style={{ display: "grid", gridTemplateColumns: "160px 1fr 100px", gap: 12 }}>
            <Campo label="Tipo">
              <select
                className="nm"
                data-testid="fonte-formato-tipo"
                value={form.formatoTipo}
                onChange={(e) => set("formatoTipo", e.target.value)}
              >
                <option value="csv">CSV</option>
                <option value="json">JSON</option>
                <option value="largura_fixa">Largura fixa</option>
                <option value="parquet">Parquet</option>
              </select>
            </Campo>
            {(form.formatoTipo === "csv" || form.formatoTipo === "largura_fixa") && (
              <Campo label="Encoding (obrigatório)">
                <input
                  className="nm"
                  data-testid="fonte-encoding"
                  value={form.encoding}
                  onChange={(e) => set("encoding", e.target.value)}
                  placeholder="latin-1"
                />
              </Campo>
            )}
            {form.formatoTipo === "csv" && (
              <Campo label="Delimitador">
                <input
                  className="nm"
                  value={form.delimitador}
                  onChange={(e) => set("delimitador", e.target.value)}
                  placeholder=";"
                />
              </Campo>
            )}
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "160px 1fr", gap: 12 }}>
            <Campo label="Compressão">
              <select
                className="nm"
                value={form.compressao}
                onChange={(e) => set("compressao", e.target.value)}
              >
                <option value="">Nenhuma</option>
                <option value="zip">ZIP</option>
                <option value="gzip">GZIP</option>
              </select>
            </Campo>
            {form.compressao === "zip" && (
              <Campo label="Arquivo dentro do ZIP (regex, obrigatório em zip-pacote)">
                <input
                  className="nm"
                  value={form.arquivoNoZip}
                  onChange={(e) => set("arquivoNoZip", e.target.value)}
                  placeholder="^inf_diario_fi_.*\.csv$"
                />
              </Campo>
            )}
          </div>
          {form.formatoTipo === "json" && (
            <Campo label="Caminho até a lista (opcional)">
              <input
                className="nm"
                value={form.jsonPath}
                onChange={(e) => set("jsonPath", e.target.value)}
                placeholder="a.b.c (vazio = o corpo)"
              />
            </Campo>
          )}
          {form.formatoTipo === "csv" && (
            <Campo label="Prefixo da linha (opcional — arquivo com duas tabelas coladas)">
              <input
                className="nm"
                value={form.prefixoLinha}
                onChange={(e) => set("prefixoLinha", e.target.value)}
                placeholder="1@"
              />
            </Campo>
          )}
          {form.formatoTipo === "largura_fixa" && (
            <>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                <Campo label="Pular linhas (cabeçalho/trailer)">
                  <input
                    className="nm"
                    inputMode="numeric"
                    value={form.pularLinhas}
                    onChange={(e) => set("pularLinhas", e.target.value)}
                  />
                </Campo>
                <Campo label="Linha do cabeçalho">
                  <input
                    className="nm"
                    inputMode="numeric"
                    value={form.linhaCabecalho}
                    onChange={(e) => set("linhaCabecalho", e.target.value)}
                  />
                </Campo>
              </div>
              <Campo label="Campos (JSON — [{nome, inicio, tamanho}], início 1-based)">
                <textarea
                  className="nm"
                  rows={4}
                  value={form.camposLarguraFixa}
                  onChange={(e) => set("camposLarguraFixa", e.target.value)}
                  placeholder={'[{"nome":"cnpj","inicio":1,"tamanho":14}]'}
                />
              </Campo>
            </>
          )}
        </div>

        <div className="card shelf" style={{ marginBottom: 14, padding: "13px 15px" }}>
          <Campo label="Colunas esperadas (opcional — uma por linha; coluna faltando manda o lote para a quarentena)">
            <textarea
              className="nm"
              rows={3}
              value={form.schemaEsperado}
              onChange={(e) => set("schemaEsperado", e.target.value)}
              placeholder={"CNPJ_FUNDO_CLASSE\nDT_COMPTC\nVL_QUOTA"}
            />
          </Campo>
        </div>

        <div className="card shelf" style={{ marginBottom: 14, padding: "13px 15px" }}>
          <button
            type="button"
            className="lk"
            data-testid="fonte-avancado-toggle"
            onClick={() => setAvancado((v) => !v)}
          >
            {avancado ? "▾" : "▸"} Avançado — grão e limites
          </button>
          {avancado && (
            <div style={{ marginTop: 12 }}>
              <p style={{ fontSize: 12, color: "var(--ink-2)" }}>
                O que é UMA LINHA desta tabela. Sem declarar, o grão fica <b>desconhecido</b> — estado
                explícito, não "está tudo bem".
              </p>
              <Campo label="Estado do grão">
                <select
                  className="nm"
                  data-testid="fonte-grao-estado"
                  value={form.graoEstado}
                  onChange={(e) => set("graoEstado", e.target.value)}
                >
                  <option value="desconhecido">Desconhecido (ninguém declarou ainda)</option>
                  <option value="declarado">Declarado (as chaves abaixo identificam a linha)</option>
                  <option value="sem_chave">Sem chave (evento repetível, sem identidade)</option>
                </select>
              </Campo>

              {form.graoEstado === "declarado" && (
                <>
                  <Campo label="Chaves (uma coluna normalizada por linha)">
                    <textarea
                      className="nm"
                      rows={2}
                      value={form.graoChaves}
                      onChange={(e) => set("graoChaves", e.target.value)}
                      placeholder={"codigo\ncompetencia"}
                    />
                  </Campo>
                  <p style={{ fontSize: 12, color: "var(--ink-2)" }}>
                    Desempate (opcional) — quando a chave é a identidade certa, mas a origem publica a
                    mesma linha mais de uma vez.
                  </p>
                  <Campo label="Colunas de desempate ('coluna:asc' ou 'coluna:desc', uma por linha)">
                    <textarea
                      className="nm"
                      rows={2}
                      value={form.graoDesempateColunas}
                      onChange={(e) => set("graoDesempateColunas", e.target.value)}
                      placeholder="data_entrega:desc"
                    />
                  </Campo>
                  {form.graoDesempateColunas.trim() && (
                    <Campo label="Motivo do desempate (obrigatório se houver colunas acima)">
                      <input
                        className="nm"
                        value={form.graoDesempateMotivo}
                        onChange={(e) => set("graoDesempateMotivo", e.target.value)}
                        placeholder="a origem reapresenta o mesmo registro; vale a última"
                      />
                    </Campo>
                  )}
                </>
              )}
              {form.graoEstado === "sem_chave" && (
                <Campo label="Motivo (obrigatório)">
                  <input
                    className="nm"
                    value={form.graoMotivo}
                    onChange={(e) => set("graoMotivo", e.target.value)}
                    placeholder="evento repetível, agregado sem identidade"
                  />
                </Campo>
              )}

              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12, marginTop: 14 }}>
                <Campo label="Timeout (segundos)">
                  <input
                    className="nm"
                    inputMode="numeric"
                    value={form.timeoutSegundos}
                    onChange={(e) => set("timeoutSegundos", e.target.value)}
                    placeholder="180"
                  />
                </Campo>
                <Campo label="Concorrência">
                  <input
                    className="nm"
                    inputMode="numeric"
                    value={form.concorrencia}
                    onChange={(e) => set("concorrencia", e.target.value)}
                    placeholder="4"
                  />
                </Campo>
                <Campo label="Tentativas">
                  <input
                    className="nm"
                    inputMode="numeric"
                    value={form.tentativas}
                    onChange={(e) => set("tentativas", e.target.value)}
                    placeholder="3"
                  />
                </Campo>
              </div>
            </div>
          )}
        </div>

        <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <button
            className="pag-btn"
            data-testid="fonte-validar"
            disabled={validando}
            onClick={() => void validar()}
          >
            {validando ? "Validando…" : "Validar contrato"}
          </button>
          <button
            className="btn-stamp"
            data-testid="fonte-salvar"
            disabled={salvando}
            onClick={() => setConfirmando(true)}
          >
            <Icon name="stamp" cls="ic" /> {salvando ? "Salvando…" : editando ? "Salvar alterações" : "Cadastrar fonte"}
          </button>
          <button className="pag-btn" onClick={onVoltar}>
            Voltar à lista
          </button>
        </div>

        {validacao && (
          <div
            className={validacao.ok ? "run-banner" : "exec-banner-falha"}
            role="status"
            data-testid="fonte-validacao"
            style={{ marginTop: 14 }}
          >
            <span className="dot" />
            <span>{validacao.texto}</span>
          </div>
        )}
      </div>
    </>
  );
}
