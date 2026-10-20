"""Single flight (`cflow_core.voo_unico`): per key, one job at a time; late arrivals wait and find
the first one's result. It removes duplicate extraction across sibling nodes in a chain and duplicate
downloads of a lake object."""

from __future__ import annotations

import asyncio

import pytest

from cflow_core.voo_unico import VooUnico


async def test_mesma_chave_um_de_cada_vez_e_quem_espera_acha_o_resultado():
    voo = VooUnico()
    cache: dict[str, str] = {}
    trabalhos: list[str] = []

    async def pedir(chave: str) -> str:
        async with voo.voo(chave):
            if chave in cache:
                return cache[chave]
            trabalhos.append(chave)
            await asyncio.sleep(0.01)
            cache[chave] = f"data for {chave}"
            return cache[chave]

    r = await asyncio.gather(pedir("x"), pedir("x"), pedir("x"), pedir("y"))
    assert r == ["data for x"] * 3 + ["data for y"]
    assert trabalhos == ["x", "y"], "the work for 'x' was repeated"
    assert len(voo) == 0, "entry left behind after everyone exited"


async def test_chaves_diferentes_nao_se_esperam():
    voo = VooUnico()
    dentro = 0
    pico = 0

    async def trabalho(chave):
        nonlocal dentro, pico
        async with voo.voo(chave):
            dentro += 1
            pico = max(pico, dentro)
            await asyncio.sleep(0.01)
            dentro -= 1

    await asyncio.gather(*(trabalho(i) for i in range(5)))
    assert pico == 5


async def test_falha_do_primeiro_solta_e_o_seguinte_tenta_de_novo():
    voo = VooUnico()
    tentativas = []

    async def trabalho(falhar: bool):
        async with voo.voo("k"):
            tentativas.append(falhar)
            await asyncio.sleep(0)
            if falhar:
                raise RuntimeError("extraction failed")

    r = await asyncio.gather(trabalho(True), trabalho(False), return_exceptions=True)
    assert isinstance(r[0], RuntimeError) and r[1] is None
    assert tentativas == [True, False]
    assert len(voo) == 0


async def test_cancelado_na_fila_nao_deixa_entrada_orfa():
    voo = VooUnico()
    await voo.entrar("k")
    esperando = asyncio.create_task(voo.entrar("k"))
    await asyncio.sleep(0)
    esperando.cancel()
    with pytest.raises(asyncio.CancelledError):
        await esperando
    voo.sair("k")
    assert len(voo) == 0
