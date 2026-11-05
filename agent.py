"""
Agente Val — atendimento da Nimbus Telecom (empresa fictícia).

Evolução do `agent.py` do repositório agentebase da disciplina. O núcleo é o
mesmo (contexto, ferramentas e o loop); o que mudou foi o que a esteira de
CI/CD precisa para publicar o agente com segurança:

    1. CONFIGURAÇÃO   — vem de config/agente.json, versionado no Git (GitOps)
    2. CONTEXTO       — o prompt de sistema é um arquivo versionado em prompts/
    3. FERRAMENTAS    — consultar_base, abrir_chamado e somar
    4. O LOOP         — o mesmo responder(), agora com um rastro de execução
    5. O TERMINAL     — python agent.py

O agente não sabe nada sobre interface: é usado pela API (api.py), pelos
testes, pelos evals e pelo terminal.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, cast

from dotenv import load_dotenv
from openai import BadRequestError, OpenAI
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolParam

# ==========================================================================
# 1. CONFIGURAÇÃO
# ==========================================================================

load_dotenv()

RAIZ = Path(__file__).resolve().parent
CONFIG_JSON = RAIZ / "config" / "agente.json"
PROMPTS_DIR = RAIZ / "prompts"
BASE_JSON = RAIZ / "dados" / "base_nimbus.json"


def carregar_config(caminho: Path = CONFIG_JSON) -> dict[str, Any]:
    """Lê o estado desejado do agente. Se não está no Git, não está em produção."""
    return json.loads(caminho.read_text(encoding="utf-8"))


CONFIG = carregar_config()
MODELO: str = CONFIG["modelo"]
TEMPERATURA: float = float(CONFIG["temperatura"])
MAX_ITERACOES: int = int(CONFIG["max_iteracoes"])
MAX_TOKENS_RESPOSTA: int = int(CONFIG["max_tokens_resposta"])
TIMEOUT_MODELO_S: float = float(CONFIG.get("timeout_modelo_s", 30))

RESPOSTA_VAZIA = "Desculpe, não consegui formular uma resposta agora. Pode repetir a pergunta com outras palavras?"
RESPOSTA_FILTRADA = (
    "Não posso atender a esse pedido. Sou a Val, do atendimento Nimbus, e posso ajudar com a sua internet fibra."
)
LIMITE_ITERACOES = "Atingi o limite de iterações sem concluir. Tente reformular a pergunta."
AVISO_DADO_SENSIVEL = (
    "Por segurança, não compartilhe senhas, dados de cartão ou códigos de verificação por aqui. "
    "Eu não vou usar essas informações."
)

_cliente: OpenAI | None = None


def get_cliente() -> OpenAI:
    """Instancia o cliente da OpenAI uma vez só, na primeira chamada.

    OPENAI_BASE_URL é opcional: permite apontar para outro provedor compatível
    (Azure OpenAI, GitHub Models, Ollama) sem mudar o código.
    """
    global _cliente
    if _cliente is None:
        chave = os.getenv("OPENAI_API_KEY")
        if not chave:
            raise RuntimeError("OPENAI_API_KEY não encontrada. Copie .env.example para .env e coloque sua chave.")
        # Timeout explícito: o padrão do SDK é 10 min, e o App Service corta em ~230 s.
        _cliente = OpenAI(
            api_key=chave,
            base_url=os.getenv("OPENAI_BASE_URL") or None,
            timeout=TIMEOUT_MODELO_S,
            max_retries=2,
        )
    return _cliente


# ==========================================================================
# 2. CONTEXTO
# ==========================================================================


def caminho_prompt(versao: str) -> Path:
    return PROMPTS_DIR / f"{versao}.md"


def carregar_contexto(versao: str | None = None) -> str:
    """Devolve o prompt de sistema da versão pedida (padrão: a estável do config).

    Cada versão é um arquivo imutável em prompts/. Trocar de versão é editar
    config/agente.json num PR — sem tocar em Python.
    """
    versao = versao or CONFIG["prompt"]["estavel"]
    arquivo = caminho_prompt(versao)
    if not arquivo.exists():
        raise FileNotFoundError(f"Prompt '{versao}' não existe em {PROMPTS_DIR}.")
    return arquivo.read_text(encoding="utf-8")


def escolher_prompt(sessao_id: str | None, config: dict[str, Any] | None = None) -> str:
    """Canary de prompt: decide qual versão atende esta sessão.

    Função pura. A mesma sessão sempre cai na mesma versão (hash do id), para
    o cliente não pular de um comportamento para outro no meio da conversa.
    Sem canário configurado, ou sem sessão, todo mundo fica na estável.
    """
    prompt = (config or CONFIG)["prompt"]
    canario, pct = prompt.get("canario"), float(prompt.get("pct_canario", 0))
    if not canario or pct <= 0 or not sessao_id:
        return prompt["estavel"]
    balde = int(hashlib.sha256(sessao_id.encode("utf-8")).hexdigest(), 16) % 100
    return canario if balde < pct else prompt["estavel"]


# ==========================================================================
# 3. FERRAMENTAS
# ==========================================================================
#
# Cada ferramenta tem duas metades: a DECLARAÇÃO (o JSON em FERRAMENTAS, que
# o modelo lê para decidir se chama) e a FUNÇÃO Python (o que roda de
# verdade). Os testes de contrato garantem que as duas não descolam.

MOTIVOS_CHAMADO = (
    "cancelamento",
    "cobranca_contestada",
    "problema_tecnico_48h",
    "pedido_de_excecao",
    "outro",
)

_base_cache: list[dict[str, Any]] | None = None


def _normalizar(texto: str) -> str:
    """Minúsculas e sem acento: 'Instalação' e 'instalacao' viram a mesma coisa."""
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    return sem_acento.lower()


def _contem_termo(texto: str, termo: str) -> bool:
    """Procura o termo como palavra inteira, aceitando plural.

    "preco" casa com "precos", mas "atende" não casa com "atendente" nem
    "conta" com "contato" — o erro que a busca por trecho cometia.
    """
    return re.search(rf"(?<![a-z0-9]){re.escape(termo)}(?:e?s)?(?![a-z0-9])", texto) is not None


def _artigos() -> list[dict[str, Any]]:
    global _base_cache
    if _base_cache is None:
        _base_cache = json.loads(BASE_JSON.read_text(encoding="utf-8"))["artigos"]
    return _base_cache


def consultar_base(pergunta: str) -> dict:
    """Busca na base de conhecimento os artigos mais ligados à pergunta.

    Recuperação por palavra-chave, de propósito: é determinística, então dá
    para testar sem rede. Devolve no máximo 3 artigos, do mais ao menos
    relevante.
    """
    texto = _normalizar(pergunta)
    pontuados = []
    for artigo in _artigos():
        # Conjunto: "preço" e "preco" viram a mesma chave e contam uma vez só.
        chaves = {_normalizar(chave) for chave in artigo["palavras_chave"]}
        pontos = sum(1 for chave in chaves if _contem_termo(texto, chave))
        if pontos:
            pontuados.append((pontos, artigo))
    pontuados.sort(key=lambda par: par[0], reverse=True)
    achados = [{"id": a["id"], "titulo": a["titulo"], "texto": a["texto"]} for _, a in pontuados[:3]]
    if not achados:
        return {
            "artigos": [],
            "aviso": "Nada encontrado na base. Não invente: diga que não tem essa informação "
            "e indique a central 0800 721 0000.",
        }
    return {"artigos": achados}


def abrir_chamado(motivo: str, resumo: str) -> dict:
    """Registra um chamado para a equipe responsável (simulado).

    O protocolo sai de um hash do conteúdo para o teste ser reproduzível.
    Em produção de verdade, isto chamaria o sistema de chamados da empresa.
    """
    if motivo not in MOTIVOS_CHAMADO:
        raise ValueError(f"motivo inválido: {motivo!r}. Use um de {', '.join(MOTIVOS_CHAMADO)}.")
    if not resumo.strip():
        raise ValueError("o resumo do chamado não pode ficar vazio")
    digest = hashlib.sha256(f"{motivo}|{resumo}".encode("utf-8")).hexdigest()
    protocolo = f"NIM-{int(digest[:8], 16) % 1_000_000:06d}"
    return {
        "protocolo": protocolo,
        "motivo": motivo,
        "proximo_passo": "A equipe responsável entra em contato pelo telefone do cadastro em até 2 dias úteis.",
    }


def somar(a: float, b: float) -> dict:
    """Modelos erram conta; Python não."""
    return {"resultado": round(a + b, 2)}


FERRAMENTAS: list[ChatCompletionToolParam] = [
    {
        "type": "function",
        "function": {
            "name": "consultar_base",
            "description": (
                "Busca na base de conhecimento oficial da Nimbus. Use SEMPRE antes de afirmar "
                "qualquer fato sobre planos, preços, taxas, cobertura, prazos, modem, fatura ou políticas."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pergunta": {
                        "type": "string",
                        "description": "A dúvida do cliente, com as palavras-chave do assunto.",
                    }
                },
                "required": ["pergunta"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "abrir_chamado",
            "description": (
                "Abre um chamado para a equipe responsável. Use para cancelamento, cobrança contestada, "
                "problema técnico há mais de 48 horas ou pedido de exceção (desconto, isenção, reembolso)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "motivo": {
                        "type": "string",
                        "enum": list(MOTIVOS_CHAMADO),
                        "description": "Categoria do chamado.",
                    },
                    "resumo": {
                        "type": "string",
                        "description": "Uma frase descrevendo o pedido, sem dados sensíveis do cliente.",
                    },
                },
                "required": ["motivo", "resumo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "somar",
            "description": "Soma dois números. Use sempre que precisar somar valores com exatidão.",
            "parameters": {
                "type": "object",
                "properties": {
                    "a": {"type": "number", "description": "Primeiro número."},
                    "b": {"type": "number", "description": "Segundo número."},
                },
                "required": ["a", "b"],
            },
        },
    },
]

EXECUTORES = {
    "consultar_base": consultar_base,
    "abrir_chamado": abrir_chamado,
    "somar": somar,
}


def executar_ferramenta(nome: str, argumentos: dict) -> str:
    """Executa uma ferramenta e devolve SEMPRE uma string (o modelo só lê texto).

    Erro vira mensagem de erro para o modelo, não crash do programa.
    """
    funcao = EXECUTORES.get(nome)
    if funcao is None:
        return json.dumps({"erro": f"Ferramenta '{nome}' não existe."}, ensure_ascii=False)
    try:
        return json.dumps(funcao(**argumentos), ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 — qualquer falha vira erro para o modelo, não crash
        return json.dumps({"erro": f"Falha em '{nome}': {exc}"}, ensure_ascii=False)


# ==========================================================================
# 4. O LOOP DO AGENTE
# ==========================================================================


# --------------------------------------------------------------------------
# Guardrail de entrada: dado sensível nunca chega ao modelo.
# --------------------------------------------------------------------------
# O prompt manda a Val avisar o cliente, mas o eval mostrou que o modelo nem
# sempre obedece. Regra de segurança não pode depender do modelo: o código
# mascara o dado antes de enviar a conversa ao provedor e o aviso entra
# sempre, no começo da resposta.

_MASCARA = "[dado removido]"
_PADROES_SENSIVEIS = [
    # número de cartão: 13 a 19 dígitos, com espaço ou hífen entre eles
    (re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)"), _MASCARA),
    # CVV / código de segurança do cartão
    (re.compile(r"(?i)\b(cvv|cvc|c[oó]digo de seguran[cç]a)(\D{0,12}?)(\d{3,4})\b"), rf"\1\2{_MASCARA}"),
    # validade do cartão
    (re.compile(r"(?i)\b(validade|vencimento do cart[aã]o)(\D{0,6}?)(\d{2}/\d{2,4})"), rf"\1\2{_MASCARA}"),
    # senha: "a senha é X", "senha: X", "minha senha do site é X", "senha 123456"
    (re.compile(r"(?i)\b(senha[^.,;\n]{0,25}?(?:\s(?:é|e|eh)\s|:|=)\s*)([^\s,;]+)"), rf"\1{_MASCARA}"),
    (re.compile(r"(?i)\b(senha\s+)(\d{4,})"), rf"\1{_MASCARA}"),
    # código de verificação / dois fatores
    (
        re.compile(
            r"(?i)\b(c[oó]digo (?:de )?(?:verifica[cç][aã]o|dois fatores|2fa|sms|autentica[cç][aã]o)\D{0,12}?)(\d{4,8})"
        ),
        rf"\1{_MASCARA}",
    ),
]


def mascarar_dados_sensiveis(texto: str) -> tuple[str, bool]:
    """Troca senha, cartão, CVV, validade e código de verificação por [dado removido]."""
    mascarado = texto
    for padrao, troca in _PADROES_SENSIVEIS:
        mascarado = padrao.sub(troca, mascarado)
    return mascarado, mascarado != texto


def proteger_conversa(mensagens: list) -> bool:
    """Mascara as mensagens do cliente no lugar. Devolve True se a ÚLTIMA tinha dado sensível."""
    do_cliente = [
        m for m in mensagens if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), str)
    ]
    achou = False
    for m in do_cliente:
        m["content"], achou = mascarar_dados_sensiveis(m["content"])
    return achou  # o valor que sobra é o da última mensagem do cliente


def _encerrar(mensagens: list, texto: str, rastro: dict) -> str:
    if rastro.get("dado_sensivel"):
        texto = f"{AVISO_DADO_SENSIVEL}\n\n{texto}"
    mensagens.append({"role": "assistant", "content": texto})
    return texto


def _bloqueio_do_filtro(exc: BadRequestError) -> bool:
    """O Azure OpenAI responde 400 com código content_filter quando barra o pedido."""
    corpo = exc.body if isinstance(exc.body, dict) else {}
    codigo = getattr(exc, "code", None) or corpo.get("code") or (corpo.get("error") or {}).get("code")
    return codigo == "content_filter" or "content management policy" in str(exc)


def novo_rastro() -> dict[str, Any]:
    """O que aconteceu numa resposta: ferramentas, tokens, chamadas ao modelo e sinais de problema."""
    return {
        "ferramentas": [],
        "tokens_entrada": 0,
        "tokens_saida": 0,
        "chamadas_modelo": 0,
        "cortada": False,  # o modelo parou no max_tokens: frase pela metade
        "vazia": False,  # o modelo devolveu texto vazio e entrou a frase de reserva
        "limite_iteracoes": False,
        "filtro_conteudo": False,  # o filtro de conteúdo do provedor barrou o pedido ou a resposta
        "dado_sensivel": False,  # a última mensagem do cliente tinha dado sensível (mascarado)
    }


def responder(
    mensagens: list[ChatCompletionMessageParam],
    verboso: bool = False,
    rastro: dict[str, Any] | None = None,
) -> str:
    """O agente propriamente dito: o mesmo loop da aula.

        manda a conversa + as ferramentas para o modelo
          -> pediu ferramenta? executa, anexa o resultado, repete
          -> respondeu em texto?  acabou, essa é a resposta

    `mensagens` é modificada no lugar. Se `rastro` for passado, ele recebe as
    ferramentas usadas e os tokens gastos — é o que a API devolve ao front e o
    que os evals conferem.
    """
    cliente = get_cliente()
    rastro = rastro if rastro is not None else novo_rastro()
    rastro["dado_sensivel"] = proteger_conversa(mensagens)

    for _ in range(MAX_ITERACOES):
        try:
            resposta = cliente.chat.completions.create(
                model=MODELO,
                messages=mensagens,
                tools=FERRAMENTAS,
                temperature=TEMPERATURA,
                max_tokens=MAX_TOKENS_RESPOSTA,
            )
        except BadRequestError as exc:
            if not _bloqueio_do_filtro(exc):
                raise
            # Guardrail de entrada: o filtro de conteúdo do Azure barrou o pedido
            # (ex.: tentativa de jailbreak). Vira uma recusa educada, não um erro.
            rastro["filtro_conteudo"] = True
            return _encerrar(mensagens, RESPOSTA_FILTRADA, rastro)
        rastro["chamadas_modelo"] += 1
        uso = getattr(resposta, "usage", None)
        if uso is not None:
            rastro["tokens_entrada"] += getattr(uso, "prompt_tokens", 0) or 0
            rastro["tokens_saida"] += getattr(uso, "completion_tokens", 0) or 0

        escolha = resposta.choices[0]
        recado = escolha.message

        # Caso 1: respondeu em texto. Fim.
        if not recado.tool_calls:
            texto = (recado.content or "").strip()
            if not texto:
                # Sem isto, o turno vazio volta no histórico e a API recusa a conversa inteira.
                rastro["vazia"] = True
                texto = RESPOSTA_VAZIA
            fim = getattr(escolha, "finish_reason", None)
            if fim == "length":
                rastro["cortada"] = True
            if fim == "content_filter":  # guardrail de saída do Azure cortou a resposta
                rastro["filtro_conteudo"] = True
                texto = RESPOSTA_FILTRADA
            return _encerrar(mensagens, texto, rastro)

        # Caso 2: quer usar ferramentas.
        mensagens.append(cast(ChatCompletionMessageParam, recado.model_dump(exclude_none=True)))

        for chamada in recado.tool_calls:
            funcao = getattr(chamada, "function", None)
            if funcao is None:
                nome = getattr(chamada, "name", "desconhecida")
                argumentos = {}
            else:
                nome = funcao.name
                try:
                    argumentos = json.loads(funcao.arguments or "{}")
                except json.JSONDecodeError:
                    argumentos = {}
            if not isinstance(argumentos, dict):  # ex.: o modelo mandou uma lista
                argumentos = {}

            if verboso:
                print(f"  [ferramenta] {nome}({argumentos})")

            resultado = executar_ferramenta(nome, argumentos)
            rastro["ferramentas"].append({"nome": nome, "argumentos": argumentos, "resultado": json.loads(resultado)})
            mensagens.append({"role": "tool", "tool_call_id": chamada.id, "content": resultado})

    rastro["limite_iteracoes"] = True
    return _encerrar(mensagens, LIMITE_ITERACOES, rastro)


# ==========================================================================
# 5. O TERMINAL
# ==========================================================================


def main() -> None:
    try:
        get_cliente()  # falha cedo e com mensagem clara se não houver chave
    except RuntimeError as exc:
        print(f"ERRO: {exc}")
        sys.exit(1)

    versao = CONFIG["prompt"]["estavel"]
    mensagens: list[ChatCompletionMessageParam] = [{"role": "system", "content": carregar_contexto(versao)}]
    print(f"Val (Nimbus Telecom) — {MODELO}, prompt {versao}. Digite /sair para encerrar.\n")

    while True:
        try:
            entrada = input("você> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAté mais.")
            return

        if not entrada:
            continue
        if entrada in ("/sair", "/quit", "/exit"):
            print("Até mais.")
            return

        mensagens.append({"role": "user", "content": entrada})
        marca = len(mensagens) - 1
        try:
            print(f"\nval> {responder(mensagens, verboso=True)}\n")
        except Exception as exc:  # noqa: BLE001 — o terminal não deve morrer por falha da API
            print(f"\n[erro ao chamar o modelo] {exc}\n")
            del mensagens[marca:]  # descarta o turno que falhou, inclusive chamadas de ferramenta


if __name__ == "__main__":
    main()
