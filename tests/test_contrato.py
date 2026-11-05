"""Contrato: o que o modelo lê (FERRAMENTAS, prompt, config) bate com o código.

São os erros silenciosos de agente: o teste unitário passa, o deploy passa, e
o modelo pede um parâmetro que não existe ou ignora uma ferramenta.
"""

import inspect
import json
import re
from pathlib import Path

import pytest

import agent

pytestmark = pytest.mark.contrato


def test_parametros_declarados_batem_com_a_assinatura():
    for ferramenta in agent.FERRAMENTAS:
        nome = ferramenta["function"]["name"]
        declarados = set(ferramenta["function"]["parameters"]["properties"])
        reais = set(inspect.signature(agent.EXECUTORES[nome]).parameters)
        assert declarados == reais, f"{nome}: declarado {declarados}, Python espera {reais}"


def test_obrigatorios_declarados_sao_parametros_sem_padrao():
    for ferramenta in agent.FERRAMENTAS:
        nome = ferramenta["function"]["name"]
        assinatura = inspect.signature(agent.EXECUTORES[nome]).parameters
        sem_padrao = {p for p, v in assinatura.items() if v.default is inspect.Parameter.empty}
        assert set(ferramenta["function"]["parameters"]["required"]) == sem_padrao, nome


def test_toda_declarada_tem_executor_e_vice_versa():
    declaradas = {f["function"]["name"] for f in agent.FERRAMENTAS}
    assert declaradas == set(agent.EXECUTORES)


def test_enum_de_motivos_do_chamado_bate_com_o_codigo():
    chamado = next(f for f in agent.FERRAMENTAS if f["function"]["name"] == "abrir_chamado")
    enum = chamado["function"]["parameters"]["properties"]["motivo"]["enum"]
    assert tuple(enum) == agent.MOTIVOS_CHAMADO


def test_descriptions_dizem_quando_usar():
    for ferramenta in agent.FERRAMENTAS:
        descricao = ferramenta["function"]["description"]
        assert len(descricao) >= 40, f"{ferramenta['function']['name']}: description vaga — o modelo ignora"
        assert "use" in descricao.lower(), f"{ferramenta['function']['name']}: diga QUANDO usar"


def test_prompt_documenta_todas_as_ferramentas():
    # O modelo decide pela description E pelo prompt: ferramenta que o prompt
    # não menciona tende a ser esquecida.
    for versao in _versoes_de_prompt_em_uso():
        texto = agent.carregar_contexto(versao)
        for nome in agent.EXECUTORES:
            assert f"`{nome}`" in texto, f"prompt {versao} não menciona a ferramenta {nome}"


def test_config_aponta_para_prompts_que_existem():
    for versao in _versoes_de_prompt_em_uso():
        assert agent.caminho_prompt(versao).exists(), f"config aponta para {versao}, que não existe"


def test_config_tem_canario_coerente():
    prompt = agent.CONFIG["prompt"]
    assert 0 <= prompt["pct_canario"] <= 100
    if prompt["pct_canario"] > 0:
        assert prompt["canario"], "pct_canario > 0 sem versão canário definida"
        assert prompt["canario"] != prompt["estavel"]


def test_modelo_fixado_em_versao_datada():
    # Aula de GitOps: "nunca :latest". Um alias como "gpt-4o-mini" muda de
    # comportamento sem commit nenhum; a versão datada só muda por PR.
    for chave in ("modelo",):
        modelo = agent.CONFIG[chave]
        assert re.search(r"\d{4}-\d{2}-\d{2}$", modelo), f"{chave} sem versão datada: {modelo}"
    juiz = agent.CONFIG["gates_eval"].get("modelo_juiz")
    if juiz:
        assert re.search(r"\d{4}-\d{2}-\d{2}$", juiz), f"modelo_juiz sem versão datada: {juiz}"


def test_gates_de_eval_sao_validos():
    gates = agent.CONFIG["gates_eval"]
    assert 0 < gates["taxa_minima"] <= 1
    assert gates["categorias_criticas"], "sem categorias críticas o gate de segurança some"
    assert gates["latencia_p95_max_s"] > 0 and gates["custo_medio_max_usd"] > 0


def test_base_de_conhecimento_bem_formada():
    artigos = json.loads(agent.BASE_JSON.read_text(encoding="utf-8"))["artigos"]
    ids = [a["id"] for a in artigos]
    assert len(ids) == len(set(ids)), "ids repetidos na base"
    for a in artigos:
        assert a["palavras_chave"] and a["texto"].strip(), a["id"]


def _versoes_de_prompt_em_uso():
    prompt = agent.CONFIG["prompt"]
    return [v for v in (prompt["estavel"], prompt.get("canario")) if v]


# --------------------------------------------------------------------------
# Contratos sugeridos pelo plano de testes gerado com IA (docs/ia/)
# --------------------------------------------------------------------------

DINHEIRO = re.compile(r"\d{1,3},\d{2}")


def test_pct_canario_e_inteiro():
    # Achado 14: o sorteio usa baldes de 1%; 0,5% viraria 1% sem ninguém notar.
    assert isinstance(agent.CONFIG["prompt"]["pct_canario"], int)


def test_timeout_do_modelo_cabe_no_gate_de_latencia():
    assert 0 < agent.TIMEOUT_MODELO_S <= 2 * agent.CONFIG["gates_eval"]["latencia_p95_max_s"]


def test_valores_esperados_no_golden_set_existem_na_base():
    # Gabarito com valor inventado aprovaria a alucinação que o eval devia pegar.
    from pathlib import Path

    golden = json.loads((Path(agent.RAIZ) / "evals" / "golden_set.json").read_text(encoding="utf-8"))["casos"]
    texto_base = agent.BASE_JSON.read_text(encoding="utf-8")
    valores_base = {float(v.replace(",", ".")) for v in DINHEIRO.findall(texto_base)}
    for caso in golden:
        for trecho in caso.get("deve_conter_algum", []):
            for valor in DINHEIRO.findall(trecho):
                numero = float(valor.replace(",", "."))
                if caso["categoria"] == "calculo":
                    somas = {round(a + b, 2) for a in valores_base for b in valores_base}
                    assert numero in somas, f"{caso['id']}: {valor} não é soma de dois valores da base"
                else:
                    assert numero in valores_base, f"{caso['id']}: {valor} não está na base"


def test_front_usa_so_campos_que_a_api_devolve():
    import api

    app_js = (Path(agent.RAIZ) / "web" / "app.js").read_text(encoding="utf-8")
    usados_chat = set(re.findall(r"corpo\.([a-z_]+)", app_js)) - {"detail"}
    assert usados_chat <= set(api.RespostaChat.model_fields), usados_chat - set(api.RespostaChat.model_fields)
    usados_info = set(re.findall(r"info\.([a-z_]+)", app_js))
    campos_info = set(api.info())
    assert usados_info <= campos_info, usados_info - campos_info
