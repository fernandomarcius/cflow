"""Rerun inherits the `montante_publica` decision from the ROOT of the original chain.

The `sem_publicar` flag only existed at runtime, in the chained dispatch: the run does not store it.
The "Rerun" button on an upstream of an early-run chain (root `montante_publica: false`) ran the
upstream with the same parameters (the early as-of) and PUBLISHED - moving the `_atual.json` of the
dataset and of the summary that the cover reads as frozen. Exactly the effect the flag exists to
prevent, and with no warning on screen: the plan only listed external-effect nodes.

What is pinned, on both paths (workflow engine and inline):
- the decision comes from the ROOT of the original chain (`cadeia_raiz = true`), in the version it
  EXECUTED;
- rerunning the upstream of that chain runs with `sem_publicar` (whole or from a node);
- rerunning the root itself publishes (the flag concerns the upstream);
- the plan NAMES the outputs of the stretch and says whether they publish.
"""

from __future__ import annotations

import pytest

from . import test_montante_sem_publicar as _cenario
from ._arnes_auth import usuario_logado
from .conftest import aguardar_execucao
from .test_montante_sem_publicar import (
    DS_RAIZ,
    DS_W,
    _def_raiz,
    _def_w,
    _disparar,
    _modo,
    _publicar,
)

pytestmark = pytest.mark.integration

# The SAME fixtures as the `montante_publica` scenario (lake under tmp, default report spy): the
# rerun must be judged against the dispatch it redoes, not against a similar arrangement.
lake = _cenario.lake
relatorios_padrao = _cenario.relatorios_padrao


async def _reexecutar(cliente, h, exec_id: str, modo: str, **corpo) -> dict:
    r = await cliente.post(f"/execucoes/{exec_id}/reexecutar", json=corpo, headers=h)
    assert r.status_code == 200, r.text
    nova = r.json()["id"]
    if modo == "temporal":
        return await aguardar_execucao(cliente, nova, h)
    return (await cliente.get(f"/execucoes/{nova}", headers=h)).json()


async def _plano(cliente, h, exec_id: str, desde_no: str | None = None) -> dict:
    q = f"?desde_no={desde_no}" if desde_no else ""
    r = await cliente.get(f"/execucoes/{exec_id}/reexecucao/plano{q}", headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def _publicacoes(plano: dict) -> dict[str, dict]:
    return {p["no_id"]: p for p in plano["publicacoes"]}


@pytest.mark.parametrize("desde_no", [None, "out"])
@pytest.mark.parametrize("modo", ["temporal", "inline"])
async def test_reexecutar_montante_de_corrida_sem_publicar_nao_publica(
    owner_engine, cliente, lake, tmp_path, monkeypatch, relatorios_padrao, modo, desde_no
):
    _modo(modo, monkeypatch)
    _, h = await usuario_logado(
        owner_engine,
        cliente,
        email=f"rxm1{modo}{desde_no or ''}@example.com",
        perfil="especialista",
    )
    export = tmp_path / "export"
    w_id = await _publicar(cliente, h, "Upstream-mont", _def_w(str(export)))
    raiz_id = await _publicar(cliente, h, "root-mont", _def_raiz(w_id, montante_publica=False))
    raiz_det, w_exec = await _disparar(cliente, h, raiz_id, modo)
    assert raiz_det["status"] == "concluida", raiz_det
    assert not (lake / "gold" / "dev" / DS_W).exists()

    plano = await _plano(cliente, h, w_exec, desde_no)
    assert plano["sem_publicar"] is True
    assert "root-mont" in plano["sem_publicar_motivo"]
    pubs = _publicacoes(plano)
    esperadas = {"out"} if desde_no else {"out", "arq"}
    assert set(pubs) == esperadas
    assert pubs["out"] == {
        "no_id": "out",
        "destino": "lake",
        "alvo": DS_W,
        "camada": "gold",
        "publica": False,
    }

    det = await _reexecutar(
        cliente, h, w_exec, modo, **({"desde_no": desde_no} if desde_no else {})
    )

    assert det["status"] == "concluida", det
    assert det["id"] != w_exec
    # The finding: the rerun used to move `gold/load_mont_w/operacao_id=61/_atual.json`. It still has nothing.
    assert not (lake / "gold" / "dev" / DS_W).exists()
    assert not export.exists() or not any(export.iterdir())
    assert det["id"] not in relatorios_padrao


@pytest.mark.parametrize("modo", ["temporal", "inline"])
async def test_reexecutar_a_reexecucao_da_montante_continua_sem_publicar(
    owner_engine, cliente, lake, tmp_path, monkeypatch, relatorios_padrao, modo
):
    """The rerun is born OUTSIDE a chain (`cadeia_id` null). Rerunning it again - the operator
    insisting after a failure - climbs through `reexecucao_de` to the origin chain: the second
    attempt cannot reopen the publication that the first kept closed."""
    _modo(modo, monkeypatch)
    _, h = await usuario_logado(
        owner_engine, cliente, email=f"rxm4{modo}@example.com", perfil="especialista"
    )
    w_id = await _publicar(cliente, h, "Upstream-mont", _def_w(str(tmp_path / "export")))
    raiz_id = await _publicar(cliente, h, "root-mont", _def_raiz(w_id, montante_publica=False))
    _, w_exec = await _disparar(cliente, h, raiz_id, modo)
    primeira = await _reexecutar(cliente, h, w_exec, modo)
    assert primeira["status"] == "concluida", primeira

    plano = await _plano(cliente, h, primeira["id"])
    assert plano["sem_publicar"] is True
    segunda = await _reexecutar(cliente, h, primeira["id"], modo)

    assert segunda["status"] == "concluida", segunda
    assert not (lake / "gold" / "dev" / DS_W).exists()


@pytest.mark.parametrize("modo", ["temporal", "inline"])
async def test_reexecutar_a_raiz_da_corrida_publica(
    owner_engine, cliente, lake, tmp_path, monkeypatch, relatorios_padrao, modo
):
    """The flag concerns the UPSTREAM: the early-run root publishes its own, including on rerun."""
    _modo(modo, monkeypatch)
    _, h = await usuario_logado(
        owner_engine, cliente, email=f"rxm2{modo}@example.com", perfil="especialista"
    )
    w_id = await _publicar(cliente, h, "Upstream-mont", _def_w(str(tmp_path / "export")))
    raiz_id = await _publicar(cliente, h, "root-mont", _def_raiz(w_id, montante_publica=False))
    raiz_det, _ = await _disparar(cliente, h, raiz_id, modo)
    ponteiro = lake / "gold" / "dev" / DS_RAIZ / "_atual.json"
    antes = ponteiro.read_text()

    plano = await _plano(cliente, h, raiz_det["id"])
    assert plano["sem_publicar"] is False and plano["sem_publicar_motivo"] is None
    assert _publicacoes(plano)["pub"]["publica"] is True

    det = await _reexecutar(cliente, h, raiz_det["id"], modo)

    assert det["status"] == "concluida", det
    assert ponteiro.read_text() != antes and det["id"] in ponteiro.read_text()
    assert not (lake / "gold" / "dev" / DS_W).exists()


@pytest.mark.parametrize("modo", ["temporal", "inline"])
async def test_sem_a_marca_a_reexecucao_publica_e_o_plano_diz(
    owner_engine, cliente, lake, tmp_path, monkeypatch, relatorios_padrao, modo
):
    """Degeneration: a root without the key (every root that exists today). The upstream rerun
    publishes as always - and now the plan says WHERE, instead of `efeitos: []`."""
    _modo(modo, monkeypatch)
    _, h = await usuario_logado(
        owner_engine, cliente, email=f"rxm3{modo}@example.com", perfil="especialista"
    )
    export = tmp_path / "export"
    w_id = await _publicar(cliente, h, "Upstream-mont", _def_w(str(export)))
    raiz_id = await _publicar(cliente, h, "root-mont", _def_raiz(w_id))
    _, w_exec = await _disparar(cliente, h, raiz_id, modo)

    plano = await _plano(cliente, h, w_exec)
    assert plano["sem_publicar"] is False
    pubs = _publicacoes(plano)
    assert pubs["out"]["publica"] is True and pubs["out"]["alvo"] == DS_W
    assert pubs["arq"] == {
        "no_id": "arq",
        "destino": "arquivo",
        "alvo": "w_file",
        "camada": None,
        "publica": True,
    }

    det = await _reexecutar(cliente, h, w_exec, modo)

    assert det["status"] == "concluida", det
    escopo = (lake / "gold" / "dev" / DS_W / "operacao_id=61" / "_atual.json").read_text()
    assert det["id"] in escopo
    assert det["id"] in relatorios_padrao
