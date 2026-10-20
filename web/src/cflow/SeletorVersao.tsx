import { useEffect, useRef, useState } from "react";

import type { VersaoResumo } from "../api/client";
import { Icon } from "./ui";

// Dropdown rico de versões no cabeçalho. Cada linha: nº, status, ativa, última, quando foi criada
// e (se houver) execuções no Portal. Selecionar uma CARREGA-a (ver); publicar/salvar a torna ativa.
// A última = `null` (volta à versão corrente). Reutilizado por todas as entidades versionadas.
export function SeletorVersao({
  versoes,
  ultima,
  sel,
  ocupado,
  onSelecionar,
  erro,
}: {
  versoes: VersaoResumo[];
  ultima: number | null;
  sel: number | null;
  ocupado: boolean;
  onSelecionar: (numero: number | null) => void;
  // A lista não pôde ser carregada. O controle CONTINUA na tela — desabilitado, mostrando a versão
  // atual e dizendo o motivo. Sumir com ele fazia parecer que o produto não tem seleção de versão,
  // que é a pior forma possível de comunicar uma falha de carga.
  erro?: string | null;
}) {
  const [aberto, setAberto] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const gatilhoRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!aberto) return;
    const fora = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setAberto(false);
    };
    // Escape fecha e devolve o foco ao gatilho: sem isto o teclado ficava preso na lista aberta.
    const tecla = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      setAberto(false);
      gatilhoRef.current?.focus();
    };
    document.addEventListener("mousedown", fora);
    document.addEventListener("keydown", tecla);
    return () => {
      document.removeEventListener("mousedown", fora);
      document.removeEventListener("keydown", tecla);
    };
  }, [aberto]);

  const fmt = (iso: string | null) => {
    if (!iso) return "—";
    try {
      return new Date(iso).toLocaleString("pt-BR", { timeZone: "America/Sao_Paulo", dateStyle: "short", timeStyle: "short" });
    } catch {
      return iso;
    }
  };
  const falhou = !!erro && versoes.length === 0;
  const atualNum = sel ?? ultima;
  const atual = versoes.find((v) => v.numero === atualNum);
  const rotuloStatus = (s: string) => (s === "publicada" ? "publicada" : s === "arquivada" ? "arquivada" : "rascunho");

  return (
    <div className="ver-dd" ref={ref}>
      <button
        ref={gatilhoRef}
        type="button"
        className="ver-dd-trigger"
        data-testid="versao-select"
        disabled={ocupado || falhou}
        aria-haspopup="listbox"
        aria-expanded={aberto}
        onClick={() => setAberto((a) => !a)}
        title={
          falhou
            ? `Não foi possível carregar as versões: ${erro}`
            : "Selecione uma versão para ver; Publicar a torna ativa"
        }
      >
        <span>
          {/* `atualNum` como fallback: enquanto a lista não chega (ou se ela falhou), a versão
              corrente ainda é conhecida pelo Redux. Mostrar "v—" ao lado de um canvas que diz
              "versão atual: v102" seria a tela se contradizendo. */}
          Versão <b>v{atual?.numero ?? atualNum ?? "—"}</b>
        </span>
        {atual?.ativa && <span className="ver-badge ativa">ativa</span>}
        <Icon name="chevron" cls="ic" />
      </button>
      {falhou && (
        <span className="ver-dd-erro" role="alert" data-testid="versao-erro">
          versões indisponíveis
        </span>
      )}
      {aberto && !falhou && (
        <div className="ver-dd-pop nowheel" data-testid="versao-lista" role="listbox" aria-label="Versões">
          {versoes.map((v) => (
            <button
              key={v.numero}
              type="button"
              role="option"
              aria-selected={atualNum === v.numero}
              className={`ver-row${atualNum === v.numero ? " on" : ""}`}
              data-testid={`versao-opt-${v.numero}`}
              onClick={() => {
                onSelecionar(v.numero === ultima ? null : v.numero);
                setAberto(false);
              }}
            >
              <span className="ver-row-topo">
                <span className="ver-row-num">v{v.numero}</span>
                <span className={`ver-badge st-${rotuloStatus(v.status)}`}>{rotuloStatus(v.status)}</span>
                {v.ativa && <span className="ver-badge ativa">ativa</span>}
                {v.numero === ultima && <span className="ver-badge ultima">última</span>}
                <span className="ver-row-data">{fmt(v.criado_em)}</span>
                {v.exec_portal != null && (
                  <span className="ver-row-exec" title="Execuções pelo Portal (operador)">
                    {v.exec_portal} no portal
                  </span>
                )}
              </span>
              {/* A nota de mudança já vinha da API e era descartada: a lista mostrava dez "v N ·
                  publicada · data" indistinguíveis, e escolher a versão certa exigia adivinhar. */}
              {v.nota_mudanca && <span className="ver-row-nota">{v.nota_mudanca}</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
