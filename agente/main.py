#!/usr/bin/env python3
"""Agente A2A e Host MCP — Central de Salas da Hill Valley Tech."""

from __future__ import annotations

import itertools
import json
import os
import re
import secrets
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# Conveniencia: se invocado com o python global e existir .venv local, reexecuta no .venv
if sys.prefix == sys.base_prefix:
    for _venv_dir in (
        Path(__file__).resolve().parent / ".venv",
        Path(__file__).resolve().parent.parent / ".venv",
    ):
        _venv_py = _venv_dir / "bin" / "python"
        if _venv_py.is_file():
            os.execv(str(_venv_py), [str(_venv_py), *sys.argv])

from mcp_types import (
    CLIENT_CAPABILITIES_META_KEY,
    CLIENT_INFO_META_KEY,
    PROTOCOL_VERSION_META_KEY,
)

MCP_PROTOCOL_VERSION = "2026-07-28"
CLIENT_INFO = {
    "name": "agente-central-de-salas",
    "version": "1.0.0",
}
CLIENT_CAPABILITIES = {
    "elicitation": {
        "form": {},
    }
}


PADRAO_TRACEPARENT = re.compile(
    r"^(?P<version>[0-9a-f]{2})-(?P<trace_id>[0-9a-f]{32})-(?P<span_id>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})$",
    re.IGNORECASE,
)


def propagar_traceparent(traceparent: str | None) -> str | None:
    """Preserva o trace-id do header W3C traceparent gerando um novo span-id para a chamada MCP.

    Descarta o header se estiver malformado segundo a especificacao W3C Trace Context.
    """
    if not traceparent or not isinstance(traceparent, str):
        return None
    m = PADRAO_TRACEPARENT.match(traceparent.strip())
    if not m:
        return None
    version = m.group("version").lower()
    trace_id = m.group("trace_id").lower()
    span_id = m.group("span_id").lower()
    flags = m.group("flags").lower()
    if version == "ff" or trace_id == "0" * 32 or span_id == "0" * 16:
        return None
    novo_span_id = secrets.token_hex(8)
    return f"{version}-{trace_id}-{novo_span_id}-{flags}"


HOSTS_LOCAIS_MCP = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


def _validar_porta(valor_raw: str, rotulo: str) -> int:
    try:
        porta = int(valor_raw)
    except ValueError as exc:
        raise RuntimeError(f"Porta {rotulo} invalida: {valor_raw!r}") from exc
    if not (1 <= porta <= 65535):
        raise RuntimeError(f"Porta {rotulo} fora do intervalo valido (1-65535): {porta}")
    return porta


def resolver_porta_mcp_ambiente() -> int:
    """Resolve a porta que o servidor MCP utilizaria com a configuracao atual de ambiente."""
    mcp_port_raw = os.environ.get("MCP_PORT", "").strip()
    port_raw = os.environ.get("PORT", "").strip()
    escolhida_raw = mcp_port_raw or port_raw or "7301"
    return _validar_porta(escolhida_raw, "MCP")


class ClienteMCP:
    """Host/Cliente MCP puro sobre Streamable HTTP stateless."""

    def __init__(self, mcp_url: str | None = None) -> None:
        url_configurada = (mcp_url or os.environ.get("MCP_URL") or "").strip()
        if url_configurada:
            self.mcp_url = url_configurada
        else:
            porta_mcp_padrao = resolver_porta_mcp_ambiente()
            self.mcp_url = f"http://localhost:{porta_mcp_padrao}/mcp"
        self._id_counter = itertools.count(1)
        self._lock = threading.Lock()
        self._tools_descobertas: dict[str, dict[str, Any]] | None = None
        self._politica_versao: str | None = None

    def _proximo_id(self) -> int:
        with self._lock:
            return next(self._id_counter)

    def _construir_meta(self, traceparent: str | None = None) -> dict[str, Any]:
        meta: dict[str, Any] = {
            PROTOCOL_VERSION_META_KEY: MCP_PROTOCOL_VERSION,
            CLIENT_INFO_META_KEY: CLIENT_INFO,
            CLIENT_CAPABILITIES_META_KEY: CLIENT_CAPABILITIES,
        }
        tp = propagar_traceparent(traceparent)
        if tp:
            meta["traceparent"] = tp
        return meta

    def enviar_rpc(
        self,
        method: str,
        params: dict[str, Any],
        mcp_name: str | None = None,
        traceparent: str | None = None,
    ) -> tuple[int, dict[str, Any], int]:
        """Envia uma requisicao JSON-RPC 2.0 ao servidor MCP com cabecalhos e _meta obrigatorios.

        Retorna (http_status, resposta_json, req_id).
        """
        req_id = self._proximo_id()
        params_completos = {
            **params,
            "_meta": self._construir_meta(traceparent),
        }
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params_completos,
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            "Mcp-Method": method,
        }
        if mcp_name:
            headers["Mcp-Name"] = mcp_name

        raw_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.mcp_url, data=raw_data, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp_bytes = resp.read()
                return resp.status, json.loads(resp_bytes.decode("utf-8")), req_id
        except urllib.error.HTTPError as err:
            err_bytes = err.read()
            try:
                err_json = json.loads(err_bytes.decode("utf-8"))
            except Exception:
                err_json = {"error": {"code": -32603, "message": err_bytes.decode("utf-8", errors="replace")}}
            return err.code, err_json, req_id

    def descobrir_tools(self, traceparent: str | None = None, forcar: bool = False) -> dict[str, dict[str, Any]]:
        """Executa tools/list em runtime antes da primeira chamada de ferramenta."""
        if self._tools_descobertas is not None and not forcar:
            return self._tools_descobertas

        status, resp, _ = self.enviar_rpc("tools/list", {}, traceparent=traceparent)
        if status != 200 or "error" in resp:
            raise RuntimeError(f"Falha em tools/list: status={status} resp={resp}")

        tools_list = (resp.get("result") or {}).get("tools") or []
        descobertas: dict[str, dict[str, Any]] = {}
        for t in tools_list:
            if isinstance(t, dict) and isinstance(t.get("name"), str):
                descobertas[t["name"]] = t

        self._tools_descobertas = descobertas
        return descobertas

    def obter_versao_politica(self, traceparent: str | None = None, forcar: bool = False) -> str:
        """Le o resource politica://uso via resources/read e extrai a versao declarada na 1a linha."""
        if self._politica_versao is not None and not forcar:
            return self._politica_versao

        uri = "politica://uso"
        status, resp, _ = self.enviar_rpc(
            "resources/read",
            {"uri": uri},
            mcp_name=uri,
            traceparent=traceparent,
        )
        if status != 200 or "error" in resp:
            raise RuntimeError(f"Falha ao ler resource {uri}: status={status} resp={resp}")

        contents = (resp.get("result") or {}).get("contents") or []
        texto = contents[0].get("text", "") if contents and isinstance(contents[0], dict) else ""
        primeira_linha = texto.splitlines()[0] if texto else ""
        versao = primeira_linha.split(":", 1)[1].strip() if ":" in primeira_linha else ""
        if not versao:
            raise RuntimeError(f"Nao foi possivel extrair a versao da politica de {uri}")

        self._politica_versao = versao
        return versao

    def chamar_tool(
        self,
        nome_tool: str,
        arguments: dict[str, Any],
        traceparent: str | None = None,
        input_responses: dict[str, Any] | None = None,
        request_state: str | None = None,
    ) -> tuple[int, dict[str, Any], int]:
        """Executa tools/call garantindo descoberta previa via tools/list.

        Nao intercepta input_required por callback: devolve a resposta crua para o agente gerenciar a Task.
        No retry, recebe input_responses e request_state opaco e emite um novo JSON-RPC id.
        """
        tools = self.descobrir_tools(traceparent=traceparent)
        if nome_tool not in tools:
            raise RuntimeError(f"Tool '{nome_tool}' nao encontrada na descoberta dinamica tools/list")
        if nome_tool == "reservar_sala":
            self.obter_versao_politica(traceparent=traceparent)

        params: dict[str, Any] = {
            "name": nome_tool,
            "arguments": arguments,
        }
        if input_responses is not None:
            params["inputResponses"] = input_responses
        if request_state is not None:
            params["requestState"] = request_state

        return self.enviar_rpc(
            "tools/call",
            params,
            mcp_name=nome_tool,
            traceparent=traceparent,
        )


ESTADOS_TERMINAIS = frozenset({
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
})

PADRAO_RESERVAR = re.compile(
    r"^\s*reservar"
    r"\s+sala=(?P<sala>\S+)"
    r"\s+inicio=(?P<inicio>\S+)"
    r"\s+fim=(?P<fim>\S+)"
    r"\s+responsavel=(?P<responsavel>.+?)\s*$"
)

PADRAO_ESCOLHA = re.compile(r"^\s*escolha=(?P<escolha>\S+)\s*$")


def parsear_comando_reserva(texto: str) -> dict[str, str] | None:
    """Extrai deterministamente {sala, inicio, fim, responsavel} do comando textual."""
    m = PADRAO_RESERVAR.match(texto)
    if not m:
        return None
    return {
        "sala": m.group("sala"),
        "inicio": m.group("inicio"),
        "fim": m.group("fim"),
        "responsavel": m.group("responsavel").strip(),
    }


def parsear_comando_escolha(texto: str) -> str | None:
    """Extrai deterministamente o valor de escolha=<valor>."""
    m = PADRAO_ESCOLHA.match(texto)
    if not m:
        return None
    return m.group("escolha").strip()


def _extrair_dados_input_required(result: dict[str, Any]) -> tuple[str, list[str], str]:
    """Extrai (chave_input, lista_alternativas, request_state) do resultado input_required do MCP."""
    input_requests = result.get("inputRequests") or {}
    if not isinstance(input_requests, dict) or not input_requests:
        raise ValueError("Resposta input_required sem inputRequests")
    chave_input = next(iter(input_requests))
    pedido = input_requests[chave_input] or {}
    params = pedido.get("params") or {}
    schema = params.get("requestedSchema") or {}
    props = schema.get("properties") or {}
    prop_sala = props.get("sala") or {}
    if "enum" in prop_sala and isinstance(prop_sala["enum"], list):
        alternativas = [str(x) for x in prop_sala["enum"]]
    elif "const" in prop_sala:
        alternativas = [str(prop_sala["const"])]
    else:
        alternativas = []
    if not alternativas:
        raise ValueError("Resposta input_required sem alternativas no requestedSchema")
    request_state = result.get("requestState")
    if not isinstance(request_state, str) or not request_state:
        raise ValueError("Resposta input_required sem requestState")
    return chave_input, alternativas, request_state


def criar_agent_card(url_a2a: str) -> dict[str, Any]:
    """Constroi o Agent Card A2A v1.0 publicado em /.well-known/agent-card.json."""
    return {
        "name": "Central de Salas",
        "description": "Reserva salas de reuniao da Hill Valley Tech.",
        "provider": {
            "organization": "Hill Valley Tech",
            "url": "https://hillvalley.example",
        },
        "version": "1.0.0",
        "supportedInterfaces": [
            {
                "url": url_a2a,
                "protocolBinding": "JSONRPC",
                "protocolVersion": "1.0",
            }
        ],
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "extendedAgentCard": False,
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "reservar-sala",
                "name": "Reservar sala",
                "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
                "tags": ["salas", "agenda"],
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain"],
                "examples": [
                    "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
                ],
            }
        ],
    }


def _extrair_texto_mensagem(message: dict[str, Any]) -> str:
    parts = message.get("parts") or []
    textos: list[str] = []
    for p in parts:
        if isinstance(p, dict) and isinstance(p.get("text"), str):
            textos.append(p["text"])
    return " ".join(textos).strip()


def _extrair_texto_mcp(result: dict[str, Any]) -> str:
    content = result.get("content") or []
    textos: list[str] = []
    for item in content:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            textos.append(item["text"])
    return " ".join(textos).strip()


def _criar_mensagem_agente(texto: str, task_id: str, context_id: str) -> dict[str, Any]:
    return {
        "messageId": f"msg-{secrets.token_hex(6)}",
        "role": "ROLE_AGENT",
        "parts": [{"text": texto}],
        "taskId": task_id,
        "contextId": context_id,
    }


class GerenciadorTasksA2A:
    """Gerencia o ciclo de vida das Tasks A2A em memoria e a coordenacao com o Host MCP."""

    def __init__(self, cliente_mcp: ClienteMCP) -> None:
        self.mcp = cliente_mcp
        self._lock = threading.Lock()
        self._tasks: dict[str, dict[str, Any]] = {}
        # Estado interno de MRTR por taskId (NUNCA serializado ou exposto ao cliente A2A)
        self._contextos_mrtr: dict[str, dict[str, Any]] = {}

    def obter_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None
            return json.loads(json.dumps(task))

    def _salvar_task(self, task: dict[str, Any], contexto_mrtr: dict[str, Any] | None = None, limpar_mrtr: bool = False) -> dict[str, Any]:
        with self._lock:
            task_id = task["id"]
            self._tasks[task_id] = task
            if limpar_mrtr:
                self._contextos_mrtr.pop(task_id, None)
            elif contexto_mrtr is not None:
                self._contextos_mrtr[task_id] = contexto_mrtr
            return json.loads(json.dumps(task))

    def _transitar_estado(
        self,
        task: dict[str, Any],
        novo_estado: str,
        texto_mensagem: str | None = None,
    ) -> None:
        status: dict[str, Any] = {"state": novo_estado}
        if texto_mensagem is not None:
            msg_agente = _criar_mensagem_agente(texto_mensagem, task["id"], task["contextId"])
            status["message"] = msg_agente
            task["history"].append(msg_agente)
        task["status"] = status

    def _concluir_reserva_na_task(
        self,
        task: dict[str, Any],
        dados: dict[str, Any],
        traceparent: str | None = None,
    ) -> None:
        versao_politica = self.mcp.obter_versao_politica(traceparent=traceparent)
        payload_artifact = {
            "reserva": dados.get("reserva"),
            "sala": dados.get("sala"),
            "inicio": dados.get("inicio"),
            "fim": dados.get("fim"),
            "responsavel": dados.get("responsavel"),
            "politica": versao_politica,
        }
        task["artifacts"].append(
            {
                "artifactId": f"art-{secrets.token_hex(6)}",
                "name": "reserva",
                "parts": [
                    {
                        "text": json.dumps(payload_artifact, ensure_ascii=False),
                    }
                ],
            }
        )
        texto_confirmacao = f"Reserva {dados.get('reserva')} confirmada na {dados.get('sala')}."
        self._transitar_estado(task, "TASK_STATE_COMPLETED", texto_confirmacao)

    def _processar_continuacao_mrtr(
        self,
        task_id_ref: str,
        message: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Retoma uma Task em TASK_STATE_INPUT_REQUIRED conectando a resposta A2A ao retry MRTR do MCP."""
        texto_continuacao = _extrair_texto_mensagem(message)
        escolha = parsear_comando_escolha(texto_continuacao)

        with self._lock:
            task_existente = self._tasks.get(task_id_ref)
            if not task_existente:
                return None, {"code": -32001, "message": f"Task inexistente: {task_id_ref}"}
            estado_atual = (task_existente.get("status") or {}).get("state", "")
            if estado_atual in ESTADOS_TERMINAIS:
                return None, {
                    "code": -32602,
                    "message": f"Task {task_id_ref} ja se encontra em estado terminal ({estado_atual})",
                }
            if estado_atual != "TASK_STATE_INPUT_REQUIRED":
                return None, {
                    "code": -32602,
                    "message": f"Task {task_id_ref} nao aguarda input ({estado_atual})",
                }
            task = json.loads(json.dumps(task_existente))
            ctx_mrtr = json.loads(json.dumps(self._contextos_mrtr.get(task_id_ref) or {}))

            msg_usuario: dict[str, Any] = {
                "messageId": message.get("messageId") or f"msg-{secrets.token_hex(6)}",
                "role": message.get("role") or "ROLE_USER",
                "parts": message.get("parts") or [],
                "taskId": task_id_ref,
            }
            task["history"].append(msg_usuario)

            alternativas: list[str] = ctx_mrtr.get("alternativas") or []
            chave_input: str = ctx_mrtr.get("chave_input") or ""
            request_state: str = ctx_mrtr.get("request_state") or ""
            argumentos_originais: dict[str, Any] = ctx_mrtr.get("argumentos") or {}
            traceparent_original: str | None = ctx_mrtr.get("traceparent")

            # Escolha fora do enum (ou formato invalido): mantem em TASK_STATE_INPUT_REQUIRED repetindo alternativas
            if escolha != "recusar" and (escolha is None or escolha not in alternativas):
                texto_alternativas = f"alternativas: {', '.join(alternativas)}"
                self._transitar_estado(task, "TASK_STATE_INPUT_REQUIRED", texto_alternativas)
                self._tasks[task_id_ref] = task
                return json.loads(json.dumps(task)), None

            # Reserva a Task em TASK_STATE_WORKING antes de liberar o lock para evitar race condition
            self._transitar_estado(task, "TASK_STATE_WORKING")
            self._tasks[task_id_ref] = json.loads(json.dumps(task))

        try:
            if escolha == "recusar":
                try:
                    _, resp_mcp, _ = self.mcp.chamar_tool(
                        "reservar_sala",
                        argumentos_originais,
                        traceparent=traceparent_original,
                        input_responses={chave_input: {"action": "decline"}},
                        request_state=request_state,
                    )
                except Exception as exc:
                    self._transitar_estado(task, "TASK_STATE_FAILED", str(exc))
                    return self._salvar_task(task, limpar_mrtr=True), None

                if "error" in resp_mcp:
                    msg_erro = (resp_mcp.get("error") or {}).get("message") or "Erro MCP no decline"
                    self._transitar_estado(task, "TASK_STATE_FAILED", msg_erro)
                    return self._salvar_task(task, limpar_mrtr=True), None

                result = resp_mcp.get("result") or {}
                if result.get("isError") is True:
                    texto_erro = _extrair_texto_mcp(result) or "Erro ao recusar reserva"
                    self._transitar_estado(task, "TASK_STATE_FAILED", texto_erro)
                    return self._salvar_task(task, limpar_mrtr=True), None

                dados_recusa = result.get("structuredContent") or {}
                if result.get("resultType") != "complete" or dados_recusa.get("reservado") is not False:
                    self._transitar_estado(task, "TASK_STATE_FAILED", "Resposta inesperada do servidor MCP ao recusar")
                    return self._salvar_task(task, limpar_mrtr=True), None

                self._transitar_estado(task, "TASK_STATE_CANCELED", "Reserva cancelada pelo solicitante.")
                return self._salvar_task(task, limpar_mrtr=True), None

            # escolha in alternativas
            try:
                _, resp_mcp, _ = self.mcp.chamar_tool(
                    "reservar_sala",
                    argumentos_originais,
                    traceparent=traceparent_original,
                    input_responses={
                        chave_input: {
                            "action": "accept",
                            "content": {"sala": escolha},
                        }
                    },
                    request_state=request_state,
                )
            except Exception as exc:
                self._transitar_estado(task, "TASK_STATE_FAILED", str(exc))
                return self._salvar_task(task, limpar_mrtr=True), None

            if "error" in resp_mcp:
                msg_erro = (resp_mcp.get("error") or {}).get("message") or "Erro MCP no retry"
                self._transitar_estado(task, "TASK_STATE_FAILED", msg_erro)
                return self._salvar_task(task, limpar_mrtr=True), None

            result = resp_mcp.get("result") or {}
            if result.get("isError") is True:
                texto_erro = _extrair_texto_mcp(result) or "Erro na confirmacao da alternativa"
                self._transitar_estado(task, "TASK_STATE_FAILED", texto_erro)
                return self._salvar_task(task, limpar_mrtr=True), None

            # Caso C: se a alternativa escolhida foi ocupada enquanto a Task estava pausada,
            # o servidor MCP pode emitir uma nova elicitation (input_required) com as novas alternativas.
            if result.get("resultType") == "input_required":
                try:
                    nova_chave, novas_alternativas, novo_request_state = _extrair_dados_input_required(result)
                except ValueError as exc:
                    self._transitar_estado(task, "TASK_STATE_FAILED", str(exc))
                    return self._salvar_task(task, limpar_mrtr=True), None

                with self._lock:
                    self._contextos_mrtr[task_id_ref] = {
                        "chave_input": nova_chave,
                        "alternativas": novas_alternativas,
                        "request_state": novo_request_state,
                        "argumentos": argumentos_originais,
                        "traceparent": traceparent_original,
                    }
                texto_alternativas = f"alternativas: {', '.join(novas_alternativas)}"
                self._transitar_estado(task, "TASK_STATE_INPUT_REQUIRED", texto_alternativas)
                return self._salvar_task(task, limpar_mrtr=False), None

            dados = result.get("structuredContent") or {}
            if result.get("resultType") != "complete" or not isinstance(dados, dict) or not dados.get("reserva"):
                self._transitar_estado(
                    task,
                    "TASK_STATE_FAILED",
                    f"Resposta inesperada no retry MRTR: resultType={result.get('resultType')}",
                )
                return self._salvar_task(task, limpar_mrtr=True), None

            self._concluir_reserva_na_task(task, dados, traceparent=traceparent_original)
            return self._salvar_task(task, limpar_mrtr=True), None
        except Exception as exc:
            # Garante que nenhuma excecao inesperada deixe a Task presa em TASK_STATE_WORKING
            self._transitar_estado(task, "TASK_STATE_FAILED", str(exc))
            return self._salvar_task(task, limpar_mrtr=True), None

    def processar_send_message(
        self,
        message: dict[str, Any],
        traceparent: str | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Processa SendMessage A2A. Retorna (task_dict, erro_rpc_dict)."""
        if not isinstance(message, dict):
            return None, {"code": -32602, "message": "Parametro 'message' invalido"}

        task_id_ref = message.get("taskId")
        if task_id_ref:
            return self._processar_continuacao_mrtr(task_id_ref, message)

        # Preserva apenas traceparent valido da requisicao original da Task
        traceparent_original = (
            traceparent.strip()
            if isinstance(traceparent, str) and propagar_traceparent(traceparent) is not None
            else None
        )

        # Nova Task: registra TASK_STATE_SUBMITTED e em seguida transita para TASK_STATE_WORKING
        task_id = f"task-{secrets.token_hex(6)}"
        context_id = message.get("contextId") or f"ctx-{secrets.token_hex(6)}"
        msg_usuario: dict[str, Any] = {
            "messageId": message.get("messageId") or f"msg-{secrets.token_hex(6)}",
            "role": message.get("role") or "ROLE_USER",
            "parts": message.get("parts") or [],
        }

        task: dict[str, Any] = {
            "id": task_id,
            "contextId": context_id,
            "status": {"state": "TASK_STATE_SUBMITTED"},
            "history": [msg_usuario],
            "artifacts": [],
        }
        self._salvar_task(json.loads(json.dumps(task)))
        self._transitar_estado(task, "TASK_STATE_WORKING")
        self._salvar_task(json.loads(json.dumps(task)))

        try:
            texto_pedido = _extrair_texto_mensagem(message)
            argumentos = parsear_comando_reserva(texto_pedido)
            if not argumentos:
                self._transitar_estado(
                    task,
                    "TASK_STATE_FAILED",
                    "Comando invalido. Formato esperado: reservar sala=<id> inicio=<iso> fim=<iso> responsavel=<nome>",
                )
                return self._salvar_task(task), None

            try:
                _, resp_mcp, _ = self.mcp.chamar_tool(
                    "reservar_sala",
                    argumentos,
                    traceparent=traceparent_original,
                )
            except Exception as exc:
                self._transitar_estado(task, "TASK_STATE_FAILED", str(exc))
                return self._salvar_task(task), None

            if "error" in resp_mcp:
                msg_erro = (resp_mcp.get("error") or {}).get("message") or "Erro MCP"
                self._transitar_estado(task, "TASK_STATE_FAILED", msg_erro)
                return self._salvar_task(task), None

            result = resp_mcp.get("result") or {}
            if result.get("isError") is True:
                texto_erro = _extrair_texto_mcp(result) or "Erro na execucao da tool"
                self._transitar_estado(task, "TASK_STATE_FAILED", texto_erro)
                return self._salvar_task(task), None

            if result.get("resultType") == "complete":
                dados = result.get("structuredContent") or {}
                self._concluir_reserva_na_task(task, dados, traceparent=traceparent_original)
                return self._salvar_task(task), None

            if result.get("resultType") == "input_required":
                chave_input, alternativas, request_state = _extrair_dados_input_required(result)
                texto_alternativas = f"alternativas: {', '.join(alternativas)}"
                self._transitar_estado(task, "TASK_STATE_INPUT_REQUIRED", texto_alternativas)
                contexto_mrtr = {
                    "chave_input": chave_input,
                    "alternativas": alternativas,
                    "request_state": request_state,
                    "argumentos": argumentos,
                    "traceparent": traceparent_original,
                }
                return self._salvar_task(task, contexto_mrtr=contexto_mrtr), None

            self._transitar_estado(
                task,
                "TASK_STATE_FAILED",
                f"resultType desconhecido: {result.get('resultType')}",
            )
            return self._salvar_task(task), None
        except Exception as exc:
            # Garante que nenhuma excecao inesperada deixe a Task presa em TASK_STATE_WORKING
            self._transitar_estado(task, "TASK_STATE_FAILED", str(exc))
            return self._salvar_task(task), None


class AgenteA2AHandler(BaseHTTPRequestHandler):
    """Handler HTTP para /.well-known/agent-card.json e /a2a."""

    gerenciador: GerenciadorTasksA2A
    agent_card: dict[str, Any]

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _enviar_json(self, status_code: int, payload: dict[str, Any]) -> None:
        corpo = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def _enviar_erro_rpc(self, status_code: int, req_id: Any, code: int, message: str) -> None:
        self._enviar_json(
            status_code,
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": code,
                    "message": message,
                },
            },
        )

    def do_GET(self) -> None:
        caminho = self.path.split("?", 1)[0]
        if caminho == "/.well-known/agent-card.json":
            self._enviar_json(200, self.agent_card)
            return
        self._enviar_json(404, {"error": "Rota nao encontrada"})

    def do_POST(self) -> None:
        caminho = self.path.split("?", 1)[0]
        if caminho != "/a2a":
            self._enviar_json(404, {"error": "Rota nao encontrada"})
            return

        tamanho = int(self.headers.get("Content-Length") or 0)
        bruto = self.rfile.read(tamanho)
        try:
            corpo = json.loads(bruto.decode("utf-8"))
        except Exception:
            self._enviar_erro_rpc(400, None, -32700, "Parse error")
            return

        if not isinstance(corpo, dict) or corpo.get("jsonrpc") != "2.0":
            self._enviar_erro_rpc(400, corpo.get("id") if isinstance(corpo, dict) else None, -32600, "Invalid Request")
            return

        req_id = corpo.get("id")
        metodo = corpo.get("method")
        params = corpo.get("params") or {}
        if not isinstance(params, dict):
            self._enviar_erro_rpc(400, req_id, -32602, "Invalid params")
            return

        traceparent = self.headers.get("traceparent")

        try:
            if metodo == "SendMessage":
                task, erro = self.gerenciador.processar_send_message(
                    params.get("message"),
                    traceparent=traceparent,
                )
                if erro is not None:
                    self._enviar_erro_rpc(200, req_id, erro["code"], erro["message"])
                    return
                self._enviar_json(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {"task": task},
                    },
                )
                return

            if metodo == "GetTask":
                task_id = params.get("id")
                if not isinstance(task_id, str) or not task_id:
                    self._enviar_erro_rpc(200, req_id, -32602, "Parametro 'id' obrigatorio em GetTask")
                    return
                task = self.gerenciador.obter_task(task_id)
                if not task:
                    self._enviar_erro_rpc(200, req_id, -32001, f"Task inexistente: {task_id}")
                    return
                self._enviar_json(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {"task": task},
                    },
                )
                return

            self._enviar_erro_rpc(200, req_id, -32601, f"Method not found: {metodo}")
        except Exception as exc:
            self._enviar_erro_rpc(500, req_id, -32603, f"Internal error: {exc}")


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def resolver_porta_a2a(mcp_url: str | None = None) -> int:
    """Resolve a porta do agente A2A e detecta colisao com servidor MCP local."""
    a2a_port_raw = os.environ.get("A2A_PORT", "").strip()
    port_raw = os.environ.get("PORT", "").strip()
    mcp_port_raw = os.environ.get("MCP_PORT", "").strip()

    escolhida_raw = a2a_port_raw or (port_raw if mcp_port_raw else "") or "7300"
    porta_a2a = _validar_porta(escolhida_raw, "A2A")

    url_efetiva = (mcp_url or os.environ.get("MCP_URL") or "").strip()
    if not url_efetiva:
        porta_mcp = resolver_porta_mcp_ambiente()
        url_efetiva = f"http://localhost:{porta_mcp}/mcp"

    parsed = urllib.parse.urlsplit(url_efetiva)
    host_mcp = (parsed.hostname or "").lower()
    if host_mcp in HOSTS_LOCAIS_MCP:
        try:
            porta_url = parsed.port
        except ValueError as exc:
            raise RuntimeError(f"Porta invalida em MCP_URL: {url_efetiva!r}") from exc
        if porta_url is None:
            porta_url = 443 if parsed.scheme == "https" else 80
        else:
            _validar_porta(str(porta_url), "MCP_URL")

        portas_mcp_locais = {porta_url}
        if mcp_port_raw:
            portas_mcp_locais.add(_validar_porta(mcp_port_raw, "MCP"))

        if porta_a2a in portas_mcp_locais:
            raise RuntimeError(
                f"Colisao de portas detectada: o agente A2A e o servidor MCP local nao podem usar a mesma porta ({porta_a2a}). "
                "Configure A2A_PORT (padrao 7300) e MCP_PORT (padrao 7301) com valores distintos."
            )

    return porta_a2a


def main() -> None:
    try:
        cliente_mcp = ClienteMCP()
        porta = resolver_porta_a2a(cliente_mcp.mcp_url)
    except RuntimeError as exc:
        print(f"[AGENTE] ERRO DE CONFIGURACAO: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)

    url_a2a = os.environ.get("A2A_URL") or f"http://localhost:{porta}/a2a"
    gerenciador = GerenciadorTasksA2A(cliente_mcp)

    AgenteA2AHandler.gerenciador = gerenciador
    AgenteA2AHandler.agent_card = criar_agent_card(url_a2a)

    try:
        servidor = ReusableThreadingHTTPServer(("0.0.0.0", porta), AgenteA2AHandler)
    except OSError as exc:
        print(
            f"[AGENTE] ERRO DE CONFIGURACAO: Nao foi possivel iniciar na porta {porta} ({exc}). "
            "Verifique se a porta ja esta em uso e utilize A2A_PORT (padrao 7300) e MCP_PORT (padrao 7301).",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)

    print(f"[AGENTE] Escutando em http://localhost:{porta} (MCP_URL={cliente_mcp.mcp_url})", file=sys.stderr, flush=True)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        servidor.server_close()


if __name__ == "__main__":
    main()


