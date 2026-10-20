// Resumo das FONTES DE INGESTÃO na aba Bronze da listagem de pipelines.
//
// Até aqui este arquivo era o bloco INTEIRO (grade + quarentena + executar). Virou card-resumo com
// link porque a fonte ganhou tela própria (`FontesIngestaoTela.tsx`, rota `/ingestao`) — cadastro,
// edição de contrato, backfill e replay saíram do "fora do Studio, admin-only" para lá, revertendo a
// decisão anterior. O que fica AQUI é só o atalho de contexto: quem já está
// olhando "o que alimenta esta camada" não deveria perder isso de vista, mas a grade de 54 linhas +
// o painel de quarentena não cabem (nem fazem sentido) dentro da listagem de pipelines.
//
// Fonte de ingestão continua NÃO sendo pipeline: sem grafo, sem versão. `ItemCadastro.id` alimenta
// `onAbrir`/`onExcluir`/`onRenomear`/`onSistema` da grade dos pipelines — misturar as duas coisas
// aqui exigiria prefixar id e despachar por prefixo em cinco handlers, e é exatamente o que a tela
// própria evita ao viver numa rota separada.
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import * as api from "../../api/client";
import { num } from "../execucao/formato";
import { contarFalhas } from "./ingestao";

export function FontesIngestao() {
  const [fontes, setFontes] = useState<api.FonteLake[]>([]);
  const [carregando, setCarregando] = useState(true);
  const [erro, setErro] = useState<api.ErroApi | null>(null);

  useEffect(() => {
    let vivo = true;
    api
      .listarFontesIngestao()
      .then((f) => vivo && setFontes(f))
      .catch((e) => vivo && setErro(e as api.ErroApi))
      .finally(() => vivo && setCarregando(false));
    return () => {
      vivo = false;
    };
  }, []);

  const total = fontes.length;
  const falhas = contarFalhas(fontes);
  const inativas = fontes.filter((f) => f.ativo === false).length;

  return (
    <section className="pad ing-bloco" aria-labelledby="ing-tit" data-testid="bronze-fontes">
      <h2 id="ing-tit" className="ing-tit">
        Fontes de ingestão
        <span className="ing-selo">não são pipelines</span>
      </h2>
      <p className="ing-sub">
        Trazem dado público de fora (CVM, BCB, B3, ANBIMA) para o lake, na camada bronze — é delas
        que o nó <b>fonte lake</b> lê. Cadastro, contrato, backfill e replay têm tela própria.
      </p>

      <div className="lc-bar">
        <span data-testid="ing-contagem">
          {carregando
            ? "carregando fontes de ingestão…"
            : erro && total === 0
              ? "não foi possível carregar as fontes de ingestão"
              : `${num(total)} ${total === 1 ? "fonte de ingestão" : "fontes de ingestão"}`}
          {falhas > 0 && ` · ${num(falhas)} com falha na última execução`}
          {inativas > 0 && ` · ${num(inativas)} desativada${inativas === 1 ? "" : "s"}`}
        </span>
        <Link to="/ingestao" className="pag-btn" style={{ marginLeft: "auto" }} data-testid="ing-ver-tela">
          Ver fontes de ingestão →
        </Link>
      </div>

      {erro && total === 0 && (
        <div className="ing-aviso erro" role="alert" data-testid="ing-erro">
          {erro.message}
        </div>
      )}
    </section>
  );
}
