"""api_query connector (SQL-over-REST gateway): registration + contract. No network."""

from __future__ import annotations

import pytest

from cflow_core.conexoes.conectores import get_conector
from cflow_core.conexoes.conectores.api_query import ConectorApiQuery
from cflow_core.conexoes.service import TIPOS
from cflow_core.errors import ErroAplicacao


def test_tipo_registrado():
    assert "api_query" in TIPOS
    assert isinstance(get_conector("api_query"), ConectorApiQuery)


async def test_schema_e_colunas_nao_aplicavel():
    c = ConectorApiQuery()
    for coro in (c.schemas_tabelas({}, None), c.colunas({}, None, "s", "t")):
        with pytest.raises(ErroAplicacao) as exc:
            await coro
        assert exc.value.codigo == "nao_aplicavel"
