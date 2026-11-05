"""Execução do eval e do smoke test, com o modelo e a rede simulados.

O avaliador e o smoke test são o que decide se a esteira segue ou para. Aqui
eles rodam de ponta a ponta contra um modelo falso (ClienteFalso) e contra um
servidor HTTP falso (httpx.MockTransport), sem custo e sem rede.
"""

import json
import sys

import httpx
import pytest
from conftest import RAIZ, mensagem, resposta, tool_call

import agent

sys.path.insert(0, str(RAIZ / "evals"))
sys.path.insert(0, str(RAIZ / "scripts"))

import avaliar  # noqa: E402
import smoke  # noqa: E402

pytestmark = pytest.mark.unitario

CASO = {
    "id": "base-teste",
    "categoria": "resposta_da_base",
    "turnos": ["oi", "quanto custa o 500 mega?"],
    "deve_conter_algum": ["129,90"],
    "nao_deve_conter": [],
    "ferramentas_esperadas": ["consultar_base"],
    "ferramentas_proibidas": [],
    "criterio": "Informa o preço do 500 Mega.",
}


@pytest.fixture(autouse=True)
def sem_espera(monkeypatch):
    monkeypatch.setattr(avaliar.time, "sleep", lambda s: None)
    monkeypatch.setattr(smoke.time, "sleep", lambda s: None)


def _args(**kw):
    import argparse

    base = {"url": None, "prompt": agent.CONFIG["prompt"]["estavel"], "sem_juiz": True}
    return argparse.Namespace(**{**base, **kw})


class TestExecucaoLocal:
    def test_conversa_de_varios_turnos_acumula_rastro(self, cliente_falso):
        cliente_falso(
            [
                resposta(mensagem(content="Bom dia, sou a Val.")),
                resposta(mensagem(tool_calls=[tool_call("c", "consultar_base", {"pergunta": "planos preço"})])),
                resposta(mensagem(content="O 500 Mega custa R$ 129,90.")),
            ]
        )
        execucao = avaliar.executar_local(CASO, "val-v1")
        assert execucao["resposta"] == "O 500 Mega custa R$ 129,90."
        assert [f["nome"] for f in execucao["ferramentas"]] == ["consultar_base"]
        assert len(execucao["latencias_s"]) == 2
        assert [m["role"] for m in execucao["conversa"]] == ["user", "assistant", "user", "assistant"]

    def test_caso_aprovado_sem_juiz(self, cliente_falso):
        cliente_falso(
            [
                resposta(mensagem(tool_calls=[tool_call("c", "consultar_base", {"pergunta": "planos"})])),
                resposta(mensagem(content="R$ 129,90")),
            ]
        )
        resultado = avaliar.avaliar_caso({**CASO, "turnos": ["quanto custa?"]}, _args(), "juiz")
        assert resultado["aprovado"] and resultado["falhas"] == []

    def test_erro_na_execucao_reprova_o_caso_sem_derrubar_o_eval(self, monkeypatch):
        def quebra(*a, **k):
            raise ConnectionError("sem rede")

        monkeypatch.setattr(avaliar, "executar_local", quebra)
        resultado = avaliar.avaliar_caso(CASO, _args(), "juiz")
        assert not resultado["aprovado"] and "ConnectionError" in resultado["falhas"][0]


class TestJuiz:
    def test_veredito_do_juiz_e_lido(self, cliente_falso):
        cliente = cliente_falso([resposta(mensagem(content='{"aprovado": true, "nota": 5, "justificativa": "ok"}'))])
        execucao = {"conversa": [], "resposta": "R$ 129,90", "ferramentas": []}
        assert avaliar.julgar(CASO, execucao, "modelo-juiz") == {"aprovado": True, "nota": 5, "justificativa": "ok"}
        enviado = cliente.chamadas[0]
        assert enviado["model"] == "modelo-juiz" and enviado["temperature"] == 0
        assert json.loads(enviado["messages"][1]["content"])["criterio_do_caso"] == CASO["criterio"]

    def test_juiz_com_formato_invalido_reprova(self, cliente_falso):
        cliente_falso([resposta(mensagem(content="acho que sim"))])
        veredito = avaliar.julgar(CASO, {"conversa": [], "resposta": "", "ferramentas": []}, "j")
        assert veredito["aprovado"] is False

    def test_juiz_reprovando_reprova_o_caso_mesmo_com_checagens_ok(self, cliente_falso):
        cliente_falso(
            [
                resposta(mensagem(tool_calls=[tool_call("c", "consultar_base", {"pergunta": "planos"})])),
                resposta(mensagem(content="R$ 129,90")),
                resposta(mensagem(content='{"aprovado": false, "nota": 2, "justificativa": "tom ruim"}')),
            ]
        )
        resultado = avaliar.avaliar_caso({**CASO, "turnos": ["preço?"]}, _args(sem_juiz=False), "j")
        assert not resultado["aprovado"] and "tom ruim" in resultado["falhas"][0]


def _api_falsa(respostas_chat=None, commit="abc", chave=True, health_falhas=0):
    """Servidor falso com /api/health, /api/info e /api/chat."""
    estado = {"health": 0, "chat": list(respostas_chat or [])}

    def handler(request: httpx.Request):
        if request.url.path == "/api/health":
            estado["health"] += 1
            if estado["health"] <= health_falhas:
                return httpx.Response(503)
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/api/info":
            return httpx.Response(
                200, json={"commit": commit, "versao": "1.0.1", "ambiente": "staging", "chave_configurada": chave}
            )
        if request.url.path == "/api/chat":
            status, corpo = estado["chat"].pop(0)
            return httpx.Response(status, json=corpo)
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://app.teste")


def _chat_ok(texto="R$ 129,90", ferramentas=("consultar_base",)):
    return (
        200,
        {
            "resposta": texto,
            "ferramentas": [{"nome": n, "argumentos": {}, "resultado": {}} for n in ferramentas],
            "versao_prompt": "val-v1",
            "modelo": "m",
            "latencia_ms": 1500,
            "tokens_entrada": 300,
            "tokens_saida": 40,
        },
    )


class TestExecucaoRemota:
    def test_conversa_pela_api_implantada(self):
        cliente = _api_falsa([_chat_ok("Bom dia."), _chat_ok()])
        execucao = avaliar.executar_remoto(CASO, "https://app.teste", cliente=cliente)
        assert execucao["resposta"] == "R$ 129,90"
        assert execucao["tokens_entrada"] == 600 and execucao["latencias_s"] == [1.5, 1.5]

    def test_espera_e_repete_quando_bate_no_limite_ou_o_app_esta_acordando(self):
        cliente = _api_falsa([(429, {}), (503, {}), _chat_ok()])
        execucao = avaliar.executar_remoto({**CASO, "turnos": ["preço?"]}, "https://app.teste", cliente=cliente)
        assert execucao["resposta"] == "R$ 129,90"


class TestMainDoEval:
    def test_gera_relatorio_e_sai_com_zero_quando_aprova(self, tmp_path, monkeypatch, cliente_falso):
        golden = tmp_path / "golden.json"
        golden.write_text(json.dumps({"casos": [{**CASO, "categoria": "injecao", "turnos": ["x"]}]}), "utf-8")
        monkeypatch.setattr(avaliar, "GOLDEN_SET", golden)
        monkeypatch.setitem(
            agent.CONFIG, "gates_eval", {**agent.CONFIG["gates_eval"], "categorias_criticas": ["injecao"]}
        )
        monkeypatch.setenv("OPENAI_API_KEY", "sk-teste")
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "resumo.md"))
        cliente_falso(
            [
                resposta(mensagem(tool_calls=[tool_call("c", "consultar_base", {"pergunta": "planos"})])),
                resposta(mensagem(content="R$ 129,90")),
            ]
        )
        saida = tmp_path / "rel.json"
        assert avaliar.main(["--sem-juiz", "--saida", str(saida)]) == 0
        assert json.loads(saida.read_text("utf-8"))["agregado"]["aprovado"] is True
        assert "APROVADO" in (tmp_path / "resumo.md").read_text("utf-8")

    def test_sai_com_um_quando_reprova(self, tmp_path, monkeypatch, cliente_falso):
        golden = tmp_path / "golden.json"
        golden.write_text(json.dumps({"casos": [{**CASO, "turnos": ["x"]}]}), "utf-8")
        monkeypatch.setattr(avaliar, "GOLDEN_SET", golden)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-teste")
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        cliente_falso([resposta(mensagem(content="não sei"))])
        assert avaliar.main(["--sem-juiz", "--saida", str(tmp_path / "r.json")]) == 1

    def test_sem_chave_e_erro_de_uso(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert avaliar.main([]) == 2


class TestSmoke:
    def test_passa_quando_a_versao_nova_responde(self, capsys):
        cliente = _api_falsa([_chat_ok()], health_falhas=2)
        smoke.esperar_saude(cliente, "https://app.teste", minutos=1)
        assert smoke.conferir_versao(cliente, "https://app.teste", "abc", minutos=1)["versao"] == "1.0.1"
        smoke.conversar(cliente, "https://app.teste")
        assert "chat ok" in capsys.readouterr().out

    def test_reprova_se_o_commit_no_ar_e_outro(self):
        cliente = _api_falsa(commit="velho")
        with pytest.raises(SystemExit, match="esperado novo"):
            smoke.conferir_versao(cliente, "https://app.teste", "novo", minutos=0)

    def test_reprova_se_falta_a_chave_no_azure(self):
        with pytest.raises(SystemExit, match="OPENAI_API_KEY"):
            smoke.conferir_versao(_api_falsa(chave=False), "https://app.teste", None, minutos=0)

    def test_reprova_se_o_agente_responde_sem_consultar_a_base(self):
        with pytest.raises(SystemExit, match="sem consultar a base"):
            smoke.conversar(_api_falsa([_chat_ok(ferramentas=())]), "https://app.teste")

    def test_reprova_se_o_health_nunca_responde(self):
        with pytest.raises(SystemExit, match="não respondeu"):
            smoke.esperar_saude(_api_falsa(health_falhas=999), "https://app.teste", minutos=0)


@pytest.mark.parametrize(
    ("modelo", "esperado"),
    [
        ("gpt-5-mini-2025-08-07", {"reasoning_effort": "low"}),
        ("o4-mini-2025-04-16", {"reasoning_effort": "low"}),
        ("gpt-4.1-mini-2025-04-14", {"temperature": 0}),
    ],
)
def test_juiz_de_raciocinio_nao_recebe_temperatura(modelo, esperado):
    assert avaliar.parametros_do_juiz(modelo) == esperado


def test_juiz_barrado_pelo_filtro_vale_a_checagem_deterministica(cliente_falso, monkeypatch):
    from test_loop import _bloqueio_azure

    cliente_falso(
        [
            resposta(mensagem(tool_calls=[tool_call("c", "consultar_base", {"pergunta": "planos"})])),
            resposta(mensagem(content="R$ 129,90")),
        ]
    )

    def juiz_barrado(*a, **k):
        raise _bloqueio_azure()

    monkeypatch.setattr(avaliar, "julgar", juiz_barrado)
    resultado = avaliar.avaliar_caso({**CASO, "turnos": ["preço?"]}, _args(sem_juiz=False), "j")
    assert resultado["aprovado"] and resultado["juiz"]["aprovado"] is None


def test_juiz_fora_do_ar_conta_como_infraestrutura(cliente_falso, monkeypatch):
    cliente_falso(
        [
            resposta(mensagem(tool_calls=[tool_call("c", "consultar_base", {"pergunta": "planos"})])),
            resposta(mensagem(content="R$ 129,90")),
        ]
    )
    monkeypatch.setattr(avaliar, "julgar", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("lento")))
    resultado = avaliar.avaliar_caso({**CASO, "turnos": ["preço?"]}, _args(sem_juiz=False), "j")
    assert not resultado["aprovado"] and resultado["erro_infra"]
