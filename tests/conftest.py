"""Peças compartilhadas pelos testes.

O dublê do cliente da OpenAI usa SimpleNamespace (como no agentebase da aula)
em vez de MagicMock: `MagicMock(name=...)` não cria o atributo `name`, e o
teste passaria pelo motivo errado.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))


def tool_call(call_id: str, nome: str, argumentos: dict | str):
    args = argumentos if isinstance(argumentos, str) else json.dumps(argumentos)
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=nome, arguments=args))


def mensagem(content=None, tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    msg.model_dump = lambda exclude_none=True: {
        "role": "assistant",
        "content": content,
        **({"tool_calls": [{"id": t.id} for t in tool_calls]} if tool_calls else {}),
    }
    return msg


def resposta(msg, tokens_entrada=100, tokens_saida=20):
    uso = SimpleNamespace(prompt_tokens=tokens_entrada, completion_tokens=tokens_saida)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=uso)


class ClienteFalso:
    """Devolve as respostas em ordem; a última se repete (simula 'nunca conclui')."""

    def __init__(self, respostas):
        self._respostas = list(respostas)
        self.chamadas = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.chamadas.append(kwargs)
        indice = min(len(self.chamadas) - 1, len(self._respostas) - 1)
        return self._respostas[indice]


@pytest.fixture(autouse=True)
def sem_modelo_de_verdade(monkeypatch):
    """Nenhum teste fala com a OpenAI. O agent.py carrega o .env ao ser
    importado; sem isto, um teste sem dublê gastaria chamada real na máquina
    de quem tem a chave — e passaria verde."""
    import agent

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(agent, "_cliente", None)


@pytest.fixture
def cliente_falso(monkeypatch):
    """Instala um ClienteFalso no agente: cliente_falso([resposta(...), ...])."""
    import agent

    def instalar(respostas):
        cliente = ClienteFalso(respostas)
        monkeypatch.setattr(agent, "get_cliente", lambda: cliente)
        return cliente

    return instalar
