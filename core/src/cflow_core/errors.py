"""Standard error envelope.

Every API error response uses this format and carries the request_id. Messages are in English;
they never contain a secret or a stack trace.
"""

from __future__ import annotations

from pydantic import BaseModel


class DetalheErro(BaseModel):
    campo: str
    mensagem: str


class CorpoErro(BaseModel):
    codigo: str
    mensagem: str
    request_id: str
    detalhes: list[DetalheErro] = []


class EnvelopeErro(BaseModel):
    """Root envelope: { "erro": { ... } }."""

    erro: CorpoErro


class ErroAplicacao(Exception):
    """Domain error that the edge converts into the standard envelope.

    `status` is the HTTP code; `codigo` is the stable machine-readable code.
    """

    def __init__(
        self,
        codigo: str,
        mensagem: str,
        status: int = 400,
        detalhes: list[DetalheErro] | None = None,
    ) -> None:
        super().__init__(mensagem)
        self.codigo = codigo
        self.mensagem = mensagem
        self.status = status
        self.detalhes = detalhes or []

    def envelope(self, request_id: str) -> EnvelopeErro:
        return EnvelopeErro(
            erro=CorpoErro(
                codigo=self.codigo,
                mensagem=self.mensagem,
                request_id=request_id,
                detalhes=self.detalhes,
            )
        )
