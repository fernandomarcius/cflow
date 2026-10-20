"""`montante_publica: false` — o formato dos argumentos que chegam ao Temporal (achado raizes-1).

Retrocompatível com workflow em voo: sem a marca, os argumentos do filho e do nó são os MESMOS de
antes do campo existir, senão o replay de um histórico gravado antes dele diverge. As peças de
core (validação da chave, decisão pela raiz, `_publicar_saida`) estão em
`core/tests/test_montante_sem_publicar_puro.py`; estas moram com o worker porque testam
`cflow_worker.workflow`.
"""

from __future__ import annotations

# --- Temporal: argumentos retrocompatíveis ----------------------------------------------------


def test_args_do_filho_sem_marca_sao_os_de_sempre():
    """Workflow em voo (histórico gravado antes deste campo existir) tem de ver os MESMOS
    argumentos no replay: sem a marca, nada é acrescentado."""
    from cflow_worker.workflow import args_do_pipeline

    item = {"execucao_id": "e1", "definicao": {"nos": []}, "ordem": ["a"], "parametros": {"x": 1}}
    assert args_do_pipeline(item, {"p": "e1"}, None) == [
        "e1",
        {"nos": []},
        ["a"],
        {"x": 1},
        {"p": "e1"},
    ]


def test_args_do_filho_com_marca_leva_sem_publicar():
    from cflow_worker.workflow import args_do_pipeline

    item = {
        "execucao_id": "e1",
        "definicao": {"nos": []},
        "ordem": ["a"],
        "parametros": {"x": 1},
        "sem_publicar": True,
    }
    args = args_do_pipeline(item, {"p": "e1"}, None)
    assert args[5:] == [None, True]  # forcar_no=None, sem_publicar=True — posicional


def test_args_do_no_so_crescem_com_a_marca():
    from cflow_worker.workflow import args_do_no

    assert args_do_no("e", {}, "n", None, None, False, False) == ["e", {}, "n", None, None, False]
    assert args_do_no("e", {}, "n", None, None, False, True)[-1] is True
