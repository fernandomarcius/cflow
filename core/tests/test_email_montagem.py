"""Unit -- assembly of the `email` node: template, attachments, plan and MIME. No database, no network.

What these tests protect: the dry-run and the execution call the SAME `montar_plano`, so everything
decided here (escaping, recipient dedup, safe header, CSV header) holds in both paths by
construction.
"""

from __future__ import annotations

import base64
from decimal import Decimal

import pytest

from cflow_core.conexoes.conectores.smtp import montar_mime, validar_config
from cflow_core.errors import ErroAplicacao
from cflow_core.execucao import email_anexos
from cflow_core.execucao.email_template import Contexto, placeholders, renderizar
from cflow_core.execucao.no_email import enderecos, montar_plano

COLUNAS = ["projeto", "valor", "obs"]
LINHAS = [
    {"projeto": "ALPHA", "valor": Decimal("1234.56"), "obs": "accent é <b>x</b>"},
    {"projeto": "BETA", "valor": Decimal("78.90"), "obs": None},
]


class FonteFake:
    """Test `FonteDados` -- proves the node does not depend on DuckDB to build the message."""

    def __init__(self, colunas=None, linhas=None, tabelas=None) -> None:
        self._t = tabelas or {}
        self._c, self._l = colunas or COLUNAS, LINHAS if linhas is None else linhas

    def tabela(self, no_id):
        return self._t.get(no_id, (self._c, self._l))

    def parquet(self, no_id):
        return b"PAR1-fake"


def _cfg(**extra):
    base = {
        "conexao_id": "cx",
        "modo": "unico",
        "para": "a@example.com",
        "assunto": "Subject",
        "corpo": "<p>body</p>",
    }
    base.update(extra)
    return base


# ---------------- template ----------------


def test_renderiza_param_contagem_e_tabela():
    ctx = Contexto(colunas=COLUNAS, linhas=LINHAS, params={"competencia": "2026-07"})
    texto, nr = renderizar("{{param.competencia}} · {{contagem}} · {{tabela}}", ctx)
    assert texto.startswith("2026-07 · 2 · <table")
    assert nr == []


def test_valor_de_dado_vai_escapado_no_html():
    ctx = Contexto(colunas=COLUNAS, linhas=LINHAS, linha=LINHAS[0])
    texto, _ = renderizar("<p>{{obs}}</p>", ctx)
    assert "&lt;b&gt;x&lt;/b&gt;" in texto and "<b>x</b>" not in texto


def test_tabela_escapa_celula_mas_gera_markup():
    ctx = Contexto(colunas=COLUNAS, linhas=LINHAS)
    texto, _ = renderizar("{{tabela}}", ctx)
    assert "<td" in texto and "&lt;b&gt;x&lt;/b&gt;" in texto


def test_decimal_preserva_todo_digito():
    ctx = Contexto(colunas=COLUNAS, linhas=LINHAS, linha=LINHAS[0])
    texto, _ = renderizar("{{valor}}", ctx)
    assert texto == "1234.56"  # no float conversion, no rounding


def test_nao_resolvido_vira_vazio_e_e_reportado():
    texto, nr = renderizar("a{{fantasma}}b", Contexto())
    assert texto == "ab" and nr == ["fantasma"]


def test_tabela_limite_declara_o_restante():
    linhas = [{"projeto": f"o{i}"} for i in range(10)]
    ctx = Contexto(colunas=["projeto"], linhas=linhas, tabela_limite=3)
    texto, _ = renderizar("{{tabela}}", ctx)
    assert texto.count("<tr>") == 4  # header + 3 rows
    assert "and 7 more row(s)" in texto


def test_tabela_texto_sem_html():
    ctx = Contexto(colunas=COLUNAS, linhas=LINHAS, html=False)
    texto, _ = renderizar("{{tabela}}", ctx)
    assert texto.startswith("projeto\tvalor\tobs")
    assert "<table" not in texto and "<td" not in texto
    # plain-text body does NOT escape: data goes as is (escaping is an HTML concern)
    assert "accent é <b>x</b>" in texto


def test_placeholders_lista_em_ordem_sem_repetir():
    assert placeholders("{{a}} {{b}} {{a}}") == ["a", "b"]


# ---------------- recipients ----------------


@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        ("a@example.com;b@example.com", ["a@example.com", "b@example.com"]),
        ("a@example.com, b@example.com ", ["a@example.com", "b@example.com"]),
        ("a@example.com;a@example.com", ["a@example.com"]),  # dedup
        ("Jane Doe <a@example.com>", ["a@example.com"]),
        ("no-at-sign;;  ", []),
    ],
)
def test_enderecos(entrada, esperado):
    assert enderecos(entrada) == esperado


def test_sem_destinatario_valido_falha():
    with pytest.raises(ErroAplicacao) as exc:
        montar_plano(_cfg(para="not-an-email"), dados=FonteFake(), entrada="no1")
    assert exc.value.codigo == "email_sem_destinatario"


# ---------------- attachments ----------------


def test_csv_tem_cabecalho_crlf_e_bom():
    dados = email_anexos.serializar("csv", COLUNAS, LINHAS)
    assert dados.startswith(b"\xef\xbb\xbf")  # utf-8-sig: Excel opens it with accents intact
    texto = dados.decode("utf-8-sig")
    assert texto.splitlines()[0] == "projeto;valor;obs"
    assert "\r\n" in texto
    assert "BETA;78.90;" in texto  # None becomes empty, not "None"


def test_csv_sem_cabecalho_e_outro_delimitador():
    texto = email_anexos.serializar(
        "csv", COLUNAS, LINHAS, incluir_cabecalho=False, delimitador=",", codificacao="utf-8"
    ).decode()
    assert texto.startswith("ALPHA,1234.56,")


def test_xlsx_e_zip_valido():
    dados = email_anexos.serializar("xlsx", COLUNAS, LINHAS)
    assert dados[:2] == b"PK"  # xlsx is a zip


def test_json_preserva_valor_como_texto_exato():
    import json as _json

    dados = _json.loads(email_anexos.serializar("json", COLUNAS, LINHAS).decode())
    assert dados[0]["valor"] == "1234.56" and dados[1]["obs"] is None


def test_parquet_delega_ao_exportador():
    assert (
        email_anexos.serializar("parquet", COLUNAS, LINHAS, parquet_bytes=lambda: b"PAR1")
        == b"PAR1"
    )


def test_parquet_sem_exportador_falha_claro():
    with pytest.raises(ErroAplicacao) as exc:
        email_anexos.serializar("parquet", COLUNAS, LINHAS)
    assert exc.value.codigo == "email_anexo_formato"


def test_anexo_vazio_falha_por_padrao():
    with pytest.raises(ErroAplicacao) as exc:
        email_anexos.de_dados({"formato": "csv"}, "x.csv", COLUNAS, [])
    assert exc.value.codigo == "email_anexo_vazio"


def test_nome_de_anexo_nao_carrega_caminho():
    anexo = email_anexos.de_dados({"formato": "csv"}, "../../etc/passwd.csv", COLUNAS, LINHAS)
    assert anexo.nome == "passwd.csv"


def test_anexo_de_coluna_base64_e_hex():
    conteudo = b"conteudo-binario"
    b64 = email_anexos.de_coluna(
        {"coluna_conteudo": "c", "coluna_nome": "n", "codificacao_conteudo": "base64"},
        {"c": base64.b64encode(conteudo).decode(), "n": "arq.pdf"},
    )
    assert b64.conteudo == conteudo and b64.nome == "arq.pdf" and b64.tipo_mime == "application/pdf"
    hexa = email_anexos.de_coluna(
        {"coluna_conteudo": "c", "codificacao_conteudo": "hex", "nome": "a.bin"},
        {"c": conteudo.hex()},
    )
    assert hexa.conteudo == conteudo


def test_anexo_de_coluna_conteudo_invalido_falha():
    with pytest.raises(ErroAplicacao) as exc:
        email_anexos.de_coluna({"coluna_conteudo": "c"}, {"c": "not base64!!"})
    assert exc.value.codigo == "email_anexo_conteudo"


def test_anexo_de_coluna_ausente_falha():
    with pytest.raises(ErroAplicacao) as exc:
        email_anexos.de_coluna({"coluna_conteudo": "sumida"}, {"c": "x"})
    assert exc.value.codigo == "email_anexo_coluna"


# ---------------- plan ----------------


def test_plano_unico_leva_todas_as_linhas_em_um_email():
    plano = montar_plano(
        _cfg(anexos=[{"origem": "dados", "formato": "csv", "nome": "r.csv"}]),
        dados=FonteFake(),
        entrada="no1",
        config_conexao={"host": "smtp.example.com", "usuario": "u@example.com"},
    )
    assert plano.total == 1 and plano.contagem_entrada == 2
    assert plano.host_destino == "smtp.example.com" and plano.remetente == "u@example.com"
    assert len(plano.mensagens[0].anexos) == 1


def test_plano_por_linha_gera_um_email_por_linha():
    plano = montar_plano(
        _cfg(modo="por_linha", para="{{projeto}}@example.com", assunto="{{projeto}}"),
        dados=FonteFake(),
        entrada="no1",
    )
    assert [m.assunto for m in plano.mensagens] == ["ALPHA", "BETA"]
    assert [m.para for m in plano.mensagens] == [("ALPHA@example.com",), ("BETA@example.com",)]


def test_plano_por_linha_sem_entrada_falha():
    with pytest.raises(ErroAplicacao) as exc:
        montar_plano(_cfg(modo="por_linha"), dados=FonteFake(), entrada=None)
    assert exc.value.codigo == "email_sem_entrada"


def test_plano_zero_linhas_falha_sem_permitir_vazio():
    with pytest.raises(ErroAplicacao) as exc:
        montar_plano(_cfg(), dados=FonteFake(linhas=[]), entrada="no1")
    assert exc.value.codigo == "email_vazio"


def test_plano_zero_linhas_permitido_ainda_envia():
    plano = montar_plano(_cfg(permitir_vazio=True), dados=FonteFake(linhas=[]), entrada="no1")
    assert plano.total == 1 and plano.contagem_entrada == 0


def test_plano_anexo_de_outro_no():
    fonte = FonteFake(tabelas={"no1": (COLUNAS, LINHAS), "outro": (["k"], [{"k": "v"}])})
    plano = montar_plano(
        _cfg(anexos=[{"origem": "dados", "de": "outro", "formato": "csv", "nome": "o.csv"}]),
        dados=fonte,
        entrada="no1",
    )
    assert plano.mensagens[0].anexos[0].conteudo.decode("utf-8-sig").splitlines()[0] == "k"


def test_plano_unico_com_anexo_por_coluna_anexa_todas_as_linhas():
    """N ready-made files coming from the previous node fit in a single e-mail."""
    linhas = [
        {"n": "a.txt", "c": base64.b64encode(b"A").decode()},
        {"n": "b.txt", "c": base64.b64encode(b"B").decode()},
    ]
    plano = montar_plano(
        _cfg(anexos=[{"origem": "coluna", "coluna_nome": "n", "coluna_conteudo": "c"}]),
        dados=FonteFake(colunas=["n", "c"], linhas=linhas),
        entrada="no1",
    )
    assert [(a.nome, a.conteudo) for a in plano.mensagens[0].anexos] == [
        ("a.txt", b"A"),
        ("b.txt", b"B"),
    ]


def test_cco_fora_do_resumo_e_do_mime():
    plano = montar_plano(
        _cfg(cc="c@example.com", cco="hidden@example.com"), dados=FonteFake(), entrada="no1"
    )
    msg = plano.mensagens[0]
    assert msg.cco == ("hidden@example.com",)
    assert (
        "hidden@example.com" not in msg.destinatarios
    )  # snapshot/trail do not expose the blind copy
    mime = montar_mime({"remetente": "u@example.com"}, msg)
    assert "hidden@example.com" not in mime.as_string()  # nor does the MIME


def test_mime_neutraliza_injecao_de_cabecalho():
    plano = montar_plano(
        _cfg(assunto="Subject\r\nBcc: intruder@example.com"), dados=FonteFake(), entrada="no1"
    )
    mime = montar_mime({"remetente": "u@example.com"}, plano.mensagens[0])
    assert mime["Bcc"] is None
    assert "intruder@example.com" in mime["Subject"]  # stayed INSIDE the subject, on a single line


def test_mime_monta_html_com_anexos():
    plano = montar_plano(
        _cfg(anexos=[{"origem": "dados", "formato": "csv", "nome": "r.csv"}]),
        dados=FonteFake(),
        entrada="no1",
    )
    mime = montar_mime({"remetente": "u@example.com", "remetente_nome": "Acme"}, plano.mensagens[0])
    assert mime["From"] == "Acme <u@example.com>"
    tipos = [p.get_content_type() for p in mime.walk()]
    assert tipos == ["multipart/mixed", "text/html", "text/csv"]


def test_mime_sem_remetente_falha():
    plano = montar_plano(_cfg(), dados=FonteFake(), entrada="no1")
    with pytest.raises(ErroAplicacao) as exc:
        montar_mime({}, plano.mensagens[0])
    assert exc.value.codigo == "smtp_sem_remetente"


# ---------------- connection config ----------------


def test_validar_config_smtp_aceita_minima():
    assert validar_config({"host": "smtp.example.com"}) == []


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"host": "smtp://x.com"},
        {"host": "x.com:587"},
        {"host": "x.com", "porta": 0},
        {"host": "x.com", "seguranca": "magia"},
        {"host": "x.com", "tentativas": 99},
        {"host": "x.com", "usuario": "no-at-sign"},
    ],
)
def test_validar_config_smtp_recusa(config):
    assert validar_config(config), f"should reject: {config}"
