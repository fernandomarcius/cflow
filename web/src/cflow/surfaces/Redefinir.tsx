import { useEffect, useRef, useState } from "react";

import * as api from "../../api/client";
import { rota } from "../../prefixo";

const SENHA_MINIMO = 12;

// ─────────────────────────────────────────────────────────────────────────
// Definir senha a partir do token enviado pelo canal de notificação (1º acesso / recuperação).
// Pré-login: lê `?token=` da URL e chama /auth/recuperacao/confirmar.
//
// Link sem token é dito NA ABERTURA, não depois de a pessoa digitar duas senhas: o formulário só
// aparece quando há token para gastar. O token nunca é exibido na tela.
// ─────────────────────────────────────────────────────────────────────────
export default function Redefinir() {
  // O token é lido UMA vez e sai da URL na montagem. Limpar só no sucesso deixava a credencial na
  // barra de endereço e no histórico de quem abandona a tela, fecha a aba ou erra a confirmação.
  const tokenRef = useRef(new URLSearchParams(window.location.search).get("token") || "");
  const token = tokenRef.current;
  useEffect(() => {
    if (token) window.history.replaceState(null, "", window.location.pathname);
  }, [token]);
  const [senha, setSenha] = useState("");
  const [senha2, setSenha2] = useState("");
  const [mostrar, setMostrar] = useState(false);
  const [erro, setErro] = useState("");
  const [ok, setOk] = useState(false);
  const [carregando, setCarregando] = useState(false);

  const tamanhoOk = senha.length >= SENHA_MINIMO;
  const confereOk = senha.length > 0 && senha === senha2;

  const definir = async () => {
    setErro("");
    if (!tamanhoOk) {
      setErro(`A senha deve ter no mínimo ${SENHA_MINIMO} caracteres.`);
      return;
    }
    if (!confereOk) {
      setErro("As senhas não conferem.");
      return;
    }
    setCarregando(true);
    try {
      await api.confirmarRecuperacao(token, senha);
      setOk(true);
      setSenha("");
      setSenha2("");
    } catch (e: any) {
      setErro(e.message ?? "Não foi possível definir a senha.");
    } finally {
      setCarregando(false);
    }
  };

  return (
    <div className="login-screen" data-testid="redefinir">
      {/* painel da marca */}
      <div className="login-brand-panel">
        <div className="login-brand-top">
          <span className="login-brand-icon">
            <img src={rota("/favicon.svg")} alt="" />
          </span>
          <span className="login-brand-name">
            c<b>Flow</b>
          </span>
        </div>
        <div className="login-brand-mid">
          <div className="login-eyebrow">Plataforma de Dados</div>
          <h1 className="login-headline">Defina sua senha de acesso.</h1>
          <p className="login-lead">
            Crie uma senha forte (mínimo {SENHA_MINIMO} caracteres) para entrar na Plataforma de
            Dados cFlow.
          </p>
        </div>
        <div className="login-brand-foot">© cFlow · Plataforma de Dados</div>
      </div>

      {/* painel do formulário */}
      <div className="login-form-panel">
        <div className="login-form">
          {!token ? (
            <>
              <div className="login-eyebrow login-eyebrow--ink">Link inválido</div>
              <h2 className="login-title">Este link não serve</h2>
              <p className="login-desc">
                O endereço veio sem o código de acesso — ou o link foi cortado no caminho. Peça um
                novo na tela de login: o mesmo botão vale para primeiro acesso e para senha
                esquecida.
              </p>
              <a className="login-btn lg-btn-a" href={rota("/")}>
                Ir para o login →
              </a>
            </>
          ) : ok ? (
            <>
              <div className="login-eyebrow login-eyebrow--ink">Tudo certo</div>
              <h2 className="login-title">Senha definida</h2>
              <p className="login-desc">
                Sua senha foi cadastrada. Agora é só entrar com seu e-mail e a nova senha. Este link
                não pode ser usado outra vez.
              </p>
              <button
                className="login-btn"
                type="button"
                data-testid="btn-ir-login"
                onClick={() => window.location.assign(rota("/"))}
              >
                Ir para o login →
              </button>
            </>
          ) : (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void definir();
              }}
            >
              <div className="login-eyebrow login-eyebrow--ink">Primeiro acesso</div>
              <h2 className="login-title">Defina sua senha</h2>
              <p className="login-desc">
                Crie uma senha com no mínimo {SENHA_MINIMO} caracteres e confirme. Ela é guardada
                apenas como hash — nem a plataforma consegue lê-la depois.
              </p>

              <div className="lg-label-linha">
                <label className="login-label" htmlFor="rd-senha">
                  Nova senha
                </label>
                <button
                  type="button"
                  className="lg-btn-txt"
                  aria-pressed={mostrar}
                  onClick={() => setMostrar((v) => !v)}
                >
                  {mostrar ? "ocultar" : "mostrar"}
                </button>
              </div>
              <input
                id="rd-senha"
                className="login-input"
                data-testid="nova-senha"
                type={mostrar ? "text" : "password"}
                placeholder="••••••••••••"
                autoComplete="new-password"
                autoFocus
                aria-describedby="rd-requisitos"
                value={senha}
                onChange={(e) => setSenha(e.target.value)}
              />

              <label className="login-label" htmlFor="rd-senha-2">
                Confirmar senha
              </label>
              <input
                id="rd-senha-2"
                className="login-input"
                data-testid="nova-senha-2"
                type={mostrar ? "text" : "password"}
                placeholder="••••••••••••"
                autoComplete="new-password"
                value={senha2}
                onChange={(e) => setSenha2(e.target.value)}
              />

              <ul className="lg-req" id="rd-requisitos">
                <li className={tamanhoOk ? "ok" : undefined}>
                  <span aria-hidden="true">{tamanhoOk ? "✓" : "•"}</span> ao menos {SENHA_MINIMO}{" "}
                  caracteres
                  {senha.length > 0 && !tamanhoOk && ` (faltam ${SENHA_MINIMO - senha.length})`}
                </li>
                <li className={confereOk ? "ok" : undefined}>
                  <span aria-hidden="true">{confereOk ? "✓" : "•"}</span> a confirmação confere
                </li>
              </ul>

              {erro && (
                <p className="login-erro" role="alert">
                  {erro}
                </p>
              )}

              <button
                className="login-btn"
                type="submit"
                data-testid="btn-definir-senha"
                disabled={carregando || !tamanhoOk || !confereOk}
              >
                {carregando ? "Salvando…" : "Definir senha →"}
              </button>
              <div className="login-foot-note">
                <a href={rota("/")} className="login-link">
                  Voltar ao login
                </a>
              </div>
            </form>
          )}
        </div>
      </div>
    </div>
  );
}
