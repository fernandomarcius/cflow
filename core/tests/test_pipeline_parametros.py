"""Declaration and validation of pipeline runtime parameters (pure logic, no database)."""

from __future__ import annotations

from datetime import date

import pytest

from cflow_core.errors import ErroAplicacao
from cflow_core.pipelines.definicao import (
    REDIGIDO,
    _competencia_janela,
    completar_declarados,
    filtrar_parametros_declarados,
    nome_parece_segredo,
    nomes_segredo,
    normalizar_parametros,
    parametros_declarados,
    redigir_segredos,
    validar,
)

# Minimal valid graph (1 node, no edges): isolates errors to the parameters.
BASE = {"nos": [{"id": "a", "tipo": "sql", "sql": "SELECT 1"}], "arestas": []}


def _com(params):
    return {**BASE, "parametros": params}


def test_sem_parametros_e_retrocompativel():
    # A definition without the "parametros" block remains valid (does not raise).
    validar(BASE)
    assert parametros_declarados(BASE) == []


def test_parametros_validos_passam():
    params = [
        {"nome": "operacao_id", "tipo": "inteiro", "obrigatorio": True},
        {"nome": "mes_ref", "tipo": "opcoes", "opcoes": ["atual", "anterior"], "default": "atual"},
        {"nome": "nome_operacao", "tipo": "texto", "obrigatorio": False},
        {"nome": "data_corte", "tipo": "data"},
    ]
    validar(_com(params))
    assert [p["nome"] for p in parametros_declarados(_com(params))] == [
        "operacao_id",
        "mes_ref",
        "nome_operacao",
        "data_corte",
    ]


def _erro_de(definicao) -> ErroAplicacao:
    with pytest.raises(ErroAplicacao) as exc:
        validar(definicao)
    assert exc.value.codigo == "grafo_invalido"
    return exc.value


def test_nome_invalido():
    e = _erro_de(_com([{"nome": "1op", "tipo": "texto"}]))  # starts with a digit
    assert any("'1op'" in d.mensagem for d in e.detalhes)


def test_sem_nome():
    e = _erro_de(_com([{"tipo": "texto"}]))
    assert any("None" in d.mensagem for d in e.detalhes)


def test_tipo_invalido():
    e = _erro_de(_com([{"nome": "x", "tipo": "booleano"}]))
    assert any("booleano" in d.mensagem for d in e.detalhes)


def test_opcoes_sem_lista():
    e = _erro_de(_com([{"nome": "x", "tipo": "opcoes"}]))
    assert any("'opcoes'" in d.mensagem for d in e.detalhes)


def test_default_fora_das_opcoes():
    e = _erro_de(_com([{"nome": "x", "tipo": "opcoes", "opcoes": ["a", "b"], "default": "c"}]))
    assert any("'x'" in d.mensagem for d in e.detalhes)


# ---- opcoes_fonte (dynamic options coming from a connection) ----


def test_opcoes_fonte_valida():
    # Static `opcoes` absent, but a well-formed `opcoes_fonte`: valid definition.
    validar(
        _com(
            [
                {
                    "nome": "operacao",
                    "tipo": "opcoes",
                    "opcoes_fonte": {"conexao_id": "c1", "coluna": "operacao"},
                }
            ]
        )
    )


def test_opcoes_fonte_formato_invalido():
    e = _erro_de(_com([{"nome": "x", "tipo": "opcoes", "opcoes_fonte": {"conexao_id": "c1"}}]))
    assert any("opcoes_fonte" in d.mensagem for d in e.detalhes)


def test_opcoes_fonte_origem_no_valida():
    # dynamic options from the output of a pipeline node.
    validar(
        _com(
            [
                {
                    "nome": "operacao",
                    "tipo": "opcoes",
                    "opcoes_fonte": {"origem": "no", "no_id": "lista", "coluna_valor": "opid"},
                }
            ]
        )
    )


def test_opcoes_fonte_origem_no_sem_no_id():
    e = _erro_de(
        _com(
            [{"nome": "x", "tipo": "opcoes", "opcoes_fonte": {"origem": "no", "coluna_valor": "v"}}]
        )
    )
    assert any("origem 'no'" in d.mensagem for d in e.detalhes)


def test_opcoes_estatica_e_fonte_sao_exclusivas():
    e = _erro_de(
        _com(
            [
                {
                    "nome": "x",
                    "tipo": "opcoes",
                    "opcoes": ["a"],
                    "opcoes_fonte": {"conexao_id": "c1", "coluna": "col"},
                }
            ]
        )
    )
    assert any("opcoes_fonte" in d.mensagem for d in e.detalhes)


def test_inteiro_default_nao_inteiro():
    e = _erro_de(_com([{"nome": "x", "tipo": "inteiro", "default": "dez"}]))
    assert any("'x'" in d.mensagem for d in e.detalhes)


def test_nomes_duplicados():
    e = _erro_de(_com([{"nome": "x", "tipo": "texto"}, {"nome": "x", "tipo": "inteiro"}]))
    assert any(d.campo == "parametros" for d in e.detalhes)


def test_parametros_formato_invalido():
    e = _erro_de({**BASE, "parametros": {"nao": "lista"}})
    assert any(d.campo == "parametros" for d in e.detalhes)


# ---- normalizar_parametros: validates VALUES against the declared ones ----

DECL = _com(
    [
        {"nome": "operacao_id", "tipo": "inteiro", "obrigatorio": True},
        {"nome": "mes_ref", "tipo": "opcoes", "opcoes": ["atual", "anterior"], "default": "atual"},
        {"nome": "rotulo", "tipo": "texto"},
    ]
)


def test_normalizar_aplica_default_e_coage_tipo():
    out = normalizar_parametros(DECL, {"operacao_id": "43"})
    assert out == {"operacao_id": 43, "mes_ref": "atual"}  # coerced integer + month default


def test_normalizar_sem_declaracao_e_sem_valores_vazio():
    assert normalizar_parametros(BASE, None) == {}
    assert normalizar_parametros(BASE, {}) == {}


# ---- completar_declarados: effective map to BIND in the SQL (declared without value -> None) ----


def test_completar_declarados_preenche_ausentes_com_none():
    # `normalizar_parametros` OMITS an optional parameter without a value; whoever binds needs the
    # key, otherwise `:rotulo` would stay literal in the SQL (syntax error) instead of "blank = all".
    norm = normalizar_parametros(DECL, {"operacao_id": 1})
    assert "rotulo" not in norm
    assert completar_declarados(DECL, norm) == {
        "operacao_id": 1,
        "mes_ref": "atual",
        "rotulo": None,
    }


def test_completar_declarados_nao_sobrescreve_valor():
    assert completar_declarados(DECL, {"rotulo": "x"})["rotulo"] == "x"


def test_completar_declarados_competencia_expande_a_janela():
    # `competencia` becomes :{nome}_inicio/:{nome}_fim; the raw name is not a bind.
    d = _com([{"nome": "c", "tipo": "competencia"}])
    assert completar_declarados(d, {}) == {"c_inicio": None, "c_fim": None}


def test_completar_declarados_sem_parametros():
    assert completar_declarados(BASE, None) == {}


# ---- "" == NO VALUE (the web UI sends "" for a blank filter; the editor omits it) ----


def test_normalizar_vazio_equivale_a_ausente():
    # Before: "" was bound as "" and the filter silently returned 0 rows (the preview, which already
    # treated "" as empty, showed everything), so the two clients diverged for the same empty field.
    assert normalizar_parametros(DECL, {"operacao_id": 1, "rotulo": ""}) == {
        "operacao_id": 1,
        "mes_ref": "atual",
    }


def test_normalizar_vazio_cai_no_default():
    assert normalizar_parametros(DECL, {"operacao_id": 1, "mes_ref": ""})["mes_ref"] == "atual"


def test_normalizar_vazio_em_obrigatorio_ainda_erra():
    # A blank scope is still a loud error; it does not become "return everything".
    e = _erro_norm(DECL, {"operacao_id": ""})
    assert any("operacao_id" in d.mensagem for d in e.detalhes)


def test_normalizar_vazio_em_opcoes_nao_e_mais_400():
    # "" is no longer checked against the options list (it used to be `parametros_invalidos`).
    d = _com([{"nome": "c", "tipo": "opcoes", "opcoes": ["a", "b"]}])
    assert normalizar_parametros(d, {"c": ""}) == {}


# ---- CREDENTIAL parameter: requires `segredo: true` and is redacted on persist ----


def test_nome_parece_segredo():
    for n in ("db_password", "password", "API_KEY", "meu_token", "credencial_x", "pwd"):
        assert nome_parece_segredo(n), n
    for n in ("operacao_id", "unit", "data_inicio", "competencia"):
        assert not nome_parece_segredo(n), n


def test_validar_exige_marca_segredo_em_nome_de_credencial():
    e = _erro_de(_com([{"nome": "db_password", "tipo": "texto"}]))
    assert any("segredo" in d.mensagem for d in e.detalhes)


def test_validar_aceita_credencial_marcada():
    validar(_com([{"nome": "db_password", "tipo": "texto", "segredo": True}]))


def test_validar_segredo_precisa_ser_booleano():
    e = _erro_de(_com([{"nome": "x", "tipo": "texto", "segredo": "sim"}]))
    assert any("true/false" in d.mensagem for d in e.detalhes)


def test_redigir_segredos():
    d = _com(
        [
            {"nome": "db_password", "tipo": "texto", "segredo": True},
            {"nome": "operacao_id", "tipo": "inteiro"},
        ]
    )
    assert nomes_segredo(d) == {"db_password"}
    saida = redigir_segredos(d, {"db_password": "p4ssw0rd", "operacao_id": 7})
    assert saida == {"db_password": REDIGIDO, "operacao_id": 7}
    assert "p4ssw0rd" not in str(saida)


def test_redigir_segredos_nao_muda_a_entrada():
    d = _com([{"nome": "token", "tipo": "texto", "segredo": True}])
    entrada = {"token": "abc"}
    redigir_segredos(d, entrada)
    assert entrada == {"token": "abc"}  # the real value remains available at runtime


def _erro_norm(definicao, valores) -> ErroAplicacao:
    with pytest.raises(ErroAplicacao) as exc:
        normalizar_parametros(definicao, valores)
    assert exc.value.codigo == "parametros_invalidos"
    return exc.value


def test_normalizar_obrigatorio_ausente():
    _erro_norm(DECL, {})  # required operacao_id missing


def test_normalizar_nao_declarado():
    e = _erro_norm(DECL, {"operacao_id": 1, "intruso": "x"})
    assert any("intruso" in d.mensagem for d in e.detalhes)


def test_normalizar_inteiro_invalido():
    e = _erro_norm(DECL, {"operacao_id": "abc"})
    assert any("operacao_id" in d.mensagem for d in e.detalhes)


def test_normalizar_opcao_invalida():
    e = _erro_norm(DECL, {"operacao_id": 1, "mes_ref": "trimestre"})
    assert any("mes_ref" in d.mensagem for d in e.detalhes)


# ---- competencia: a type that EXPANDS into :{nome}_inicio / :{nome}_fim (half-open window) ----

COMP = _com([{"nome": "c", "tipo": "competencia", "default": "anterior"}])


def test_competencia_e_tipo_valido():
    # A declaration of type "competencia" (with default "anterior") is valid and does not raise.
    validar(COMP)
    validar(_com([{"nome": "c", "tipo": "competencia", "default": "2026-05"}]))


def test_competencia_default_invalido_rejeitado_na_validacao():
    e = _erro_de(_com([{"nome": "c", "tipo": "competencia", "default": "2026-13"}]))
    assert any("'c'" in d.mensagem for d in e.detalhes)


def test_competencia_janela_ym_explicito():
    # "YYYY-MM" -> [1st of the month, 1st of the next month). December rolls over into January.
    assert _competencia_janela("2026-05") == ("2026-05-01", "2026-06-01")
    assert _competencia_janela("2026-12") == ("2026-12-01", "2027-01-01")


def test_competencia_janela_anterior_relativo_ao_mes_corrente():
    # "anterior" = the closed month before the current one (America/Sao_Paulo). Checked against
    # calendar arithmetic from today's date there, without depending on a fixed date.
    from cflow_core.timezone import FUSO_SAO_PAULO, agora_utc, para_exibicao

    hoje = para_exibicao(agora_utc(), FUSO_SAO_PAULO).date()
    ano, mes = (hoje.year - 1, 12) if hoje.month == 1 else (hoje.year, hoje.month - 1)
    inicio_esp = date(ano, mes, 1)
    fim_esp = date(ano + 1, 1, 1) if mes == 12 else date(ano, mes + 1, 1)
    assert _competencia_janela("anterior") == (inicio_esp.isoformat(), fim_esp.isoformat())


def test_normalizar_competencia_expande_em_duas_datas():
    # The value "2026-05" injects ONLY :c_inicio / :c_fim (not :c). Half-open window.
    out = normalizar_parametros(COMP, {"c": "2026-05"})
    assert out == {"c_inicio": "2026-05-01", "c_fim": "2026-06-01"}
    assert "c" not in out


def test_normalizar_competencia_default_anterior():
    # With no value it uses the default "anterior" and expands into the two boundary dates.
    out = normalizar_parametros(COMP, {})
    from cflow_core.timezone import FUSO_SAO_PAULO, agora_utc, para_exibicao

    hoje = para_exibicao(agora_utc(), FUSO_SAO_PAULO).date()
    ano, mes = (hoje.year - 1, 12) if hoje.month == 1 else (hoje.year, hoje.month - 1)
    esperado_inicio = date(ano, mes, 1).isoformat()
    assert out["c_inicio"] == esperado_inicio
    assert set(out) == {"c_inicio", "c_fim"}


def test_normalizar_competencia_valor_invalido():
    e = _erro_norm(COMP, {"c": "maio/2026"})
    assert any("'c'" in d.mensagem for d in e.detalhes)


# ---- as_of: SYSTEM-time cutoff (data_registro) -> :{nome} = inclusive timestamp ----

ASOF = _com([{"nome": "data_registro", "tipo": "as_of", "default": "hoje"}])


def test_as_of_e_tipo_valido():
    validar(ASOF)
    validar(_com([{"nome": "dr", "tipo": "as_of", "default": "2026-05"}]))
    validar(_com([{"nome": "dr", "tipo": "as_of", "default": "2026-05-15 14:30:00"}]))


def test_as_of_default_invalido_rejeitado():
    e = _erro_de(_com([{"nome": "dr", "tipo": "as_of", "default": "trimestre"}]))
    assert any("as_of" in d.mensagem for d in e.detalhes)


def test_normalizar_as_of_competencia_vira_fim_do_mes():
    # Clean cutoff: as-of = "2026-05" -> last instant of May (injects a single :data_registro).
    out = normalizar_parametros(ASOF, {"data_registro": "2026-05"})
    assert out == {"data_registro": "2026-05-31 23:59:59"}


def test_normalizar_as_of_timestamp_literal_intradia():
    out = normalizar_parametros(ASOF, {"data_registro": "2026-06-23 14:30:00"})
    assert out == {"data_registro": "2026-06-23 14:30:00"}


def test_normalizar_as_of_default_hoje_resolve():
    # With no value the default "hoje" resolves to the last instant of today (Sao Paulo).
    out = normalizar_parametros(ASOF, {})
    assert out["data_registro"].endswith("23:59:59")
    assert set(out) == {"data_registro"}


# ---- DERIVED parameter (unified form): `derivacao` lives in the parameter definition ----

DER_OK = {
    "de": "operacao_id",
    "mapa": "Operacoes",
    "campo_id": "OperationID",
    "campo_valor": "SystemName",
    "normalizar": "sistema",
}


def test_derivacao_valida_passa():
    validar(
        _com(
            [
                {"nome": "operacao_id", "tipo": "inteiro", "obrigatorio": True},
                {"nome": "sistema", "tipo": "texto", "derivacao": DER_OK},
            ]
        )
    )


def test_derivacao_com_juntar_passa():
    der = {
        "de": "operacao_id",
        "mapa": "Projects",
        "campo_id": "OpID",
        "campo_valor": "Project",
        "juntar": ",",
    }
    validar(
        _com(
            [
                {"nome": "operacao_id", "tipo": "inteiro"},
                {"nome": "projects", "tipo": "texto", "derivacao": der},
            ]
        )
    )


def test_derivacao_sem_campos_obrigatorios():
    e = _erro_de(_com([{"nome": "x", "tipo": "texto", "derivacao": {"de": "y"}}]))
    assert any("'mapa'" in d.mensagem for d in e.detalhes)
    assert any("'campo_id'" in d.mensagem for d in e.detalhes)
    assert any("'campo_valor'" in d.mensagem for d in e.detalhes)


def test_derivacao_nao_objeto():
    e = _erro_de(_com([{"nome": "x", "tipo": "texto", "derivacao": "nao-objeto"}]))
    assert any("x" in d.mensagem for d in e.detalhes)


def test_derivacao_normalizar_invalido():
    der = {**DER_OK, "normalizar": "outra-coisa"}
    e = _erro_de(_com([{"nome": "x", "tipo": "texto", "derivacao": der}]))
    assert any("'normalizar'" in d.mensagem for d in e.detalhes)


def test_normalizar_derivado_nao_e_cobrado_do_operador():
    # A derived parameter marked required is NOT reported as "missing"; the web UI computes it.
    definicao = _com(
        [
            {"nome": "operacao_id", "tipo": "inteiro", "obrigatorio": True},
            {"nome": "sistema", "tipo": "texto", "obrigatorio": True, "derivacao": DER_OK},
        ]
    )
    out = normalizar_parametros(definicao, {"operacao_id": "43"})
    assert out == {"operacao_id": 43}  # sistema is NOT included (the web UI will supply it)


def test_normalizar_derivado_aceita_valor_quando_enviado():
    # When the web UI sends the computed value, it is accepted and normalized as text.
    definicao = _com(
        [
            {"nome": "operacao_id", "tipo": "inteiro", "obrigatorio": True},
            {"nome": "sistema", "tipo": "texto", "derivacao": DER_OK},
        ]
    )
    out = normalizar_parametros(definicao, {"operacao_id": 43, "sistema": "alpha"})
    assert out == {"operacao_id": 43, "sistema": "alpha"}


# ---- Origin of the derivation map: arquivo (default) / fonte / no ----


def test_derivacao_origem_arquivo_explicita_passa():
    der = {**DER_OK, "origem": "arquivo"}
    validar(
        _com(
            [
                {"nome": "operacao_id", "tipo": "inteiro"},
                {"nome": "sistema", "tipo": "texto", "derivacao": der},
            ]
        )
    )


def test_derivacao_origem_fonte_passa():
    der = {
        "origem": "fonte",
        "de": "operacao_id",
        "conexao_id": "3f6b0e2a-0000-4000-8000-000000000001",
        "sql": "SELECT id, sistema FROM mapa WHERE ativo = 1",
        "campo_id": "id",
        "campo_valor": "sistema",
    }
    validar(
        _com(
            [
                {"nome": "operacao_id", "tipo": "inteiro"},
                {"nome": "sistema", "tipo": "texto", "derivacao": der},
            ]
        )
    )


def test_derivacao_origem_fonte_sem_conexao():
    der = {"origem": "fonte", "de": "operacao_id", "campo_id": "id", "campo_valor": "sistema"}
    e = _erro_de(_com([{"nome": "sistema", "tipo": "texto", "derivacao": der}]))
    assert any("'conexao_id'" in d.mensagem for d in e.detalhes)


def test_derivacao_origem_fonte_sql_nao_texto():
    der = {
        "origem": "fonte",
        "de": "operacao_id",
        "conexao_id": "x",
        "sql": 123,
        "campo_id": "id",
        "campo_valor": "sistema",
    }
    e = _erro_de(_com([{"nome": "sistema", "tipo": "texto", "derivacao": der}]))
    assert any("'sql'" in d.mensagem for d in e.detalhes)


def test_derivacao_origem_no_passa():
    der = {
        "origem": "no",
        "de": "operacao_id",
        "no_id": "filtro_1",
        "campo_id": "id",
        "campo_valor": "sistema",
    }
    validar(
        _com(
            [
                {"nome": "operacao_id", "tipo": "inteiro"},
                {"nome": "sistema", "tipo": "texto", "derivacao": der},
            ]
        )
    )


def test_derivacao_origem_no_sem_no_id():
    der = {"origem": "no", "de": "operacao_id", "campo_id": "id", "campo_valor": "sistema"}
    e = _erro_de(_com([{"nome": "sistema", "tipo": "texto", "derivacao": der}]))
    assert any("'no_id'" in d.mensagem for d in e.detalhes)


def test_derivacao_origem_invalida():
    der = {**DER_OK, "origem": "banco"}
    e = _erro_de(_com([{"nome": "sistema", "tipo": "texto", "derivacao": der}]))
    assert any("'origem'" in d.mensagem for d in e.detalhes)


def test_normalizar_opcoes_fonte_e_lenient():
    # With `opcoes_fonte` the backend does not know the options (resolved on the client), so it
    # accepts the value as text without checking membership in a static list.
    decl = _com(
        [
            {
                "nome": "operacao",
                "tipo": "opcoes",
                "opcoes_fonte": {"conexao_id": "c1", "coluna": "operacao"},
            }
        ]
    )
    assert normalizar_parametros(decl, {"operacao": "Acme Plaza"}) == {"operacao": "Acme Plaza"}


def test_filtrar_parametros_declarados_recorta_subconjunto():
    # Pipeline DAG: an upstream pipeline declares a subset of the root's parameters (the root also
    # has `competencia`). The filter hands over only what it declares, avoiding a 400
    # parametros_invalidos on normalization. Afterwards normalization accepts the subset.
    crc = _com(
        [
            {"nome": "operacao_id", "tipo": "texto", "obrigatorio": True},
            {"nome": "data_registro", "tipo": "as_of", "default": "hoje"},
        ]
    )
    todos = {"operacao_id": "63", "data_registro": "2026-07-08", "competencia": "2021-12"}
    recorte = filtrar_parametros_declarados(crc, todos)
    assert recorte == {"operacao_id": "63", "data_registro": "2026-07-08"}
    # the subset passes normalizar_parametros without error (the raw dict would raise parametros_invalidos)
    normalizar_parametros(crc, recorte)
    with pytest.raises(ErroAplicacao):
        normalizar_parametros(crc, todos)


def test_filtrar_parametros_declarados_vazio_ou_sem_declaracao():
    assert filtrar_parametros_declarados(_com([{"nome": "x", "tipo": "texto"}]), None) == {}
    assert filtrar_parametros_declarados(BASE, {"x": "1"}) == {}
