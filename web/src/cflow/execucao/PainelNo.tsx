import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import * as api from "../../api/client";
import type { AmostraNoExecucao } from "../../api/client";
import type { NoDoGrafo } from "./ExecGrafo";
import {
  celula,
  decorridoNo,
  fmtDuracaoMs,
  fmtMomento,
  num,
  perfisDaAmostra,
  pillNo,
  linhasTxt,
} from "./formato";

// Painel do nó selecionado, EMBAIXO do grafo e com a largura inteira da tela.
//
// Antes isto vivia numa coluna fixa de 326px: a amostra do snapshot saía com scroll horizontal
// numa faixa estreita, o que tornava conferir o dado que rodou praticamente inviável. Aqui a
// tabela ocupa a largura da página, o cabeçalho fica fixo ao rolar e o total de linhas do
// snapshot aparece junto do seletor de quantas linhas trazer.

const LIMITES = [50, 100, 200];

type Aba = "dados" | "resumo" | "sql" | "erro";

// RESUMO derivado do registro da execução (status/linhas/duração/erro) — NÃO é o log do motor: a
// API não persiste linhas de log. O rótulo da aba diz "Resumo" e a primeira linha do bloco declara
// a origem, porque numa plataforma cuja trilha é evidência nada pode se passar por log que não é.
function logsDe(n: NoDoGrafo): Array<[string, string]> {
  if (n.estado === "ok") {
    const ls: Array<[string, string]> = [
      ["▸ executando " + n.tipo + "…", "lmut"],
      ["✓ " + linhasTxt(n.linhas), "lok"],
    ];
    // Só afirma o snapshot quando ele EXISTE: a linha era incondicional e contradizia a aba
    // Dados do mesmo nó, que dizia "não gera dado consultável". Duas abas, dois fatos opostos.
    if (n.snapshot) {
      ls.push(["✓ snapshot Parquet gravado", "lok"]);
      ls.push(["  " + n.snapshot, "lmut"]);
    }
    return ls;
  }
  if (n.estado === "reap") {
    return [
      ["▸ conferindo entrada (hash)…", "lmut"],
      ["= entrada idêntica à última execução", "lmut"],
      // A duração real vem do registro; "0 ms" era um número fixo escrito na tela.
      [
        "✓ snapshot reaproveitado" +
          (n.duracao_ms != null ? " · " + fmtDuracaoMs(n.duracao_ms) : "") +
          " · " + linhasTxt(n.linhas),
        "lok",
      ],
    ];
  }
  if (n.estado === "falha") {
    return [
      ["▸ lendo entrada…", "lmut"],
      ["▸ aplicando " + n.tipo + "…", "lmut"],
      ["✗ " + (n.erro ?? "Falha ao executar o nó."), "lerr"],
      ["  execução abortada", "lmut"],
    ];
  }
  if (n.estado === "executando") {
    const d = decorridoNo(n);
    return [
      ["▸ executando " + n.tipo + "…", "lmut"],
      ["  começou " + (n.iniciado_em ? fmtMomento(n.iniciado_em) : "agora há pouco"), "lmut"],
      // O tempo decorrido é o dado que responde "travou?" — e não pode ser confundido com uma
      // duração final: por isso "há", não um número solto na mesma forma dos nós concluídos.
      ...(d != null ? ([["  rodando há " + fmtDuracaoMs(d), "lmut"]] as Array<[string, string]>) : []),
      ["  o dado aparece nesta aba quando o nó terminar", "lmut"],
    ];
  }
  if (n.estado === "pendente") {
    return [["— ainda não executado (execução em andamento)", "lmut"]];
  }
  if (n.estado === "indefinido") {
    return [
      ["▸ executando " + n.tipo + "…", "lmut"],
      ["? o nó começou e não registrou o fim", "lerr"],
      ["  a execução terminou com este nó ainda marcado como em andamento", "lmut"],
      ["  se ele grava fora da plataforma, o efeito pode ter acontecido — confira no destino", "lmut"],
    ];
  }
  return [
    ["— não executado", "lmut"],
    ["  o pipeline parou num nó anterior", "lmut"],
  ];
}

export default function PainelNo({
  execucaoId,
  no,
  onLocalizar,
  onReexecutar,
}: {
  execucaoId: string;
  no: NoDoGrafo | null;
  // Refazer a partir DESTE nó. Ausente = a execução ainda roda (refazer no meio de uma execução em
  // curso deixaria duas rodadas escrevendo o mesmo trecho).
  onReexecutar?: (noId: string) => void;
  // Revela o nó selecionado no grafo/na lista. Existe porque a abertura respeita o enquadramento
  // (e a lista abre nas fontes): num pipeline grande, o painel mostra um nó que pode não estar na
  // parte visível, e o nome no cabeçalho era a única pista de qual era.
  onLocalizar?: () => void;
}) {
  const [limite, setLimite] = useState(LIMITES[0]);
  const [dados, setDados] = useState<AmostraNoExecucao | null>(null);
  const [carregando, setCarregando] = useState(false);
  const [erroCarga, setErroCarga] = useState<string | null>(null);
  const [aba, setAba] = useState<Aba>("dados");

  const noId = no?.id ?? null;
  const falhou = no?.estado === "falha";

  // Nó que falhou abre já no erro — é o que a pessoa veio ver. Nos outros casos, MANTÉM a aba
  // escolhida ao trocar de nó (quem está comparando SQL entre etapas não quer voltar pra Dados a
  // cada clique); só volta pra Dados se a aba atual não existir no nó novo.
  useEffect(() => {
    setAba((atual) => {
      if (falhou) return "erro";
      if (atual === "erro") return "dados";
      return atual;
    });
  }, [noId, falhou]);

  // Só busca de nó que EXECUTOU (nó pendente/não executado não tem registro → 404 à toa).
  useEffect(() => {
    setDados(null);
    setErroCarga(null);
    if (!noId || !no?.executou) {
      // Desliga o "carregando" herdado do nó anterior: sem isto, trocar de um nó que estava
      // carregando para um nó que não executou deixava "Carregando dados do nó…" para sempre,
      // e a mensagem correta ("ainda não rodou") nunca aparecia.
      setCarregando(false);
      return;
    }
    let vivo = true;
    setCarregando(true);
    api
      .previewNoExecucao(execucaoId, noId, limite)
      .then((d) => vivo && setDados(d))
      .catch((e) => vivo && setErroCarga(e?.message ?? "Falha ao carregar os dados do nó."))
      .finally(() => vivo && setCarregando(false));
    return () => {
      vivo = false;
    };
  }, [execucaoId, noId, no?.executou, limite]);

  const linhas = useMemo(() => dados?.amostra ?? [], [dados]);
  const perfis = useMemo(
    () => perfisDaAmostra(dados?.colunas ?? [], linhas),
    [dados?.colunas, linhas],
  );

  // Copiar a amostra visível como TSV — é o gesto que fecha a conferência contra a planilha/o
  // sistema de origem; hoje só existia selecionando a tabela com o mouse.
  const [copiado, setCopiado] = useState(false);
  const timerCopia = useRef<number | null>(null);
  useEffect(() => () => {
    if (timerCopia.current) window.clearTimeout(timerCopia.current);
  }, []);
  const copiar = useCallback(async () => {
    if (!dados) return;
    const cabecalho = dados.colunas.join("\t");
    const corpo = linhas
            .map((l) =>
        dados.colunas
          .map((c) => {
            // TSV leva o valor BRUTO, não o texto da tela: o formatado arredonda float e usa
            // separador pt-BR, que na planilha de conferência vira texto ou esconde diferença.
            // Nulo/vazio saem como célula vazia; tab/quebra viram espaço para não rasgar a linha.
            const bruto = l[c];
            if (bruto === null || bruto === undefined) return "";
            return String(bruto).replace(/[\t\r\n]+/g, " ");
          })
          .join("\t"),
      )
      .join("\n");
    try {
      await navigator.clipboard.writeText(`${cabecalho}\n${corpo}`);
      setCopiado(true);
      if (timerCopia.current) window.clearTimeout(timerCopia.current);
      timerCopia.current = window.setTimeout(() => setCopiado(false), 1800);
    } catch {
      /* sem permissão de área de transferência — o usuário ainda pode selecionar a tabela */
    }
  }, [dados, linhas, perfis]);

  // Abas disponíveis no nó atual (Erro só existe quando houve erro) + navegação por seta.
  const abasVisiveis: { chave: Aba; rotulo: string; testid: string }[] = [
    { chave: "dados", rotulo: "Dados", testid: "exec-aba-dados" },
    { chave: "resumo", rotulo: "Resumo", testid: "exec-aba-log" },
    { chave: "sql", rotulo: "SQL", testid: "exec-aba-sql" },
    ...(falhou || no?.erro
      ? [{ chave: "erro" as Aba, rotulo: "Erro", testid: "exec-aba-erro" }]
      : []),
  ];
  const aoNavegarAbas = (ev: React.KeyboardEvent) => {
    const passo = ev.key === "ArrowRight" ? 1 : ev.key === "ArrowLeft" ? -1 : 0;
    if (!passo) return;
    ev.preventDefault();
    const i = abasVisiveis.findIndex((a) => a.chave === aba);
    const proxima = abasVisiveis[(i + passo + abasVisiveis.length) % abasVisiveis.length];
    setAba(proxima.chave);
    document.getElementById(`exec-aba-${proxima.chave}`)?.focus();
  };

  if (!no) {
    return (
      <div className="card exec-painel">
        {/* "no grafo" estava errado com a visão em lista ativa — que é o default de pipeline longo. */}
        <div className="exec-painel-vazio">Selecione um nó para ver o dado que rodou.</div>
      </div>
    );
  }

  const pill = pillNo(no.estado);
  const temColunas = (dados?.colunas?.length ?? 0) > 0;

  return (
    <div className="card exec-painel" data-testid="exec-painel-no">
      <div className="card-h exec-painel-h">
        <h3 data-testid="exec-painel-nome">{no.nome}</h3>
        {onLocalizar && (
          <button
            type="button"
            className="lk exec-localizar"
            data-testid="exec-localizar-no"
            title="Rolar até este nó no grafo ou na lista"
            onClick={onLocalizar}
          >
            localizar
          </button>
        )}
        {/* Vive aqui, e não no card do grafo: o card é compacto de propósito e o grafo é
            read-only. Selecionar o nó já abre este painel, então a ação está a um clique de onde
            a pessoa decide — inclusive no nó que falhou. */}
        {onReexecutar && (
          <button
            type="button"
            className="lk exec-reexec-no"
            data-testid="exec-reexecutar-no"
            title="Cria uma execução nova: refaz este nó e os que dependem dele; os anteriores vêm prontos desta"
            onClick={() => onReexecutar(no.id)}
          >
            executar a partir daqui
          </button>
        )}
        <span className={"statuspill " + pill.cls}>
          <span className="d" />
          {pill.label}
        </span>
        <span className="exec-painel-meta">
          <span>{no.tipo}</span>
          {/* Nó que falhou não tem linhas nem, às vezes, duração — mostrar "— linhas" seria ruído. */}
          {no.linhas != null && <span>{linhasTxt(no.linhas)}</span>}
          {no.duracao_ms != null && <span>{fmtDuracaoMs(no.duracao_ms)}</span>}
          {decorridoNo(no) != null && <span>há {fmtDuracaoMs(decorridoNo(no))}</span>}
        </span>
        {/* Abas de verdade: um tab-stop só, navegação por seta e o painel ligado à aba ativa. */}
        <div
          className="cfg-tabs exec-painel-abas"
          data-testid="exec-abas"
          role="tablist"
          aria-label="Dado, resumo, SQL e erro do nó"
          onKeyDown={aoNavegarAbas}
        >
          {abasVisiveis.map((a) => (
            <button
              key={a.chave}
              type="button"
              role="tab"
              id={`exec-aba-${a.chave}`}
              aria-selected={aba === a.chave}
              aria-controls="exec-painel-corpo"
              tabIndex={aba === a.chave ? 0 : -1}
              data-testid={a.testid}
              className={aba === a.chave ? "on" : ""}
              onClick={() => setAba(a.chave)}
            >
              {a.rotulo}
            </button>
          ))}
        </div>
      </div>

      <div
        className="exec-painel-corpo"
        id="exec-painel-corpo"
        role="tabpanel"
        aria-labelledby={`exec-aba-${aba}`}
        tabIndex={0}
      >
        {aba === "dados" && (
          <>
            {carregando && <div className="exec-painel-vazio">Carregando dados do nó…</div>}
            {erroCarga && <div className="exec-painel-erro-carga">{erroCarga}</div>}
            {!carregando && !erroCarga && !no.executou && (
              <div className="exec-painel-vazio">
                {no.estado === "pendente"
                  ? "Este nó ainda não rodou nesta execução."
                  : "Este nó não rodou nesta execução — o pipeline parou antes."}
              </div>
            )}
            {/* Nó em andamento TEM registro (`executou`), mas ainda não tem snapshot: sem este
                caso ele caía em "Snapshot indisponível (arquivo removido ou expirado)" — a tela
                acusando perda de dado quando o nó apenas não terminou. */}
            {!carregando && !erroCarga && no.executou && !temColunas && no.estado === "executando" && (
              <div className="exec-painel-vazio">
                Este nó está rodando agora
                {decorridoNo(no) != null ? ` (há ${fmtDuracaoMs(decorridoNo(no))})` : ""}. O dado
                aparece aqui quando ele terminar.
              </div>
            )}
            {!carregando && !erroCarga && no.executou && !temColunas && no.estado !== "executando" && (
              <div className="exec-painel-vazio">
                {/* "Rodou e não trouxe linha" é um ACHADO da conferência, não uma falha de leitura
                    do snapshot: as duas coisas caíam na mesma mensagem genérica. */}
                {/* A ordem importa: afirmar "o snapshot existe e está vazio" para um nó com
                    registro de 0 linhas E arquivo ausente é uma asserção que esta tela não pode
                    sustentar. Só afirma o arquivo quando ele consta. */}
                {!no.snapshot
                  ? "Este nó não gera dado consultável (nó de efeito ou sem snapshot)."
                  : (no.linhas ?? null) === 0
                    ? "O nó rodou e não produziu nenhuma linha. O snapshot existe e está vazio — não é erro de leitura."
                    : "Snapshot indisponível (arquivo removido ou expirado)."}
              </div>
            )}
            {temColunas && (
              <>
                {/* Caixa rolável precisa ser focável, senão o conteúdo é inalcançável por teclado. */}
                <div
                  className="exec-grade"
                  tabIndex={0}
                  role="region"
                  aria-label={`Amostra do dado do nó ${no.nome}`}
                >
                  <table className="obs exec-obs" data-testid="exec-no-preview">
                    <thead>
                      <tr>
                        {dados!.colunas.map((c) => (
                          <th scope="col" key={c} className={perfis[c]?.tipo === "numero" ? "num" : undefined}>
                            {c}
                          </th>
                        ))}
                        {/* Com 1–3 colunas, a tabela de 100% jogava o valor a ~1000px do rótulo. */}
                        {dados!.colunas.length <= 3 && <th className="espaco" aria-hidden="true" />}
                      </tr>
                    </thead>
                    <tbody>
                      {linhas.length === 0 && (
                        <tr>
                          <td
                            className="exec-grade-vazia"
                            colSpan={dados!.colunas.length + (dados!.colunas.length <= 3 ? 1 : 0)}
                          >
                            O nó rodou e não produziu nenhuma linha — o snapshot tem estas colunas e
                            está vazio. Não é erro de leitura.
                          </td>
                        </tr>
                      )}
                      {linhas.map((l, ri) => (
                        <tr key={ri}>
                          {dados!.colunas.map((c) => {
                            const cel = celula(l[c], perfis[c]);
                            return (
                              <td
                                key={c}
                                title={cel.titulo ?? (cel.texto !== String(l[c]) ? String(l[c]) : undefined)}
                                className={
                                  (perfis[c]?.tipo === "numero" ? "num" : "") +
                                  (cel.vazio ? " cel-vazia" : "")
                                }
                              >
                                {cel.texto}
                              </td>
                            );
                          })}
                          {dados!.colunas.length <= 3 && <td className="espaco" />}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="exec-grade-pe">
                  <span data-testid="exec-grade-contagem">
                    {typeof dados!.contagem === "number"
                      ? `${num(linhas.length)} de ${linhasTxt(dados!.contagem)}`
                      : linhasTxt(linhas.length)}
                  </span>
                  <span className="exec-grade-acoes">
                    <button type="button" className="pag-btn" data-testid="exec-copiar" onClick={() => void copiar()}>
                      {copiado ? "copiado ✓" : "Copiar"}
                    </button>
                    <label className="exec-grade-limite">
                      mostrar
                      <select
                        data-testid="exec-grade-limite"
                        value={limite}
                        onChange={(e) => setLimite(Number(e.target.value))}
                      >
                        {LIMITES.map((n) => (
                          <option key={n} value={n}>
                            {n}
                          </option>
                        ))}
                      </select>
                    </label>
                  </span>
                </div>
              </>
            )}
          </>
        )}

        {aba === "resumo" && (
          <div className="logbox" data-testid="exec-no-log">
            <div className="lmut"># resumo derivado do registro da execução — não é o log do motor</div>
            {logsDe(no).map((l, i) => (
              <div key={i} className={l[1]}>
                {l[0]}
              </div>
            ))}
          </div>
        )}

        {aba === "sql" && (
          <pre className="sql" data-testid="exec-no-sql">
            {no.sql?.trim() ||
              "Este nó não tem SQL próprio (fonte, operação guiada ou nó de efeito). Abra o pipeline no Studio para ver a configuração."}
          </pre>
        )}

        {aba === "erro" && (
          <pre className="exec-erro-pre" data-testid="exec-no-erro">
            {no.erro ?? dados?.erro ?? "Sem detalhe de erro para este nó."}
          </pre>
        )}
      </div>
    </div>
  );
}
