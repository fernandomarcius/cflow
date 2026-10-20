import { useEffect, useState } from "react";

import * as api from "../../api/client";
import { iniciais, rotuloPerfil } from "../perfil";
import { ConfirmarAcao } from "../ui";

const SENHA_MINIMO = 12;

// Botão de alternar visibilidade das senhas. Reusa o chip do design system (`.chip`/`.mk`/`.on` —
// o mesmo controle de escolha de Permissões e Funções): a marca quadrada preenchida é o estado
// LIGADO/DESLIGADO visível, e o rótulo fica ESTÁVEL para `aria-pressed` não se contradizer
// ("Mostrar senhas, pressionado"). `cursor`/`font-family` inline porque `<button>` não herda
// nenhum dos dois e `.chip` nasceu em `<div>`.
const ESTILO_CHIP: React.CSSProperties = { cursor: "pointer", fontFamily: "inherit" };

// ─────────────────────────────────────────────────────────────────────────
// Minha conta — o usuário logado vê seus dados, troca a PRÓPRIA senha (exige a senha atual) e
// cadastra o segundo fator (TOTP).
//
// Nenhum segredo já salvo é reexibido: a senha só existe no campo que a pessoa acabou de digitar
// (e o "mostrar" revela apenas isso), e o segredo do MFA aparece UMA vez, no momento em que é
// gerado, porque é o único jeito de cadastrá-lo no aplicativo autenticador.
// ─────────────────────────────────────────────────────────────────────────
export default function Conta() {
  const [usuario, setUsuario] = useState<api.UsuarioAtual | null>(null);
  const [erroUsuario, setErroUsuario] = useState(false);

  const [atual, setAtual] = useState("");
  const [nova, setNova] = useState("");
  const [confirma, setConfirma] = useState("");
  const [mostrar, setMostrar] = useState(false);
  const [erro, setErro] = useState<string | null>(null);
  const [ok, setOk] = useState(false);
  const [salvando, setSalvando] = useState(false);

  // MFA (TOTP): configurar gera um segredo novo e DESLIGA o fator até a confirmação — por isso a
  // ação passa por confirmação explícita.
  const [pedirMfa, setPedirMfa] = useState(false);
  const [segredoMfa, setSegredoMfa] = useState<string | null>(null);
  const [codigoMfa, setCodigoMfa] = useState("");
  const [erroMfa, setErroMfa] = useState<string | null>(null);
  const [okMfa, setOkMfa] = useState(false);
  const [ocupadoMfa, setOcupadoMfa] = useState(false);
  const [copiado, setCopiado] = useState(false);
  // Estado REAL do 2º fator (vem do /auth/me). `okMfa` é só o retorno da ação nesta visita.
  const [mfaLigado, setMfaLigado] = useState(false);
  const [pedirDesligarMfa, setPedirDesligarMfa] = useState(false);

  useEffect(() => {
    let vivo = true;
    api
      .meAtual()
      .then((u) => {
        if (!vivo) return;
        setUsuario(u);
        setMfaLigado(u.mfa_habilitado === true);
      })
      .catch(() => vivo && setErroUsuario(true));
    return () => {
      vivo = false;
    };
  }, []);

  const atualOk = atual.length > 0;
  const tamanhoOk = nova.length >= SENHA_MINIMO;
  const confereOk = nova.length > 0 && nova === confirma;
  const podeTrocar = atualOk && tamanhoOk && confereOk && !salvando;

  // Regra da senha em TRÊS estados, não dois: enquanto o campo está em branco a regra é "pendente"
  // (marcador neutro), não "não atendida" — abrir a tela com tudo vermelho culpa quem ainda não
  // digitou. Depois de digitar, ✓ ou ✗.
  //
  // O TEXTO tem de mudar junto com o estado: em "pendente" ele enuncia o que falta ("as duas
  // senhas iguais"), nunca afirma a regra atendida. A lista dizia "a confirmação confere" com o
  // marcador neutro antes de qualquer tecla — afirmava conferência de dois campos vazios.
  type EstadoRegra = "ok" | "falta" | "pendente";
  // A senha atual não tem terceiro estado: o cliente só sabe se foi INFORMADA (se está certa, quem
  // diz é o servidor no envio). Marcar ✗ aqui seria acusar uma senha que ninguém conferiu.
  const estadoAtual: EstadoRegra = atualOk ? "ok" : "pendente";
  const estadoTamanho: EstadoRegra = tamanhoOk ? "ok" : nova.length > 0 ? "falta" : "pendente";
  const estadoConfere: EstadoRegra = confereOk
    ? "ok"
    : confirma.length > 0
      ? "falta"
      : "pendente";
  const marca = (e: EstadoRegra) => (e === "ok" ? "✓" : e === "falta" ? "✗" : "•");

  const trocar = async () => {
    setErro(null);
    setOk(false);
    if (!atual) {
      setErro("Informe a senha atual.");
      return;
    }
    if (!tamanhoOk) {
      setErro(`A nova senha deve ter ao menos ${SENHA_MINIMO} caracteres.`);
      return;
    }
    if (!confereOk) {
      setErro("A confirmação não confere com a nova senha.");
      return;
    }
    setSalvando(true);
    try {
      await api.trocarSenha(atual, nova);
      setOk(true);
      setAtual("");
      setNova("");
      setConfirma("");
      setMostrar(false);
    } catch (e: any) {
      // `requestId` do envelope junto da mensagem (o cliente já o carrega): é o código que o
      // suporte pede, e só o "Desligar MFA" desta tela o mostrava.
      const rid = e?.requestId ? ` (código de suporte ${e.requestId})` : "";
      setErro((e instanceof Error ? e.message : "Falha ao trocar a senha.") + rid);
    } finally {
      setSalvando(false);
    }
  };

  const configurarMfa = async () => {
    setErroMfa(null);
    setOkMfa(false);
    setOcupadoMfa(true);
    try {
      const r = await api.apiFetch("POST", "/auth/mfa/configurar");
      if (!r.ok) {
        const j = await r.json().catch(() => null);
        setErroMfa((j as any)?.erro?.mensagem ?? "Não foi possível iniciar o cadastro do MFA.");
        return;
      }
      const j = await r.json();
      setSegredoMfa(String(j.segredo ?? ""));
      setCodigoMfa("");
    } catch {
      setErroMfa("Falha de rede ao iniciar o cadastro do MFA.");
    } finally {
      setOcupadoMfa(false);
    }
  };

  const ativarMfa = async () => {
    setErroMfa(null);
    const codigo = codigoMfa.replace(/\D/g, "");
    if (codigo.length !== 6) {
      setErroMfa("Informe os 6 dígitos mostrados no aplicativo.");
      return;
    }
    setOcupadoMfa(true);
    try {
      const r = await api.apiFetch("POST", "/auth/mfa/ativar", { codigo });
      if (!r.ok) {
        const j = await r.json().catch(() => null);
        setErroMfa(
          (j as any)?.erro?.mensagem ??
            "Código inválido. Ele muda a cada 30 segundos — tente o atual.",
        );
        return;
      }
      // O segredo sai da tela no instante em que o fator é confirmado (não se reexibe).
      setSegredoMfa(null);
      setCodigoMfa("");
      setOkMfa(true);
      setMfaLigado(true);
    } catch {
      setErroMfa("Falha de rede ao confirmar o código.");
    } finally {
      setOcupadoMfa(false);
    }
  };

  const copiarSegredo = async () => {
    if (!segredoMfa) return;
    try {
      await navigator.clipboard.writeText(segredoMfa);
      setCopiado(true);
      window.setTimeout(() => setCopiado(false), 2500);
    } catch {
      // Sem permissão de área de transferência (contexto não seguro): o valor está visível para
      // digitação manual, que é o caminho do cadastro sem QR.
      setErroMfa("Não foi possível copiar. Selecione o código e copie manualmente.");
    }
  };

  const desligarMfa = async (senha: string) => {
    setErroMfa(null);
    setOkMfa(false);
    setOcupadoMfa(true);
    try {
      await api.mfaDesativar(senha);
      setMfaLigado(false);
      setSegredoMfa(null);
      setCodigoMfa("");
      // Sem isto, desligar o 2º fator mudava só a pill: a ação de segurança terminava sem nenhuma
      // confirmação e sem dizer o que muda no próximo login.
      setOkMfa(true);
    } catch (e: any) {
      const rid = e?.requestId ? ` (código de suporte ${e.requestId})` : "";
      setErroMfa((e?.message ?? "Falha ao desligar a verificação em duas etapas.") + rid);
    } finally {
      setOcupadoMfa(false);
    }
  };

  return (
    <>
      {pedirMfa && (
        <ConfirmarAcao
          titulo="Gerar um novo segredo de MFA"
          corpo={
            <>
              Vamos gerar um código de cadastro novo para o seu aplicativo autenticador. Enquanto
              você não confirmar com os 6 dígitos, o <b>segundo fator fica desligado</b> — e{" "}
              <b>se você já usa um autenticador nesta conta, o cadastro antigo deixa de valer</b>.
              Tenha o aplicativo à mão antes de continuar.
            </>
          }
          rotuloConfirmar="Gerar código de cadastro"
          onConfirmar={() => {
            setPedirMfa(false);
            void configurarMfa();
          }}
          onCancelar={() => setPedirMfa(false)}
        />
      )}
      {pedirDesligarMfa && (
        <ConfirmarAcao
          titulo="Desligar a verificação em duas etapas"
          corpo={
            <>
              Sem o segundo fator, entrar passa a exigir <b>só a sua senha</b>. Confirme com a senha
              atual — e guarde que o cadastro do autenticador é apagado: religar depois gera uma
              chave nova.
            </>
          }
          rotuloConfirmar="Desligar"
          perigo
          rotuloCampo="Sua senha atual"
          valorInicial=""
          onConfirmar={(senha) => {
            setPedirDesligarMfa(false);
            void desligarMfa(senha);
          }}
          onCancelar={() => setPedirDesligarMfa(false)}
        />
      )}

      <div className="page-head">
        <h1 className="display">Minha conta</h1>
        <p className="sub">
          Seus dados, sua senha e o segundo fator de autenticação. Seu perfil de acesso é definido
          por um administrador.
        </p>
      </div>

      <div className="pad">
        {/* Grade de cards (identidade em cima, senha e MFA lado a lado): quem a define é `.ct-wrap`
            no CSS. Os estilos inline que duplicavam a grade saíram — a regra do CSS já era a
            vigente, e o comentário de "o CSS ainda centra/estreita" descrevia um estado que não
            existe mais. */}
        <div className="conta-wrap ct-wrap">
          {usuario ? (
            <div className="conta-card conta-id" data-testid="conta-usuario">
              <span className="conta-avatar">{iniciais(usuario.nome)}</span>
              <div>
                <div className="conta-nome">{usuario.nome}</div>
                <div className="conta-meta">{usuario.email}</div>
                <div className="conta-perfil">{rotuloPerfil(usuario.perfil)}</div>
              </div>
            </div>
          ) : erroUsuario ? (
            <div className="conta-card ct-erro-id" role="alert">
              Não foi possível carregar seus dados agora. A troca de senha abaixo continua
              funcionando.
            </div>
          ) : (
            <div className="conta-card conta-id" aria-busy="true">
              <span className="conta-avatar">…</span>
              <div className="conta-meta">Carregando seus dados…</div>
            </div>
          )}

          <form
            className="conta-card conta-form"
            data-testid="conta"
            onSubmit={(e) => {
              e.preventDefault();
              void trocar();
            }}
          >
            <h2 className="conta-h2">Trocar minha senha</h2>

            <label className="conta-fld" htmlFor="ct-atual">
              Senha atual
            </label>
            <input
              id="ct-atual"
              className="conta-in"
              type={mostrar ? "text" : "password"}
              autoComplete="current-password"
              data-testid="conta-senha-atual"
              value={atual}
              onChange={(e) => setAtual(e.target.value)}
            />

            <label className="conta-fld" htmlFor="ct-nova">
              Nova senha
            </label>
            <input
              id="ct-nova"
              className="conta-in"
              type={mostrar ? "text" : "password"}
              autoComplete="new-password"
              placeholder={`mínimo ${SENHA_MINIMO} caracteres`}
              aria-describedby="ct-requisitos"
              data-testid="conta-senha-nova"
              value={nova}
              onChange={(e) => setNova(e.target.value)}
            />

            <label className="conta-fld" htmlFor="ct-confirma">
              Confirmar nova senha
            </label>
            <input
              id="ct-confirma"
              className="conta-in"
              type={mostrar ? "text" : "password"}
              autoComplete="new-password"
              data-testid="conta-senha-confirma"
              value={confirma}
              onChange={(e) => setConfirma(e.target.value)}
            />

            {/* `aria-live`: a lista é a descrição dos campos (aria-describedby) E o retorno ao vivo
                da política — sem isto, quem usa leitor de tela só descobre o estado ao submeter. */}
            <ul className="ct-req" id="ct-requisitos" aria-live="polite">
              <li className={estadoAtual}>
                <span aria-hidden="true">{marca(estadoAtual)}</span>{" "}
                {estadoAtual === "ok"
                  ? "senha atual informada — ela é conferida no envio"
                  : "a senha atual, para confirmar que é você"}
              </li>
              <li className={estadoTamanho}>
                <span aria-hidden="true">{marca(estadoTamanho)}</span> ao menos {SENHA_MINIMO}{" "}
                caracteres na nova senha
                {estadoTamanho === "falta" && ` — faltam ${SENHA_MINIMO - nova.length}`}
                {estadoTamanho === "ok" && ` — ${nova.length} digitados`}
              </li>
              <li className={estadoConfere}>
                <span aria-hidden="true">{marca(estadoConfere)}</span>{" "}
                {estadoConfere === "ok"
                  ? "a confirmação confere"
                  : estadoConfere === "falta"
                    ? "a confirmação ainda não confere"
                    : "a nova senha repetida na confirmação"}
              </li>
            </ul>

            <div className="ct-acoes">
              <button
                className="btn-stamp"
                type="submit"
                disabled={!podeTrocar}
                data-testid="conta-salvar"
                title={
                  podeTrocar
                    ? undefined
                    : !atualOk
                      ? "Informe a senha atual"
                      : !tamanhoOk
                        ? `A nova senha precisa de ao menos ${SENHA_MINIMO} caracteres`
                        : "A confirmação ainda não confere"
                }
                aria-describedby="ct-requisitos"
              >
                {salvando ? "Salvando…" : "Trocar senha"}
              </button>
              <button
                type="button"
                className={"chip" + (mostrar ? " on" : "")}
                style={ESTILO_CHIP}
                aria-pressed={mostrar}
                data-testid="conta-mostrar"
                title={
                  mostrar
                    ? "As três senhas estão visíveis nesta tela — clique para ocultar"
                    : "Mostra em texto o que você digitou nos três campos"
                }
                onClick={() => setMostrar((v) => !v)}
              >
                <span className="mk" aria-hidden="true" />
                Mostrar senhas
              </button>
            </div>

            {/* Faixa do design system (`.adm-banner`), não `.login-erro`: aquela classe fixa fundo
                claro e, no tema escuro, era o único bloco branco dentro do card escuro. */}
            {erro && (
              <div className="card adm-banner adm-banner--erro ct-msg" role="alert" data-testid="conta-erro">
                <span className="adm-banner-d" />
                <span>{erro}</span>
              </div>
            )}
            {ok && (
              <div className="card adm-banner adm-banner--ok ct-msg" role="status" data-testid="conta-ok">
                <span className="adm-banner-d" />
                <span>
                  Senha trocada. Sua sessão atual segue valendo; se você estiver logado em outro
                  navegador, saia e entre de novo com a senha nova.
                </span>
              </div>
            )}
          </form>

          <section className="conta-card ct-mfa">
            <h2 className="conta-h2">Verificação em duas etapas (MFA)</h2>
            <p className="ct-mfa-txt">
              Com o MFA ligado, o login pede um código de 6 dígitos gerado por um aplicativo
              autenticador (TOTP) no seu celular, além da senha. O código é gerado no aparelho, sem
              depender de rede — nada sai da nossa infraestrutura.
            </p>

            {/* Estado explícito: antes a tela oferecia "configurar" para todo mundo e quem já tinha
                MFA não sabia se estava protegido.
                Pill `off` (neutro), não `skip` (âmbar): a plataforma não exige MFA, então
                "desligado" é um FATO da conta, não um alerta — âmbar aqui pintaria de atenção uma
                configuração legítima (e âmbar já significa "pode escrever" em Conexões). O empurrão
                fica na frase ao lado, em palavras. */}
            <p className="ct-mfa-estado">
              <span className={"statuspill " + (mfaLigado ? "ok" : "off")} data-testid="conta-mfa-estado">
                <span className="d" />
                {mfaLigado ? "ligado nesta conta" : "desligado"}
              </span>{" "}
              <span className="ct-mfa-nota">
                {mfaLigado
                  ? "o login pede a senha e o código do aplicativo"
                  : "hoje o login pede só a sua senha — recomendado ligar"}
              </span>
            </p>

            {!segredoMfa && (
              <div className="ct-mfa-acoes">
                <button
                  type="button"
                  className="btn-sec"
                  disabled={ocupadoMfa}
                  data-testid="conta-mfa-configurar"
                  onClick={() => setPedirMfa(true)}
                >
                  {ocupadoMfa
                    ? "Gerando…"
                    : mfaLigado
                      ? "Trocar de aplicativo autenticador"
                      : "Configurar aplicativo autenticador"}
                </button>
                {mfaLigado && (
                  <button
                    type="button"
                    className="btn-sec"
                    disabled={ocupadoMfa}
                    data-testid="conta-mfa-desligar"
                    onClick={() => setPedirDesligarMfa(true)}
                  >
                    Desligar verificação em duas etapas
                  </button>
                )}
              </div>
            )}

            {segredoMfa && (
              <div className="ct-mfa-passos">
                <ol>
                  <li>
                    No aplicativo autenticador, escolha “adicionar conta” → “inserir chave manual”.
                  </li>
                  <li>
                    Cole a chave abaixo (conta:{" "}
                    <b>{usuario?.email ?? "seu e-mail"}</b>). Ela aparece{" "}
                    <b>uma única vez</b> e não é reexibida.
                    <div className="ct-mfa-segredo">
                      <code data-testid="conta-mfa-segredo">{segredoMfa}</code>
                      <button type="button" className="pag-btn" onClick={() => void copiarSegredo()}>
                        {copiado ? "Copiado" : "Copiar"}
                      </button>
                    </div>
                  </li>
                  <li>
                    Digite o código de 6 dígitos que o aplicativo mostrar:
                    <div className="ct-mfa-codigo">
                      <label className="conta-fld" htmlFor="ct-mfa-codigo">
                        Código do aplicativo
                      </label>
                      <input
                        id="ct-mfa-codigo"
                        className="conta-in num"
                        inputMode="numeric"
                        autoComplete="one-time-code"
                        maxLength={6}
                        placeholder="000000"
                        data-testid="conta-mfa-codigo"
                        value={codigoMfa}
                        onChange={(e) => setCodigoMfa(e.target.value.replace(/\D/g, ""))}
                        onKeyDown={(e) => e.key === "Enter" && (e.preventDefault(), void ativarMfa())}
                      />
                    </div>
                  </li>
                </ol>
                <div className="ct-acoes">
                  <button
                    type="button"
                    className="btn-stamp"
                    disabled={ocupadoMfa}
                    data-testid="conta-mfa-ativar"
                    onClick={() => void ativarMfa()}
                  >
                    {ocupadoMfa ? "Confirmando…" : "Confirmar e ligar o MFA"}
                  </button>
                  <button
                    type="button"
                    className="lnk"
                    onClick={() => {
                      setSegredoMfa(null);
                      setCodigoMfa("");
                      setErroMfa(null);
                    }}
                  >
                    Cancelar
                  </button>
                </div>
                <p className="ct-mfa-aviso">
                  Enquanto você não confirmar, o segundo fator fica <b>desligado</b> e o login pede
                  só a senha.
                </p>
              </div>
            )}

            {erroMfa && (
              <div
                className="card adm-banner adm-banner--erro ct-msg"
                role="alert"
                data-testid="conta-mfa-erro"
              >
                <span className="adm-banner-d" />
                <span>{erroMfa}</span>
              </div>
            )}
            {okMfa && (
              <div
                className="card adm-banner adm-banner--ok ct-msg"
                role="status"
                data-testid="conta-mfa-ok"
              >
                <span className="adm-banner-d" />
                <span>
                  {mfaLigado
                    ? "MFA ligado. No próximo login, informe a senha e o código do aplicativo."
                    : "Verificação em duas etapas desligada. O próximo login pede só a sua senha, e o cadastro do autenticador antigo não vale mais."}
                </span>
              </div>
            )}
          </section>
        </div>
      </div>
    </>
  );
}
