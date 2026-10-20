import { useEffect, useRef, useState } from "react";
import { useDispatch } from "react-redux";

import * as api from "../../api/client";
import { rota } from "../../prefixo";
import { setToken } from "../../store/store";

// ─────────────────────────────────────────────────────────────────────────
// Login (fora do shell) — split de dois painéis: marca (navy) + formulário.
//
// Três coisas que a tela tem de acertar:
//
// 1. SEGUNDO FATOR. Quem tem MFA ligado só entra informando o código do autenticador. A API
//    responde `mfa_requerido` e a tela revela o campo — sem ele, uma conta com MFA simplesmente
//    não conseguia entrar (o campo não existia em nenhum lugar do produto).
// 2. BLOQUEIO POR TENTATIVAS. `muitas_tentativas` (429) é uma resposta DIFERENTE de credencial
//    errada: mostrar "e-mail ou senha inválidos" fazia a pessoa tentar de novo (e prolongar o
//    bloqueio) achando que tinha errado a senha.
// 3. RESPOSTA GENÉRICA. Login e recuperação nunca revelam se o e-mail existe: mensagem única para
//    credencial inválida e aviso único ("se houver uma conta…") na recuperação.
// ─────────────────────────────────────────────────────────────────────────
const AVISO_RECUPERACAO =
  "Se houver uma conta com esse e-mail, enviamos um link para definir a senha. " +
  "O mesmo link serve para o primeiro acesso e vale por tempo limitado.";

export default function Login() {
  const d = useDispatch();
  const [email, setEmail] = useState("");
  const [senha, setSenha] = useState("");
  const [codigo, setCodigo] = useState("");
  const [pedeCodigo, setPedeCodigo] = useState(false);
  const [mostrarSenha, setMostrarSenha] = useState(false);
  const [erro, setErroLocal] = useState("");
  const [carregando, setCarregando] = useState(false);
  const [enviandoLink, setEnviandoLink] = useState(false);
  const [aviso, setAviso] = useState("");
  const campoCodigo = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (pedeCodigo) campoCodigo.current?.focus();
  }, [pedeCodigo]);

  const recuperar = async () => {
    setErroLocal("");
    setAviso("");
    const e = email.trim().toLowerCase();
    if (!e) {
      setErroLocal("Informe seu e-mail para receber o link de acesso.");
      return;
    }
    setEnviandoLink(true);
    try {
      await api.solicitarRecuperacao(e);
    } catch {
      /* resposta genérica de propósito: não revela se o e-mail tem cadastro */
    } finally {
      setEnviandoLink(false);
    }
    setAviso(AVISO_RECUPERACAO);
  };

  const entrar = async () => {
    setErroLocal("");
    setAviso("");
    const e = email.trim().toLowerCase();
    if (!e || !senha) {
      setErroLocal("Informe e-mail e senha.");
      return;
    }
    setCarregando(true);
    try {
      // Chamada direta (em vez de `api.login`) para ler o CÓDIGO do envelope de erro: é ele que
      // distingue "precisa do 2º fator" de "credencial inválida" e de "bloqueado por tentativas".
      const r = await api.apiFetch("POST", "/auth/login", {
        email: e,
        senha,
        codigo: codigo.trim() || null,
      });
      if (r.ok) {
        const j = await r.json();
        api.definirToken(j.token);
        d(setToken(j.token));
        return;
      }
      const j = await r.json().catch(() => null);
      const codigoErro = (j as any)?.erro?.codigo;
      if (codigoErro === "mfa_requerido") {
        setPedeCodigo(true);
        setErroLocal(
          codigo.trim()
            ? "Código inválido ou expirado. O código muda a cada 30 segundos — use o atual."
            : "",
        );
        setAviso(
          codigo.trim() ? "" : "Esta conta usa verificação em duas etapas. Informe o código do aplicativo autenticador.",
        );
        setCodigo("");
        return;
      }
      if (codigoErro === "muitas_tentativas") {
        setErroLocal(
          (j as any)?.erro?.mensagem ??
            "Muitas tentativas de login. Aguarde alguns minutos antes de tentar de novo.",
        );
        return;
      }
      // Falha DO SERVIDOR não é credencial errada: com 500/502/503 a tela acusava a senha da
      // pessoa, que então trocava a senha para resolver uma indisponibilidade. A resposta
      // genérica existe para não revelar se o e-mail existe — isso vale para o 401, não para o
      // servidor fora do ar.
      if (r.status >= 500) {
        setErroLocal(
          "O servidor não respondeu corretamente. Não é problema com a sua senha — tente de novo em instantes." +
            ((j as any)?.erro?.request_id ? ` (código de suporte ${(j as any).erro.request_id})` : ""),
        );
        return;
      }
      setErroLocal("E-mail ou senha inválidos.");
    } catch {
      setErroLocal("Não foi possível falar com o servidor. Verifique a conexão e tente de novo.");
    } finally {
      setCarregando(false);
    }
  };

  return (
    <div className="login-screen" data-testid="login">
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
          <h1 className="login-headline">Do dado ao relatório, auditável de ponta a ponta.</h1>
          <p className="login-lead">
            Monte transformações em grafo, publique medidas governadas e gere relatórios carimbados
            e rastreáveis — tudo na sua infraestrutura.
          </p>
        </div>
        <div className="login-brand-foot">© cFlow · Plataforma de Dados</div>
      </div>

      {/* painel do formulário */}
      <div className="login-form-panel">
        <form
          className="login-form"
          onSubmit={(ev) => {
            ev.preventDefault();
            void entrar();
          }}
        >
          <div className="login-eyebrow login-eyebrow--ink">Bem-vindo de volta</div>
          <h2 className="login-title">Entrar no sistema</h2>
          <p className="login-desc">Informe seu e-mail e senha para acessar.</p>

          <label className="login-label" htmlFor="lg-email">
            E-mail
          </label>
          <input
            id="lg-email"
            className="login-input"
            data-testid="email"
            type="email"
            placeholder="voce@example.com"
            autoComplete="username"
            autoFocus
            value={email}
            onChange={(ev) => setEmail(ev.target.value)}
          />

          <div className="lg-label-linha">
            <label className="login-label" htmlFor="lg-senha">
              Senha
            </label>
            {/* Alternar visibilidade da senha: era a palavra "MOSTRAR"/"OCULTAR" em caixa-alta, do
                tamanho e da cor do rótulo do campo — lia como legenda, não como controle, e o
                estado só existia na troca da palavra. Agora é um controle com marca de LIGADO (o
                mesmo quadrado do chip do design system) e rótulo ESTÁVEL, para `aria-pressed` dizer
                o estado sem se contradizer com o texto.
                Cores fixas, não tokens: este painel é claro em qualquer tema (ver "CSS a aplicar"). */}
            <button
              type="button"
              className="lg-olho"
              aria-pressed={mostrarSenha}
              data-testid="mostrar-senha"
              title={
                mostrarSenha
                  ? "A senha está visível na tela — clique para ocultar"
                  : "Clique para ver a senha que você digitou"
              }
              onClick={() => setMostrarSenha((v) => !v)}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                marginBottom: 6,
                padding: "3px 9px",
                borderRadius: 999,
                border: "1px solid " + (mostrarSenha ? "#002050" : "#d8dfe8"),
                background: mostrarSenha ? "#eef2f8" : "#fff",
                color: mostrarSenha ? "#002050" : "#5b6b80",
                fontFamily: "inherit",
                fontSize: 11,
                fontWeight: 700,
                letterSpacing: ".02em",
                cursor: "pointer",
              }}
            >
              <span
                aria-hidden="true"
                style={{
                  width: 9,
                  height: 9,
                  borderRadius: 3,
                  flex: "none",
                  border: "1.5px solid " + (mostrarSenha ? "#C0D000" : "#c3ccd8"),
                  background: mostrarSenha ? "#C0D000" : "transparent",
                }}
              />
              Mostrar senha
            </button>
          </div>
          <input
            id="lg-senha"
            className="login-input"
            data-testid="senha"
            type={mostrarSenha ? "text" : "password"}
            // Não usar oito bolinhas como placeholder: num campo de senha vazio elas parecem um
            // valor preenchido (o navegador desenha o mesmo glifo do mascaramento).
            placeholder="sua senha"
            autoComplete="current-password"
            value={senha}
            onChange={(ev) => setSenha(ev.target.value)}
          />

          {pedeCodigo && (
            <>
              <label className="login-label" htmlFor="lg-codigo">
                Código do autenticador
              </label>
              <input
                id="lg-codigo"
                ref={campoCodigo}
                className="login-input num"
                data-testid="codigo-mfa"
                type="text"
                inputMode="numeric"
                maxLength={6}
                placeholder="000000"
                autoComplete="one-time-code"
                aria-describedby="lg-codigo-dica"
                value={codigo}
                onChange={(ev) => setCodigo(ev.target.value.replace(/\D/g, ""))}
              />
              <p className="lg-dica" id="lg-codigo-dica">
                6 dígitos do seu aplicativo autenticador. Ele muda a cada 30 segundos.
              </p>
            </>
          )}

          {erro && (
            <p className="login-erro" role="alert">
              {erro}
            </p>
          )}
          {aviso && (
            <p className="login-aviso" role="status">
              {aviso}
            </p>
          )}

          <button className="login-btn" type="submit" data-testid="btn-entrar" disabled={carregando}>
            {carregando ? "Entrando…" : "Entrar →"}
          </button>
          <div className="login-foot-note">
            <button
              type="button"
              className="login-link lg-btn-link"
              data-testid="link-recuperar"
              disabled={enviandoLink}
              onClick={() => void recuperar()}
            >
              {enviandoLink ? "Enviando…" : "Esqueci minha senha / primeiro acesso"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
