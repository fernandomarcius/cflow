"""Unit: standard error envelope."""

from __future__ import annotations

from cflow_core.errors import DetalheErro, ErroAplicacao


def test_envelope_tem_formato_esperado():
    erro = ErroAplicacao(codigo="validacao", mensagem="Invalid field.", status=400)
    env = erro.envelope(request_id="req-1")
    dados = env.model_dump()
    assert set(dados) == {"erro"}
    assert dados["erro"]["codigo"] == "validacao"
    assert dados["erro"]["mensagem"] == "Invalid field."
    assert dados["erro"]["request_id"] == "req-1"
    assert dados["erro"]["detalhes"] == []


def test_envelope_com_detalhes():
    erro = ErroAplicacao(
        codigo="validacao",
        mensagem="Validation errors.",
        detalhes=[DetalheErro(campo="email", mensagem="required")],
    )
    dados = erro.envelope(request_id="r").model_dump()
    assert dados["erro"]["detalhes"] == [{"campo": "email", "mensagem": "required"}]


def test_status_padrao_400():
    assert ErroAplicacao(codigo="x", mensagem="y").status == 400
