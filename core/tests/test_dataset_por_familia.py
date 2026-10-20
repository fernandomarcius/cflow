"""The lake `dataset` accepts `/` to group by family - and what it does NOT accept.

Hundreds of sibling datasets at the root of the gold bucket were unreadable in the console, and the
hierarchy already existed inside the NAME (`ref05_summary`, `load_orders`). The `/` takes it out of
the name and puts it in the key.

The bucket has no folders: `/` is just part of the object key. That is why validation is what
prevents a malformed path - and it REFUSES instead of sanitising, which is the partition value rule:
silently sanitising makes two different datasets become the same path.
"""

from __future__ import annotations

import pytest

from cflow_core.pipelines import definicao


def _definicao(dataset: str, destino: str = "lake") -> dict:
    cfg = {"destino": destino, "dataset": dataset}
    if destino == "lake":
        cfg["camada"] = "gold"
    return {
        "nos": [
            {"id": "fonte", "tipo": "sql", "sql": "SELECT 1 AS x"},
            {"id": "saida", "tipo": "saida", "config": cfg},
        ],
        "arestas": [{"de": "fonte", "para": "saida"}],
    }


def _erros(dataset: str, destino: str = "lake") -> list[str]:
    try:
        definicao.validar(_definicao(dataset, destino))
    except Exception as e:  # ErroAplicacao with details
        return [
            d.mensagem if hasattr(d, "mensagem") else str(d) for d in getattr(e, "detalhes", [])
        ] or [str(e)]
    return []


@pytest.mark.parametrize("dataset", ["load_orders", "load/orders", "ref/05_summary", "a/b/c"])
def test_familia_e_aceita_ate_tres_niveis(dataset):
    assert _erros(dataset) == []


@pytest.mark.parametrize(
    "dataset",
    [
        "a/b/c/d",  # fourth level: a hierarchy nobody navigates
        "ref//05",  # an empty segment becomes `//` in the key
        "../load",  # climbs a directory
        "load/..",
        "/load",  # slash at the ends: a key with an empty segment
        "load/",
        "load/order items",  # space
    ],
)
def test_caminho_malformado_e_RECUSADO_nunca_sanitizado(dataset):
    # Silently sanitising would make two different datasets become the SAME path - and the second
    # would publish over the first with no error at all.
    assert _erros(dataset), f"{dataset!r} should be refused"


def test_no_ICEBERG_a_barra_continua_proibida():
    # There the dataset becomes a TABLE NAME in the catalog that query engines read.
    assert _erros("load/orders", destino="iceberg")


def test_a_fonte_publicado_usa_a_MESMA_regua():
    # Without this, a `ref//05` in the source would only fail at read time, with "dataset not
    # published" - the right error, with the cause hidden.
    d = {
        "nos": [
            {
                "id": "src",
                "tipo": "fonte",
                "config": {"origem": "publicado", "camada": "gold", "dataset": "ref//05"},
            }
        ],
        "arestas": [],
    }
    try:
        definicao.validar(d)
    except Exception as e:
        assert getattr(e, "detalhes", None)
    else:
        pytest.fail("a malformed dataset in the 'publicado' source should be refused")
