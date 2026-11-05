"""
API do agente Val — é isto que vai para o Azure.

    GET  /                -> o front (web/index.html), que consome a API abaixo
    POST /api/chat        -> conversa com o agente
    GET  /api/health      -> o app está de pé? (não chama o modelo; usado no deploy)
    GET  /api/info        -> qual versão está no ar: commit, build, modelo, prompt

A API não guarda conversa: o front manda o histórico a cada turno. Assim o
app não tem estado e qualquer instância (ou versão, no canário) pode atender.

Cada atendimento gera uma linha de log em JSON (só metadados, nunca o texto
da conversa), visível no log stream do App Service.

Rodar local:  uvicorn api:app --reload
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StringConstraints

import agent

RAIZ = Path(__file__).resolve().parent
WEB_DIR = RAIZ / "web"
VERSION_JSON = RAIZ / "version.json"

LIMITES = agent.CONFIG["limites"]
LIMITE_CLIENTE = int(LIMITES["max_caracteres_mensagem"])
# As respostas da Val voltam no histórico e podem passar do limite do cliente
# (500 tokens ~ 2.000 caracteres). Limite próprio, só para conter o tamanho do pedido.
LIMITE_ASSISTENTE = 4 * LIMITE_CLIENTE


def ler_versao() -> dict:
    """version.json é gerado pela esteira no build. Sem ele, estamos em desenvolvimento."""
    if VERSION_JSON.exists():
        return json.loads(VERSION_JSON.read_text(encoding="utf-8"))
    return {"versao": "dev", "commit": "local", "data_build": None, "repositorio": None, "execucao": None}


VERSAO = ler_versao()

app = FastAPI(
    title="Val — atendimento Nimbus Telecom",
    description="Agente de IA da disciplina Deploy e CI/CD (FIAP). Empresa fictícia.",
    version=str(VERSAO["versao"]),
)

# --------------------------------------------------------------------------
# Registro de cada atendimento — a matéria-prima da avaliação online.
# --------------------------------------------------------------------------

log = logging.getLogger("val.api")
if not log.handlers:
    _saida = logging.StreamHandler(sys.stdout)
    _saida.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(_saida)
    log.setLevel(logging.INFO)
    log.propagate = False


def _hash(valor: str | None) -> str | None:
    """Identifica sessão e IP no log sem guardar o valor em si."""
    return hashlib.sha256(valor.encode("utf-8")).hexdigest()[:12] if valor else None


def registrar(**campos) -> None:
    base = {"evento": "chat", "ambiente": os.getenv("AMBIENTE", "local"), "versao": VERSAO["versao"]}
    log.info(json.dumps({**base, **campos}, ensure_ascii=False))


# --------------------------------------------------------------------------
# Limite de requisições por IP — o site é público e cada pergunta custa
# chamada de modelo. Em memória, por instância: suficiente para uma instância
# do App Service. Com várias, o lugar disto é um gateway (API Management).
# --------------------------------------------------------------------------


class LimitadorPorIP:
    def __init__(self, por_minuto: int, limpar_a_cada: int = 200):
        self.por_minuto = por_minuto
        self._acessos: dict[str, deque[float]] = {}
        self._trava = threading.Lock()
        self._chamadas = 0
        self._limpar_a_cada = limpar_a_cada

    def permitir(self, ip: str, agora: float | None = None) -> bool:
        agora = time.monotonic() if agora is None else agora
        with self._trava:
            self._chamadas += 1
            if self._chamadas % self._limpar_a_cada == 0:
                self._esquecer_inativos(agora)
            fila = self._acessos.setdefault(ip, deque())
            while fila and agora - fila[0] > 60:
                fila.popleft()
            if len(fila) >= self.por_minuto:
                return False
            fila.append(agora)
            return True

    def _esquecer_inativos(self, agora: float) -> None:
        # Sem isto, cada IP novo (ou forjado) ficaria na memória para sempre.
        for ip in [ip for ip, fila in self._acessos.items() if not fila or agora - fila[-1] > 60]:
            del self._acessos[ip]

    def __len__(self) -> int:
        return len(self._acessos)


limitador = LimitadorPorIP(int(LIMITES["requisicoes_por_minuto_por_ip"]))


def _sem_porta(endereco: str) -> str:
    endereco = endereco.strip()
    if endereco.startswith("["):  # IPv6 com porta: "[2001:db8::1]:443"
        return endereco[1 : endereco.find("]")]
    if endereco.count(":") == 1:  # IPv4 com porta: "203.0.113.5:51234"
        return endereco.split(":")[0]
    return endereco


def ip_do_cliente(request: Request) -> str:
    """IP de quem fez o pedido, atrás do proxy do Azure.

    O cliente pode mandar o próprio X-Forwarded-For com qualquer valor; o
    proxy do App Service acrescenta o IP real no fim (ou substitui o
    cabeçalho). Nos dois casos, a última entrada é a confiável; a primeira é
    forjável. Isto vale para um único proxy na frente do app, que é o caso do
    App Service sem gateway.
    """
    encaminhado = request.headers.get("x-forwarded-for", "")
    if encaminhado.strip():
        return _sem_porta(encaminhado.split(",")[-1])
    return request.client.host if request.client else "desconhecido"


# --------------------------------------------------------------------------
# Contrato da API
# --------------------------------------------------------------------------

Texto = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Mensagem(BaseModel):
    # "system" não é aceito de propósito: o prompt de sistema é do servidor.
    role: Literal["user", "assistant"]
    content: Texto


class PedidoChat(BaseModel):
    mensagens: list[Mensagem] = Field(min_length=1)
    sessao_id: str | None = Field(default=None, max_length=100)


class Ferramenta(BaseModel):
    nome: str
    argumentos: dict
    resultado: dict


class RespostaChat(BaseModel):
    resposta: str
    ferramentas: list[Ferramenta]
    versao_prompt: str
    modelo: str
    latencia_ms: int
    tokens_entrada: int
    tokens_saida: int
    cortada: bool = False


@app.post("/api/chat", response_model=RespostaChat)
def chat(pedido: PedidoChat, request: Request) -> RespostaChat:
    if pedido.mensagens[-1].role != "user":
        raise HTTPException(422, "A última mensagem precisa ser do usuário.")

    # Corta o histórico ANTES de validar: o que ficou de fora não importa.
    historico = pedido.mensagens[-int(LIMITES["max_mensagens_historico"]) :]
    for m in historico:
        limite = LIMITE_CLIENTE if m.role == "user" else LIMITE_ASSISTENTE
        if len(m.content) > limite:
            raise HTTPException(422, f"Mensagem longa demais (máximo {limite} caracteres).")

    ip = ip_do_cliente(request)
    if not limitador.permitir(ip):
        registrar(status=429, ip=_hash(ip))
        raise HTTPException(429, "Muitas mensagens em pouco tempo. Espere um minuto e tente de novo.")

    versao_prompt = agent.escolher_prompt(pedido.sessao_id)
    rastro = agent.novo_rastro()
    inicio = time.perf_counter()
    try:
        mensagens = [{"role": "system", "content": agent.carregar_contexto(versao_prompt)}]
        mensagens += [{"role": m.role, "content": m.content} for m in historico]
        texto = agent.responder(mensagens, rastro=rastro)
    except FileNotFoundError as exc:  # config aponta para um prompt que não foi publicado
        registrar(status=503, erro="prompt_inexistente", versao_prompt=versao_prompt)
        raise HTTPException(503, "Configuração do agente inválida. A equipe foi avisada pelo log.") from exc
    except RuntimeError as exc:  # falta de chave: erro de configuração do servidor
        registrar(status=503, erro="sem_chave")
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — falha do provedor do modelo não derruba a API
        registrar(status=502, erro=type(exc).__name__, versao_prompt=versao_prompt)
        raise HTTPException(502, f"O modelo não respondeu agora. Tente de novo. ({type(exc).__name__})") from exc

    latencia_ms = int((time.perf_counter() - inicio) * 1000)
    precos = agent.CONFIG["precos_usd_por_milhao_tokens"]
    registrar(
        status=200,
        sessao=_hash(pedido.sessao_id),
        ip=_hash(ip),
        versao_prompt=versao_prompt,
        modelo=agent.MODELO,
        latencia_ms=latencia_ms,
        tokens_entrada=rastro["tokens_entrada"],
        tokens_saida=rastro["tokens_saida"],
        custo_usd=round(
            (rastro["tokens_entrada"] * precos["entrada"] + rastro["tokens_saida"] * precos["saida"]) / 1e6, 6
        ),
        ferramentas=[f["nome"] for f in rastro["ferramentas"]],
        chamadas_modelo=rastro["chamadas_modelo"],
        cortada=rastro["cortada"],
        vazia=rastro["vazia"],
        limite_iteracoes=rastro["limite_iteracoes"],
    )
    return RespostaChat(
        resposta=texto,
        ferramentas=[Ferramenta(**f) for f in rastro["ferramentas"]],
        versao_prompt=versao_prompt,
        modelo=agent.MODELO,
        latencia_ms=latencia_ms,
        tokens_entrada=rastro["tokens_entrada"],
        tokens_saida=rastro["tokens_saida"],
        cortada=rastro["cortada"],
    )


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/info")
def info() -> dict:
    prompt = agent.CONFIG["prompt"]
    return {
        "agente": agent.CONFIG["agente"],
        "ambiente": os.getenv("AMBIENTE", "local"),
        **VERSAO,
        "modelo": agent.MODELO,
        "prompt_estavel": prompt["estavel"],
        "prompt_canario": prompt.get("canario"),
        "pct_canario": prompt.get("pct_canario", 0),
        "chave_configurada": bool(os.getenv("OPENAI_API_KEY")),
    }


# --------------------------------------------------------------------------
# Front — arquivos estáticos em web/. Montado por último para não engolir /api.
# --------------------------------------------------------------------------


@app.get("/", include_in_schema=False)
def front() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


app.mount("/", StaticFiles(directory=WEB_DIR), name="web")
