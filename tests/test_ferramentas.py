"""Unitários das ferramentas e do contexto — funções puras, sem rede."""

import json

import pytest

import agent

pytestmark = pytest.mark.unitario


class TestConsultarBase:
    def test_encontra_os_precos_dos_planos(self):
        artigos = agent.consultar_base("quais planos e preços vocês têm?")["artigos"]
        assert artigos[0]["id"] == "planos"
        assert "129,90" in artigos[0]["texto"]

    def test_ignora_acento_e_maiuscula(self):
        com = agent.consultar_base("Qual a taxa de INSTALAÇÃO?")["artigos"]
        sem = agent.consultar_base("qual a taxa de instalacao?")["artigos"]
        assert com and com == sem
        assert com[0]["id"] == "instalacao"

    def test_devolve_no_maximo_tres_artigos(self):
        texto = "plano preço instalação cancelar boleto modem cobertura"
        assert len(agent.consultar_base(texto)["artigos"]) <= 3

    def test_sem_resultado_avisa_para_nao_inventar(self):
        resultado = agent.consultar_base("xyz abc qwerty")
        assert resultado["artigos"] == []
        assert "não invente" in resultado["aviso"].lower()

    def test_mais_relevante_vem_primeiro(self):
        artigos = agent.consultar_base("quero cancelar, quanto é a multa da fidelidade?")["artigos"]
        assert artigos[0]["id"] == "cancelamento"


class TestAbrirChamado:
    def test_devolve_protocolo_no_formato_nim(self):
        r = agent.abrir_chamado("cancelamento", "cliente quer cancelar o contrato")
        assert r["protocolo"].startswith("NIM-") and len(r["protocolo"]) == 10
        assert r["motivo"] == "cancelamento"

    def test_protocolo_e_reproduzivel(self):
        a = agent.abrir_chamado("outro", "mesmo resumo")
        b = agent.abrir_chamado("outro", "mesmo resumo")
        assert a["protocolo"] == b["protocolo"]

    def test_motivo_invalido_levanta_erro(self):
        with pytest.raises(ValueError, match="motivo inválido"):
            agent.abrir_chamado("qualquer", "resumo")

    def test_resumo_vazio_levanta_erro(self):
        with pytest.raises(ValueError, match="resumo"):
            agent.abrir_chamado("outro", "   ")


class TestSomar:
    @pytest.mark.parametrize(
        ("a", "b", "esperado"),
        [(2, 3, 5), (-5, 3, -2), (129.9, 80, 209.9), (0.1, 0.2, 0.3), (0, 0, 0)],
    )
    def test_soma_com_duas_casas(self, a, b, esperado):
        assert agent.somar(a, b) == {"resultado": esperado}


class TestExecutarFerramenta:
    def test_resultado_e_sempre_string_json(self):
        resultado = agent.executar_ferramenta("somar", {"a": 1, "b": 1})
        assert isinstance(resultado, str)
        assert json.loads(resultado) == {"resultado": 2}

    def test_ferramenta_inexistente_vira_erro_para_o_modelo(self):
        payload = json.loads(agent.executar_ferramenta("nao_existe", {}))
        assert "nao_existe" in payload["erro"]

    def test_argumento_faltando_vira_erro_nao_excecao(self):
        payload = json.loads(agent.executar_ferramenta("somar", {"a": 1}))
        assert "somar" in payload["erro"]

    def test_excecao_da_ferramenta_vira_erro_nao_crash(self):
        payload = json.loads(agent.executar_ferramenta("abrir_chamado", {"motivo": "x", "resumo": "y"}))
        assert "motivo inválido" in payload["erro"]


class TestContexto:
    def test_carrega_o_prompt_estavel_do_config(self):
        contexto = agent.carregar_contexto()
        assert "Val" in contexto and "Nimbus" in contexto

    def test_versao_inexistente_falha_com_mensagem_clara(self):
        with pytest.raises(FileNotFoundError, match="val-v999"):
            agent.carregar_contexto("val-v999")

    def test_get_cliente_sem_chave_explica_o_que_fazer(self, monkeypatch):
        monkeypatch.setattr(agent, "_cliente", None)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            agent.get_cliente()


class TestCanario:
    CONFIG = {"prompt": {"estavel": "val-v1", "canario": "val-v2", "pct_canario": 30}}

    def test_sem_canario_todo_mundo_na_estavel(self):
        config = {"prompt": {"estavel": "val-v1", "canario": None, "pct_canario": 50}}
        assert agent.escolher_prompt("qualquer", config) == "val-v1"

    def test_pct_zero_desliga_o_canario(self):
        config = {"prompt": {"estavel": "val-v1", "canario": "val-v2", "pct_canario": 0}}
        assert {agent.escolher_prompt(f"s{i}", config) for i in range(50)} == {"val-v1"}

    def test_sem_sessao_vai_para_a_estavel(self):
        assert agent.escolher_prompt(None, self.CONFIG) == "val-v1"

    def test_mesma_sessao_sempre_na_mesma_versao(self):
        versoes = {agent.escolher_prompt("sessao-fixa", self.CONFIG) for _ in range(20)}
        assert len(versoes) == 1

    def test_fracao_no_canario_perto_do_percentual(self):
        no_canario = sum(agent.escolher_prompt(f"sessao-{i}", self.CONFIG) == "val-v2" for i in range(2000))
        assert 0.25 < no_canario / 2000 < 0.35

    def test_pct_cem_manda_todo_mundo_para_o_canario(self):
        config = {"prompt": {"estavel": "val-v1", "canario": "val-v2", "pct_canario": 100}}
        assert {agent.escolher_prompt(f"s{i}", config) for i in range(50)} == {"val-v2"}


class TestBuscaCorrigida:
    """Achados 7 e 8 do plano de testes gerado com IA (docs/ia/)."""

    def test_atendente_traz_os_canais_e_nao_as_cidades(self):
        ids = [a["id"] for a in agent.consultar_base("quero falar com um atendente")["artigos"]]
        assert ids[0] == "canais" and "cobertura" not in ids

    def test_contato_nao_casa_com_conta(self):
        ids = [a["id"] for a in agent.consultar_base("qual o contato de vocês?")["artigos"]]
        assert ids[0] == "canais" and "boleto" not in ids

    def test_plural_continua_casando(self):
        assert agent.consultar_base("quais os preços?")["artigos"][0]["id"] == "planos"

    def test_conjugacao_de_atender_traz_cobertura(self):
        assert agent.consultar_base("vocês atendem em campinas?")["artigos"][0]["id"] == "cobertura"

    def test_variante_com_e_sem_acento_conta_uma_vez(self, monkeypatch):
        artigos = [
            {"id": "a", "titulo": "A", "palavras_chave": ["preço", "preco"], "texto": "a"},
            {"id": "b", "titulo": "B", "palavras_chave": ["preço", "plano"], "texto": "b"},
        ]
        monkeypatch.setattr(agent, "_base_cache", artigos)
        # Antes, "a" ganhava 2 pontos só por repetir a mesma palavra com e sem acento.
        assert agent.consultar_base("preço do plano")["artigos"][0]["id"] == "b"


class TestGuardrailDadoSensivel:
    """O dado sensível é mascarado antes de ir para o modelo (achado do primeiro eval real)."""

    @pytest.mark.parametrize(
        ("texto", "vazado"),
        [
            ("meu login é marcia.souza82 e a senha é Nimbus@2024, entra ai", "Nimbus@2024"),
            ("anota ai: 5162 9934 0071 2287, validade 11/27, cvv 482", "5162"),
            ("anota ai: 5162 9934 0071 2287, validade 11/27, cvv 482", "11/27"),
            ("anota ai: 5162 9934 0071 2287, validade 11/27, cvv 482", "482"),
            ("minha senha do site é 123456, pode anotar", "123456"),
            ("cartão 4111-1111-1111-1111", "4111"),
            ("o código de verificação é 448812", "448812"),
        ],
    )
    def test_mascara_o_dado(self, texto, vazado):
        mascarado, achou = agent.mascarar_dados_sensiveis(texto)
        assert achou and vazado not in mascarado and "[dado removido]" in mascarado

    @pytest.mark.parametrize(
        "texto",
        [
            "liga no 0800 721 0000, meu cep é 01310-100",
            "o protocolo é NIM-123456",
            "quanto custa o plano de 500 mega? e o de 1 giga?",
            "esqueci minha senha do wi-fi, como troco?",
        ],
    )
    def test_nao_mexe_no_que_nao_e_sensivel(self, texto):
        assert agent.mascarar_dados_sensiveis(texto) == (texto, False)

    def test_so_a_ultima_mensagem_decide_o_aviso(self):
        conversa = [
            {"role": "user", "content": "minha senha é abc12345"},
            {"role": "assistant", "content": "Não compartilhe."},
            {"role": "user", "content": "ok, e o boleto?"},
        ]
        assert agent.proteger_conversa(conversa) is False
        assert "abc12345" not in conversa[0]["content"]  # mas a antiga continua mascarada
