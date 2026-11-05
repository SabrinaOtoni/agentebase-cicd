"""Run-level: o loop responder() inteiro, com o modelo trocado por um dublê.

Testa as decisões do agente (chamar ferramenta, usar o resultado, parar) sem
gastar chamada real e sem depender de o modelo responder sempre igual.
"""

import json

import pytest
from conftest import mensagem, resposta, tool_call

import agent

pytestmark = pytest.mark.run_level


def test_resposta_direta_nao_chama_ferramenta(cliente_falso):
    cliente = cliente_falso([resposta(mensagem(content="Bom dia, sou a Val."))])
    mensagens = [{"role": "user", "content": "oi"}]

    assert agent.responder(mensagens) == "Bom dia, sou a Val."
    assert len(cliente.chamadas) == 1
    assert not any(m.get("role") == "tool" for m in mensagens)


def test_consulta_a_base_e_usa_o_resultado(cliente_falso):
    chamada = tool_call("c1", "consultar_base", {"pergunta": "planos"})
    cliente = cliente_falso(
        [resposta(mensagem(tool_calls=[chamada])), resposta(mensagem(content="O 500 Mega custa R$ 129,90."))]
    )
    mensagens = [{"role": "user", "content": "quais planos?"}]
    rastro = agent.novo_rastro()

    assert agent.responder(mensagens, rastro=rastro) == "O 500 Mega custa R$ 129,90."
    assert len(cliente.chamadas) == 2
    tool = next(m for m in mensagens if m.get("role") == "tool")
    assert tool["tool_call_id"] == "c1"
    assert json.loads(tool["content"])["artigos"][0]["id"] == "planos"
    assert [f["nome"] for f in rastro["ferramentas"]] == ["consultar_base"]


def test_varias_ferramentas_na_mesma_rodada(cliente_falso):
    chamadas = [
        tool_call("a", "consultar_base", {"pergunta": "cancelar multa"}),
        tool_call("b", "abrir_chamado", {"motivo": "cancelamento", "resumo": "quer cancelar"}),
    ]
    cliente_falso([resposta(mensagem(tool_calls=chamadas)), resposta(mensagem(content="Protocolo aberto."))])
    rastro = agent.novo_rastro()

    agent.responder([{"role": "user", "content": "quero cancelar"}], rastro=rastro)

    assert [f["nome"] for f in rastro["ferramentas"]] == ["consultar_base", "abrir_chamado"]
    assert rastro["ferramentas"][1]["resultado"]["protocolo"].startswith("NIM-")


def test_argumentos_json_quebrados_nao_derrubam_o_loop(cliente_falso):
    cliente_falso(
        [resposta(mensagem(tool_calls=[tool_call("x", "somar", "{não é json")])), resposta(mensagem(content="ok"))]
    )
    mensagens = [{"role": "user", "content": "soma"}]

    assert agent.responder(mensagens) == "ok"
    tool = next(m for m in mensagens if m.get("role") == "tool")
    assert "erro" in json.loads(tool["content"])


def test_para_no_limite_de_iteracoes(cliente_falso):
    sempre_ferramenta = resposta(mensagem(tool_calls=[tool_call("l", "somar", {"a": 1, "b": 1})]))
    cliente = cliente_falso([sempre_ferramenta])

    resultado = agent.responder([{"role": "user", "content": "..."}])

    assert "limite de iterações" in resultado.lower()
    assert len(cliente.chamadas) == agent.MAX_ITERACOES


def test_rastro_soma_tokens_de_todas_as_chamadas(cliente_falso):
    cliente_falso(
        [
            resposta(mensagem(tool_calls=[tool_call("c", "somar", {"a": 1, "b": 2})]), 100, 10),
            resposta(mensagem(content="3"), 150, 5),
        ]
    )
    rastro = agent.novo_rastro()
    agent.responder([{"role": "user", "content": "1+2"}], rastro=rastro)
    assert (rastro["tokens_entrada"], rastro["tokens_saida"], rastro["chamadas_modelo"]) == (250, 15, 2)


def test_envia_ao_modelo_a_configuracao_do_git(cliente_falso):
    cliente = cliente_falso([resposta(mensagem(content="ok"))])
    agent.responder([{"role": "user", "content": "oi"}])
    enviado = cliente.chamadas[0]
    assert enviado["model"] == agent.CONFIG["modelo"]
    assert enviado["temperature"] == agent.CONFIG["temperatura"]
    assert enviado["max_tokens"] == agent.CONFIG["max_tokens_resposta"]
    assert {t["function"]["name"] for t in enviado["tools"]} == set(agent.EXECUTORES)


# --------------------------------------------------------------------------
# Correções apontadas pelo plano de testes gerado com IA (docs/ia/, seção 10)
# --------------------------------------------------------------------------


def test_resposta_vazia_vira_frase_de_reserva(cliente_falso):
    # Achado 2: "" voltava no histórico e a API recusava o turno seguinte.
    cliente_falso([resposta(mensagem(content="   "))])
    rastro = agent.novo_rastro()
    texto = agent.responder([{"role": "user", "content": "oi"}], rastro=rastro)
    assert texto == agent.RESPOSTA_VAZIA and rastro["vazia"]


def test_resposta_cortada_por_max_tokens_e_sinalizada(cliente_falso):
    # Achado 11: o cliente recebia frase pela metade sem ninguém saber.
    cortada = resposta(mensagem(content="Os planos são: 300 Mega por R$ 99,90, 500"))
    cortada.choices[0].finish_reason = "length"
    cliente_falso([cortada])
    rastro = agent.novo_rastro()
    agent.responder([{"role": "user", "content": "planos?"}], rastro=rastro)
    assert rastro["cortada"]


def test_argumentos_que_nao_sao_objeto_viram_dict_vazio(cliente_falso):
    # Achado 9: uma lista ia para o rastro e a API devolvia 500.
    cliente_falso(
        [resposta(mensagem(tool_calls=[tool_call("x", "somar", "[1, 2]")])), resposta(mensagem(content="ok"))]
    )
    rastro = agent.novo_rastro()
    agent.responder([{"role": "user", "content": "soma"}], rastro=rastro)
    assert rastro["ferramentas"][0]["argumentos"] == {}
    assert "erro" in rastro["ferramentas"][0]["resultado"]


def test_limite_de_iteracoes_fica_no_rastro_e_no_historico(cliente_falso):
    cliente_falso([resposta(mensagem(tool_calls=[tool_call("l", "somar", {"a": 1, "b": 1})]))])
    mensagens = [{"role": "user", "content": "..."}]
    rastro = agent.novo_rastro()
    agent.responder(mensagens, rastro=rastro)
    assert rastro["limite_iteracoes"]
    assert mensagens[-1] == {"role": "assistant", "content": agent.LIMITE_ITERACOES}


def test_cliente_da_openai_tem_timeout_e_retentativas(monkeypatch):
    # Achado 5: o padrão do SDK espera 10 minutos; o App Service corta antes.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-teste")
    cliente = agent.get_cliente()
    assert cliente.timeout == agent.TIMEOUT_MODELO_S <= agent.CONFIG["gates_eval"]["latencia_p95_max_s"] * 2
    assert cliente.max_retries == 2


def test_loop_com_os_tipos_reais_da_openai(monkeypatch):
    # O dublê com SimpleNamespace pode esconder diferença de formato. Aqui as
    # respostas são objetos ChatCompletion de verdade, como em produção.
    from openai.types.chat import ChatCompletion

    def completion(mensagem, finish="stop"):
        return ChatCompletion.model_validate(
            {
                "id": "c",
                "object": "chat.completion",
                "created": 0,
                "model": "m",
                "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", **mensagem}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        )

    chamada = {"id": "t1", "type": "function", "function": {"name": "somar", "arguments": '{"a": 129.9, "b": 80}'}}
    fila = [
        completion({"content": None, "tool_calls": [chamada]}, "tool_calls"),
        completion({"content": "Total: R$ 209,90."}),
    ]
    enviados = []

    class Cliente:
        class chat:  # noqa: N801 — imita o formato do SDK
            class completions:  # noqa: N801
                @staticmethod
                def create(**kwargs):
                    enviados.append([dict(m) for m in kwargs["messages"]])
                    return fila.pop(0)

    monkeypatch.setattr(agent, "get_cliente", lambda: Cliente)
    mensagens = [{"role": "user", "content": "500 mega + mudança?"}]
    assert agent.responder(mensagens) == "Total: R$ 209,90."
    # A mensagem do assistente com tool_calls vai de volta no formato que a API aceita.
    assistente = enviados[1][1]
    assert assistente["role"] == "assistant" and assistente["tool_calls"][0]["id"] == "t1"
    assert enviados[1][2] == {"role": "tool", "tool_call_id": "t1", "content": '{"resultado": 209.9}'}
