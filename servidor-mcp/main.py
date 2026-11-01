#!/usr/bin/env python3
"""Servidor MCP (Streamable HTTP 2026-07-28) — Central de Salas da Hill Valley Tech.

Implementado sobre o SDK oficial do MCP v2 (`mcp==2.3.0`):
- `MCPServer` com transporte Streamable HTTP stateless (`2026-07-28`)
- `Resolve` + `Elicit` + `ElicitationResult` (`input_required` MRTR de primeira classe)
- `RequestStateSecurity` (AES-256-GCM + HKDF-SHA256 + request binding + TTL)
"""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

# Conveniencia: se invocado com o python global e existir .venv local, reexecuta no .venv
if sys.prefix == sys.base_prefix:
    for _venv_dir in (
        Path(__file__).resolve().parent / ".venv",
        Path(__file__).resolve().parent.parent / ".venv",
    ):
        _venv_py = _venv_dir / "bin" / "python"
        if _venv_py.is_file():
            os.execv(str(_venv_py), [str(_venv_py), *sys.argv])

import uvicorn
from collections.abc import Mapping
from mcp_types import INTERNAL_ERROR, INVALID_PARAMS, InputRequiredResult
from pydantic import BaseModel, Field, create_model
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.mcpserver import (
    AcceptedElicitation,
    CancelledElicitation,
    Context,
    DeclinedElicitation,
    Elicit,
    ElicitationResult,
    MCPServer,
    RequestStateSecurity,
    Resolve,
)
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError

FUSO_SP = timezone(timedelta(hours=-3))
REQUEST_STATE_TTL_SECONDS = 600.0  # 10 minutos (dentro da janela de 5 a 30 min)

ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"

BASE_DIR = Path(__file__).resolve().parent.parent
DADOS_DIR = BASE_DIR / "dados"


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


class EstadoDominio:
    """Armazena dados carregados na inicializacao do servidor e regras de dominio."""

    def __init__(self, dados_dir: Path) -> None:
        self._lock = threading.Lock()
        with open(dados_dir / "salas.json", encoding="utf-8") as f:
            self.salas: list[dict[str, Any]] = json.load(f)
        self.salas_por_id: dict[str, dict[str, Any]] = {s["id"]: s for s in self.salas}
        with open(dados_dir / "reservas.json", encoding="utf-8") as f:
            self.reservas: list[dict[str, Any]] = json.load(f)
        with open(dados_dir / "politica-de-uso.md", encoding="utf-8") as f:
            self.politica_texto: str = f.read()
        primeira_linha = self.politica_texto.splitlines()[0] if self.politica_texto else ""
        self.politica_versao: str = (
            primeira_linha.split(":", 1)[1].strip() if ":" in primeira_linha else "2026-11-01"
        )

    @staticmethod
    def parse_iso_sp(valor: str) -> datetime:
        """Converte string ISO 8601 para datetime com fuso de Sao Paulo (-03:00)."""
        if isinstance(valor, str) and valor.endswith("Z"):
            valor = valor[:-1] + "+00:00"
        dt = datetime.fromisoformat(valor)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=FUSO_SP)
        else:
            dt = dt.astimezone(FUSO_SP)
        return dt

    def validar_intervalo_e_sala(
        self, sala: str, inicio: str, fim: str
    ) -> tuple[str | None, datetime | None, datetime | None]:
        """Valida existencia da sala e regras da politica de uso.

        Retorna (mensagem_erro, dt_inicio_sp, dt_fim_sp).
        """
        if not sala or sala not in self.salas_por_id:
            return f"Sala inexistente: {sala}", None, None

        try:
            dt_inicio = self.parse_iso_sp(inicio)
            dt_fim = self.parse_iso_sp(fim)
        except (ValueError, TypeError):
            return ERRO_INTERVALO, None, None

        if dt_fim <= dt_inicio:
            return ERRO_INTERVALO, None, None

        janela_inicio = dt_inicio.replace(hour=8, minute=0, second=0, microsecond=0)
        janela_fim = dt_inicio.replace(hour=20, minute=0, second=0, microsecond=0)

        if (
            dt_inicio.date() != dt_fim.date()
            or dt_inicio < janela_inicio
            or dt_inicio >= janela_fim
            or dt_fim < janela_inicio
            or dt_fim > janela_fim
        ):
            return ERRO_JANELA, None, None

        if (dt_fim - dt_inicio) > timedelta(hours=2):
            return ERRO_DURACAO, None, None

        return None, dt_inicio, dt_fim

    def buscar_conflitos(
        self, sala: str, dt_inicio: datetime, dt_fim: datetime
    ) -> list[dict[str, Any]]:
        """Retorna lista de reservas conflitantes na sala informada."""
        conflitos: list[dict[str, Any]] = []
        with self._lock:
            for r in self.reservas:
                if r["sala"] != sala:
                    continue
                r_inicio = self.parse_iso_sp(r["inicio"])
                r_fim = self.parse_iso_sp(r["fim"])
                if dt_inicio < r_fim and r_inicio < dt_fim:
                    conflitos.append(
                        {
                            "id": r["id"],
                            "inicio": r["inicio"],
                            "fim": r["fim"],
                            "responsavel": r["responsavel"],
                        }
                    )
        return conflitos

    def criar_reserva(
        self, sala: str, inicio: str, fim: str, responsavel: str
    ) -> ReservaOut:
        """Persiste uma nova reserva em memoria e devolve o modelo ReservaOut."""
        with self._lock:
            novo_id = f"res-{len(self.reservas) + 1:04d}"
            nova_reserva = {
                "id": novo_id,
                "sala": sala,
                "inicio": inicio,
                "fim": fim,
                "responsavel": responsavel,
            }
            self.reservas.append(nova_reserva)
            return ReservaOut(
                reserva=novo_id,
                reservado=True,
                sala=sala,
                inicio=inicio,
                fim=fim,
                responsavel=responsavel,
                politica=self.politica_versao,
                motivo=None,
            )

    def calcular_alternativas(
        self, sala_pedida: str, dt_inicio: datetime, dt_fim: datetime
    ) -> list[str]:
        """Calcula ate 3 salas alternativas livres com capacidade >= a da sala pedida.

        Ordenadas por capacidade crescente e, em caso de empate, por id em ordem alfabetica.
        """
        sala_info = self.salas_por_id.get(sala_pedida)
        if not sala_info:
            return []
        cap_min = int(sala_info["capacidade"])
        candidatas: list[dict[str, Any]] = []
        for s in self.salas:
            sid = s["id"]
            if sid == sala_pedida:
                continue
            if int(s["capacidade"]) < cap_min:
                continue
            if len(self.buscar_conflitos(sid, dt_inicio, dt_fim)) == 0:
                candidatas.append(s)

        candidatas.sort(key=lambda x: (int(x["capacidade"]), str(x["id"])))
        return [str(s["id"]) for s in candidatas[:3]]


DOMINIO = EstadoDominio(DADOS_DIR)


def carregar_segredo() -> bytes:
    """Carrega e valida REQUEST_STATE_SECRET do ambiente (hexadecimal >= 64 caracteres / 32 bytes)."""
    raw = os.environ.get("REQUEST_STATE_SECRET", "").strip()
    if not raw:
        raise RuntimeError(
            "Variavel de ambiente REQUEST_STATE_SECRET ausente. "
            'Gere uma chave com: python3 -c "import secrets; print(secrets.token_hex(32))"'
        )
    if (
        len(raw) < 64
        or len(raw) % 2 != 0
        or not all(c in "0123456789abcdefABCDEF" for c in raw)
    ):
        raise RuntimeError(
            "REQUEST_STATE_SECRET invalido: deve ser hexadecimal valido com pelo menos "
            f"64 caracteres (32 bytes); recebido {len(raw)} caracteres. "
            'Gere uma chave com: python3 -c "import secrets; print(secrets.token_hex(32))"'
        )
    try:
        key = bytes.fromhex(raw)
    except ValueError as exc:
        raise RuntimeError(
            "REQUEST_STATE_SECRET invalido: deve conter exclusivamente caracteres hexadecimais. "
            'Gere uma chave com: python3 -c "import secrets; print(secrets.token_hex(32))"'
        ) from exc

    if len(key) < 32:
        raise RuntimeError(
            f"REQUEST_STATE_SECRET invalido: precisa ter no minimo 32 bytes (recebido {len(key)} bytes)."
        )
    return key


def _construir_elicit_salas(alternativas: list[str]) -> Elicit[BaseModel]:
    """Constroi o marcador Elicit com o enum exato das salas alternativas."""
    opcoes_literal = Literal[tuple(alternativas)]  # type: ignore[valid-type]
    modelo_escolha = create_model(
        "EscolhaDeSala",
        sala=(
            opcoes_literal,
            Field(title="Sala", description="Sala alternativa escolhida"),
        ),
    )
    return Elicit(
        message="A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.",
        schema=modelo_escolha,
    )


def _validar_lista_alternativas(alts: Any) -> list[str] | None:
    """Valida que `alts` e uma lista nao vazia de strings sem duplicatas e todas salas validas do dominio."""
    if not isinstance(alts, list) or len(alts) == 0:
        return None
    if not all(isinstance(x, str) and bool(x.strip()) for x in alts):
        return None
    if len(set(alts)) != len(alts):
        return None
    if not all(x in DOMINIO.salas_por_id for x in alts):
        return None
    return list(alts)


def _extrair_alternativas_do_request_state(request_state: str | None) -> list[str] | None:
    """Extrai e valida as alternativas seladas no request_state (ja autenticado pelo RequestStateBoundary)."""
    if request_state is None:
        return None
    if not isinstance(request_state, str) or not request_state.strip():
        print(
            "[MCP] ERRO: request_state ausente ou nao-string ao reconstruir estado MRTR.",
            file=sys.stderr,
            flush=True,
        )
        return None
    try:
        dados = json.loads(request_state)
    except (ValueError, TypeError) as exc:
        print(
            f"[MCP] ERRO: request_state nao e JSON valido ({exc}).",
            file=sys.stderr,
            flush=True,
        )
        return None
    if (
        not isinstance(dados, dict)
        or type(dados.get("v")) is not int
        or dados.get("v") != 3
        or not isinstance(dados.get("outcomes"), dict)
        or not isinstance(dados.get("asked"), dict)
        or len(dados["asked"]) != 1
    ):
        print(
            "[MCP] ERRO: request_state nao possui a estrutura interna esperada do mcp==2.3.0 (v=3, outcomes, asked).",
            file=sys.stderr,
            flush=True,
        )
        return None
    alts_validadas = _validar_lista_alternativas(dados.get("alternativas"))
    if alts_validadas is None:
        print(
            "[MCP] ERRO: campo 'alternativas' ausente ou invalido no request_state.",
            file=sys.stderr,
            flush=True,
        )
        return None
    return alts_validadas


def _extrair_alternativas_do_schema(schema: Any) -> list[str]:
    """Extrai e valida a lista de salas do requestedSchema de um inputRequest."""
    if not isinstance(schema, Mapping):
        return []
    props = schema.get("properties")
    if not isinstance(props, Mapping):
        return []
    campo_sala = props.get("sala")
    if not isinstance(campo_sala, Mapping):
        return []
    if "enum" in campo_sala:
        enum_val = campo_sala["enum"]
        if not isinstance(enum_val, list) or not all(isinstance(x, str) for x in enum_val):
            return []
        candidatas = list(enum_val)
    elif "const" in campo_sala:
        const_val = campo_sala["const"]
        if not isinstance(const_val, str):
            return []
        candidatas = [const_val]
    else:
        return []
    validadas = _validar_lista_alternativas(candidatas)
    return validadas if validadas is not None else []


def _falhar_enriquecimento_mrtr(motivo: str) -> Any:
    """Registra falha no stderr e aborta de forma fail-closed sem emitir input_required incompleto."""
    print(f"[MCP] ERRO (fail-closed MRTR): {motivo}", file=sys.stderr, flush=True)
    raise MCPError(
        code=INTERNAL_ERROR,
        message="Falha interna ao preparar estado de elicitation",
    )


async def preservar_alternativas_mrtr(
    ctx: ServerRequestContext[Any, Any],
    call_next: CallNext,
) -> HandlerResult:
    """Middleware que valida e registra as alternativas ofertadas no request_state antes da selagem AEAD."""
    result = await call_next(ctx)
    eh_modelo = isinstance(result, InputRequiredResult)
    eh_mapping_ir = isinstance(result, Mapping) and result.get("resultType") == "input_required"
    if not eh_modelo and not eh_mapping_ir:
        return result

    pedidos = result.input_requests if eh_modelo else result.get("inputRequests")  # type: ignore[union-attr]
    state_str = result.request_state if eh_modelo else result.get("requestState")  # type: ignore[union-attr]

    if not isinstance(pedidos, Mapping) or len(pedidos) != 1:
        _falhar_enriquecimento_mrtr(
            f"inputRequests deve conter exatamente 1 entrada (recebido: {len(pedidos) if isinstance(pedidos, Mapping) else type(pedidos).__name__})"
        )

    _, pedido = next(iter(pedidos.items()))
    if isinstance(pedido, Mapping):
        params = pedido.get("params")
        schema = params.get("requestedSchema") if isinstance(params, Mapping) else None
    else:
        params = getattr(pedido, "params", None)
        schema = getattr(params, "requested_schema", None)

    if not isinstance(schema, Mapping):
        _falhar_enriquecimento_mrtr("requestedSchema ausente ou invalido em inputRequests")

    alts = _extrair_alternativas_do_schema(schema)
    if not alts:
        _falhar_enriquecimento_mrtr("propriedade 'sala' sem enum/const valido de salas do dominio no requestedSchema")

    if not isinstance(state_str, str) or not state_str.strip():
        _falhar_enriquecimento_mrtr("requestState ausente ou vazio no resultado input_required")

    try:
        state_dict = json.loads(state_str)
    except (ValueError, TypeError) as exc:
        _falhar_enriquecimento_mrtr(f"requestState interno nao e JSON valido: {exc}")

    if (
        not isinstance(state_dict, dict)
        or type(state_dict.get("v")) is not int
        or state_dict.get("v") != 3
        or not isinstance(state_dict.get("outcomes"), dict)
        or not isinstance(state_dict.get("asked"), dict)
        or len(state_dict["asked"]) != 1
    ):
        _falhar_enriquecimento_mrtr("estrutura interna de requestState invalida antes do enriquecimento (esperado v=3)")

    novo_state_dict = {**state_dict, "alternativas": alts}
    novo_state_str = json.dumps(novo_state_dict, separators=(",", ":"))
    if _extrair_alternativas_do_request_state(novo_state_str) != alts:
        _falhar_enriquecimento_mrtr("validacao pos-atualizacao do requestState falhou")

    if eh_modelo:
        return result.model_copy(update={"request_state": novo_state_str})  # type: ignore[union-attr]
    return {**result, "requestState": novo_state_str}  # type: ignore[dict-item]


def escolha_de_sala(sala: str, inicio: str, fim: str, ctx: Context[Any, Any]) -> str | Elicit[BaseModel]:
    """Resolver MRTR para `reservar_sala`: valida regras da politica e emite Elicit em caso de conflito."""
    erro_msg, dt_inicio, dt_fim = DOMINIO.validar_intervalo_e_sala(sala, inicio, fim)
    if erro_msg or dt_inicio is None or dt_fim is None:
        raise ToolError(erro_msg or ERRO_INTERVALO)

    # Retomada MRTR: request_state ja foi autenticado e aberto pelo RequestStateBoundary
    if ctx.request_state is not None:
        alternativas_originais = _extrair_alternativas_do_request_state(ctx.request_state)
        if alternativas_originais is None:
            raise MCPError(
                code=INVALID_PARAMS,
                message="Invalid or expired requestState",
                data={"reason": "invalid_request_state"},
            )
        chave_asked = next(iter(json.loads(ctx.request_state)["asked"]))
        respostas = ctx.input_responses or {}
        resposta = (
            respostas.get(chave_asked)
            if isinstance(respostas, Mapping)
            else None
        )
        if resposta is not None:
            acao = getattr(resposta, "action", None)
            if acao is None and isinstance(resposta, Mapping):
                acao = resposta.get("action")

            # Caso A: Decline ou Cancel continua sendo recusa mesmo se a sala original
            # tiver ficado livre (ex.: apos restart) ou se as alternativas mudarem.
            if acao in ("decline", "cancel"):
                return _construir_elicit_salas(alternativas_originais)

            # Caso B e Caso C: Accept
            if acao == "accept":
                bruto_content = getattr(resposta, "content", None)
                if bruto_content is None and isinstance(resposta, Mapping):
                    bruto_content = resposta.get("content")
                conteudo = bruto_content if isinstance(bruto_content, Mapping) else {}
                sala_escolhida = conteudo.get("sala")
                # Valor fora do enum original: devolve o Elicit com as alternativas originais
                # para que a validacao de schema do SDK (mcp==2.3.0) rejeite a resposta com ToolError (isError=true).
                if not isinstance(sala_escolhida, str) or sala_escolhida not in alternativas_originais:
                    return _construir_elicit_salas(alternativas_originais)

                # Valida se a alternativa escolhida continua livre no momento da retomada
                if len(DOMINIO.buscar_conflitos(sala_escolhida, dt_inicio, dt_fim)) == 0:
                    return _construir_elicit_salas(alternativas_originais)

                # A alternativa escolhida foi ocupada entre a pausa e a retomada:
                # recalcula alternativas atuais para emitir nova elicitation ou erro.
                alternativas_atuais = DOMINIO.calcular_alternativas(sala, dt_inicio, dt_fim)
                if len(alternativas_atuais) == 0:
                    raise ToolError(ERRO_SEM_ALTERNATIVA)
                return _construir_elicit_salas(alternativas_atuais)

        alternativas_atuais = DOMINIO.calcular_alternativas(sala, dt_inicio, dt_fim)
        return _construir_elicit_salas(alternativas_atuais or alternativas_originais)

    conflitos = DOMINIO.buscar_conflitos(sala, dt_inicio, dt_fim)
    if len(conflitos) == 0:
        return sala

    alternativas = DOMINIO.calcular_alternativas(sala, dt_inicio, dt_fim)
    if len(alternativas) == 0:
        raise ToolError(ERRO_SEM_ALTERNATIVA)

    return _construir_elicit_salas(alternativas)


def criar_mcp_server(secret_bytes: bytes) -> MCPServer[Any]:
    """Instancia e registra tools e resources no MCPServer oficial (`mcp==2.3.0`)."""
    servidor = MCPServer(
        "central-de-salas",
        version="1.0.0",
        request_state_security=RequestStateSecurity(
            keys=[secret_bytes],
            ttl=REQUEST_STATE_TTL_SECONDS,
        ),
        middleware=[preservar_alternativas_mrtr],
    )

    @servidor.tool(description="Lista todas as salas com capacidade e recursos.")
    def listar_salas() -> ListaDeSalas:
        return ListaDeSalas(salas=[SalaOut(**s) for s in DOMINIO.salas])

    @servidor.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
    def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
        erro_msg, dt_inicio, dt_fim = DOMINIO.validar_intervalo_e_sala(sala, inicio, fim)
        if erro_msg or dt_inicio is None or dt_fim is None:
            raise ToolError(erro_msg or ERRO_INTERVALO)
        conflitos = DOMINIO.buscar_conflitos(sala, dt_inicio, dt_fim)
        return Disponibilidade(
            sala=sala,
            livre=len(conflitos) == 0,
            conflitos=[ConflitoOut(**c) for c in conflitos],
        )

    @servidor.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
    def reservar_sala(
        sala: str,
        inicio: str,
        fim: str,
        responsavel: str,
        escolha: Annotated[ElicitationResult[Any], Resolve(escolha_de_sala)],
    ) -> ReservaOut:
        if isinstance(escolha, (DeclinedElicitation, CancelledElicitation)):
            return ReservaOut(
                reserva=None,
                reservado=False,
                sala=None,
                inicio=None,
                fim=None,
                responsavel=None,
                politica=None,
                motivo="recusado",
            )
        if isinstance(escolha, AcceptedElicitation):
            dado = escolha.data
            sala_final = dado if isinstance(dado, str) else str(getattr(dado, "sala", sala))
            erro_msg, dt_inicio, dt_fim = DOMINIO.validar_intervalo_e_sala(sala_final, inicio, fim)
            if erro_msg or dt_inicio is None or dt_fim is None:
                raise ToolError(erro_msg or ERRO_INTERVALO)
            if len(DOMINIO.buscar_conflitos(sala_final, dt_inicio, dt_fim)) > 0:
                raise ToolError(ERRO_SEM_ALTERNATIVA)
            return DOMINIO.criar_reserva(sala_final, inicio, fim, responsavel)
        raise ToolError("Resultado de elicitation invalido")

    @servidor.resource(
        "politica://uso",
        name="Politica de Uso",
        description="Politica de uso das salas de reuniao da Hill Valley Tech.",
        mime_type="text/markdown",
    )
    def ler_politica_uso() -> str:
        return DOMINIO.politica_texto

    return servidor


class ModernMCPASGIApp:
    """Middleware ASGI sobre a API publica `MCPServer.streamable_http_app()`.

    Registra no stderr cada request recebido (method, id e traceparent) e repassa
    o corpo integralmente para o app Streamable HTTP oficial do SDK.
    """

    def __init__(self, mcp_server: MCPServer[Any], path: str = "/mcp") -> None:
        self.path = path.rstrip("/") or "/mcp"
        self.app: ASGIApp = mcp_server.streamable_http_app(
            streamable_http_path=self.path,
            json_response=True,
            stateless_http=True,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        # Coleta o corpo uma unica vez para logar method, id e traceparent no stderr
        chunks: list[bytes] = []
        more_body = True
        while more_body:
            msg = await receive()
            if msg["type"] == "http.request":
                chunks.append(msg.get("body", b""))
                more_body = msg.get("more_body", False)
            elif msg["type"] == "http.disconnect":
                more_body = False

        raw_body = b"".join(chunks)
        if raw_body:
            try:
                decoded = json.loads(raw_body)
                if isinstance(decoded, dict):
                    req_id = decoded.get("id")
                    method = decoded.get("method")
                    params = decoded.get("params") if isinstance(decoded.get("params"), dict) else {}
                    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
                    traceparent = meta.get("traceparent")
                    target = params.get("name") or params.get("uri") or ""
                    extra = f" target={target}" if target else ""
                    print(
                        f"[MCP] method={method}{extra} id={req_id} traceparent={traceparent or '-'}",
                        file=sys.stderr,
                        flush=True,
                    )
            except (ValueError, TypeError, UnicodeDecodeError):
                pass

        replayed = False

        async def replay_receive() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": raw_body, "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)


def resolver_porta_mcp() -> int:
    """Resolve a porta do servidor MCP com precedencia MCP_PORT > PORT > 7301 e detecta colisao com A2A."""
    mcp_port_raw = os.environ.get("MCP_PORT", "").strip()
    port_raw = os.environ.get("PORT", "").strip()
    a2a_port_raw = os.environ.get("A2A_PORT", "").strip()

    escolhida_raw = mcp_port_raw or port_raw or "7301"
    try:
        porta_mcp = int(escolhida_raw)
    except ValueError as exc:
        raise RuntimeError(f"Porta MCP invalida: {escolhida_raw!r}") from exc
    if not (1 <= porta_mcp <= 65535):
        raise RuntimeError(f"Porta MCP fora do intervalo valido (1-65535): {porta_mcp}")

    referencia_a2a_raw = a2a_port_raw or (port_raw if mcp_port_raw else "") or "7300"
    try:
        porta_a2a = int(referencia_a2a_raw)
    except ValueError as exc:
        raise RuntimeError(f"Porta A2A invalida: {referencia_a2a_raw!r}") from exc
    if not (1 <= porta_a2a <= 65535):
        raise RuntimeError(f"Porta A2A fora do intervalo valido (1-65535): {porta_a2a}")

    if porta_mcp == porta_a2a:
        raise RuntimeError(
            f"Colisao de portas detectada: o servidor MCP e o agente A2A nao podem usar a mesma porta ({porta_mcp}). "
            "Configure MCP_PORT (padrao 7301) e A2A_PORT (padrao 7300) com valores distintos."
        )
    return porta_mcp


def main() -> None:
    try:
        secret_bytes = carregar_segredo()
        port = resolver_porta_mcp()
    except RuntimeError as exc:
        print(f"[MCP] ERRO DE CONFIGURACAO: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)

    host = os.environ.get("HOST", "0.0.0.0")
    mcp_server = criar_mcp_server(secret_bytes)
    app: ASGIApp = ModernMCPASGIApp(mcp_server, path="/mcp")

    print(f"[MCP] Servidor MCP (SDK mcp v2) escutando em http://{host}:{port}/mcp", file=sys.stderr, flush=True)
    uvicorn.run(app, host=host, port=port, log_level="warning", ws="none")


if __name__ == "__main__":
    main()
