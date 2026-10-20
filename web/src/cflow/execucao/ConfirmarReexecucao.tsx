import { useEffect, useState } from "react";

import * as api from "../../api/client";
import { ConfirmarAcao } from "../ui";
import { rotuloEfeito, rotuloPublicacao } from "./formato";

// Confirmação de "executar de novo" — UM componente para os três pontos de entrada (lista, detalhe
// da execução e painel do nó). Escrito três vezes, o diálogo divergiria no dia em que o aviso de
// efeito mudar, e é justamente esse aviso que não pode faltar em lugar nenhum. Mesma razão de
// `MenuAcoes` e `AbasFiltro` terem sido unificados.
//
// É o `ConfirmarAcao` do design system, não um modal próprio: Escape, ciclo de Tab, foco de volta
// no gatilho e Enter para submeter vêm de graça, e um diálogo paralelo teria de reimplementar tudo.

export default function ConfirmarReexecucao({
  execucaoId,
  desdeNo,
  onFeito,
  onCancelar,
}: {
  execucaoId: string;
  // Nó de partida: refaz ele e o que depende dele. Ausente = a execução inteira.
  desdeNo?: string | null;
  onFeito: (novaExecucaoId: string) => void;
  onCancelar: () => void;
}) {
  const [plano, setPlano] = useState<api.PlanoReexecucao | null>(null);
  const [erro, setErro] = useState<string | null>(null);
  const [enviando, setEnviando] = useState(false);
  // Um valor por parâmetro de credencial. O persistido é a marca de redação (`***`), então não há
  // o que reusar: ou a pessoa informa de novo, ou a reexecução é recusada pelo backend.
  const [segredos, setSegredos] = useState<Record<string, string>>({});

  useEffect(() => {
    let vivo = true;
    setErro(null);
    api
      .planoReexecucao(execucaoId, desdeNo)
      .then((p) => vivo && setPlano(p))
      .catch((e) => vivo && setErro(e?.message ?? "Falha ao montar o plano de reexecução."));
    return () => {
      vivo = false;
    };
  }, [execucaoId, desdeNo]);

  // `textoPrincipal` é o campo do próprio modal (o primeiro segredo). Vem no submit, então não há
  // por que espelhá-lo em estado local — espelhar durante o render era um efeito colateral à
  // espera de um laço.
  const confirmar = async (textoPrincipal: string) => {
    if (!plano || enviando) return;
    const valores = { ...segredos };
    if (plano.parametros_segredo[0] !== undefined) {
      valores[plano.parametros_segredo[0]] = textoPrincipal;
    }
    const faltando = plano.parametros_segredo.filter((n) => !(valores[n] ?? "").trim());
    if (faltando.length) {
      setErro(
        `Informe o valor de: ${faltando.join(", ")}. O valor não fica guardado, então precisa ser digitado de novo a cada reexecução.`,
      );
      return;
    }
    setEnviando(true);
    setErro(null);
    try {
      const r = await api.reexecutar(execucaoId, { desdeNo, parametros: valores });
      onFeito(r.id);
    } catch (e: any) {
      setErro(e?.message ?? "Falha ao reexecutar.");
      setEnviando(false);
    }
  };

  // Enquanto o plano não chega, o modal já existe (a pessoa clicou e precisa de resposta), mas sem
  // prometer nada sobre o que vai rodar.
  if (!plano) {
    return (
      <ConfirmarAcao
        titulo={desdeNo ? "Executar a partir deste nó" : "Executar de novo"}
        corpo={erro ?? "Verificando o que será refeito…"}
        rotuloConfirmar="Executar"
        erro={erro}
        onConfirmar={() => {}}
        onCancelar={onCancelar}
      />
    );
  }

  const naoIdempotentes = plano.efeitos.filter((e) => !e.idempotente);
  const segredoPrincipal = plano.parametros_segredo[0];
  const outrosSegredos = plano.parametros_segredo.slice(1);

  // Contagens em português de gente: "os 1 nó(s)" e "Os 0 anteriores" são o tipo de detalhe que
  // faz a tela parecer um dump de variável.
  const dependentes = plano.nos_a_executar.length - 1;
  const reaproveitados = plano.nos_reaproveitados.length;
  const frase = (n: number, um: string, muitos: string) => (n === 1 ? um : `${n} ${muitos}`);

  const corpo = (
    <>
      <span style={{ display: "block", marginBottom: 8 }}>
        {desdeNo ? (
          <>
            Refaz <b>{desdeNo}</b>
            {dependentes > 0 && <> e {frase(dependentes, "o nó", "nós")} que dependem dele</>}.
            {reaproveitados > 0 && (
              <>
                {" "}
                {frase(reaproveitados, "A etapa anterior vem pronta", "etapas anteriores vêm prontas")}{" "}
                desta execução, sem rodar de novo.
              </>
            )}
          </>
        ) : (
          <>
            Roda o pipeline <b>{plano.pipeline_nome}</b> de novo, na versão <b>v{plano.versao}</b> e
            com os mesmos parâmetros. As etapas que não mudaram são reaproveitadas.
          </>
        )}
      </span>
      <span style={{ display: "block", marginBottom: 8 }}>
        Isto cria uma <b>execução nova</b>. Esta continua no histórico, do jeito que está.
      </span>
      {plano.efeitos.length > 0 && (
        // O aviso é a única proteção que existe: a trava de efeito é por execução, e numa execução
        // NOVA ela não vale — o e-mail sai de novo, o POST é refeito, o append duplica.
        <span className="exec-erro" style={{ display: "block" }} data-testid="reexec-aviso-efeito">
          <b>Atenção:</b> este trecho tem efeito fora da plataforma e ele acontece de novo:
          <ul style={{ margin: "6px 0 0 18px" }}>
            {plano.efeitos.map((e) => (
              <li key={e.no_id}>
                <b>{e.no_id}</b> — {rotuloEfeito(e.tipo)}
                {e.idempotente ? " (repetir não duplica)" : " (repetir DUPLICA)"}
              </li>
            ))}
          </ul>
          {naoIdempotentes.length > 0 && (
            <span style={{ display: "block", marginTop: 6 }}>
              Não há como desfazer o que sair daqui.
            </span>
          )}
        </span>
      )}
      {plano.sem_publicar ? (
        // Montante de cadeia marcada: recalcula e grava o snapshot, mas não move ponteiro.
        <span style={{ display: "block", marginTop: 8 }} data-testid="reexec-sem-publicar">
          <b>Não publica:</b> {plano.sem_publicar_motivo}
        </span>
      ) : (
        (plano.publicacoes?.length ?? 0) > 0 && (
          // Republicar não duplica — é outra versão imutável —, mas move o ponteiro que os
          // consumidores leem. Quem clica precisa ver QUAL, não um "há publicações".
          <span style={{ display: "block", marginTop: 8 }} data-testid="reexec-publicacoes">
            Publica de novo, movendo o ponteiro que os consumidores leem:
            <ul style={{ margin: "6px 0 0 18px" }}>
              {plano.publicacoes!.map((p) => (
                <li key={p.no_id}>
                  <b>{p.no_id}</b> — {rotuloPublicacao(p)}
                </li>
              ))}
            </ul>
          </span>
        )
      )}
    </>
  );

  return (
    <ConfirmarAcao
      titulo={desdeNo ? "Executar a partir deste nó" : "Executar de novo"}
      corpo={corpo}
      rotuloConfirmar={
        enviando ? "Executando…" : desdeNo ? "Executar a partir daqui" : "Executar de novo"
      }
      perigo={naoIdempotentes.length > 0}
      // O campo do modal serve ao PRIMEIRO segredo; os demais vêm no `extra`, que renderiza junto.
      // Pipeline com credencial em parâmetro quase sempre tem uma só — e o caminho de N continua
      // coberto em vez de recusado.
      valorInicial={segredoPrincipal !== undefined ? "" : undefined}
      rotuloCampo={segredoPrincipal !== undefined ? segredoPrincipal : undefined}
      campoSegredo
      extra={
        segredoPrincipal !== undefined
          ? () => (
              <>
                  {outrosSegredos.map((n) => (
                    <span key={n} style={{ display: "block", marginTop: 10 }}>
                      {n}
                      <input
                        className="nm"
                        type="password"
                        autoComplete="off"
                        value={segredos[n] ?? ""}
                        onChange={(e) => setSegredos((s) => ({ ...s, [n]: e.target.value }))}
                        data-testid={`reexec-segredo-${n}`}
                      />
                    </span>
                  ))}
                <span className="hint" style={{ display: "block", marginTop: 6 }}>
                  O valor não fica guardado na execução — por isso precisa ser informado de novo.
                </span>
              </>
            )
          : undefined
      }
      erro={erro}
      onConfirmar={(texto) => void confirmar(texto)}
      onCancelar={onCancelar}
    />
  );
}
