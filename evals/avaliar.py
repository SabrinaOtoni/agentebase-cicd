"""
Avaliador do agente: roda o golden dataset e decide se a versão pode seguir.

    python evals/avaliar.py                        # agente local, prompt estável
    python evals/avaliar.py --prompt val-v2        # avalia outra versão de prompt
    python evals/avaliar.py --url https://...      # avalia o agente IMPLANTADO, pela API
    python evals/avaliar.py --repeticoes 3         # cada caso 3 vezes (não-determinismo)
    python evals/avaliar.py --sem-juiz             # só as checagens determinísticas
    python evals/avaliar.py --casos in-x,rb-y      # só alguns casos

Cada caso passa por três camadas:
  1. checagens do caso — trechos obrigatórios e proibidos, ferramentas que
     precisam (ou não podem) ser chamadas;
  2. checagens gerais, iguais para todo caso — protocolo inventado, valor em
     reais sem ferramenta por trás, vazamento do prompt, emoji, nome interno
     de ferramenta na resposta, resposta vazia ou no limite de iterações;
  3. LLM-as-judge — um modelo lê a conversa e o critério do caso e decide se
     aprovaria (prompt em evals/juiz.md). O juiz pode reprovar; nunca absolve
     uma falha das camadas 1 e 2.

Com repetições, caso de categoria crítica só passa se passar em TODAS; os
outros, na maioria. Os gates vêm de config/agente.json (gates_eval).

Saída: relatório JSON em relatorios/, resumo em Markdown no terminal e no
resumo do job do GitHub Actions. Código de saída: 0 aprovado, 1 reprovado,
2 erro de uso.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import agent  # noqa: E402 — precisa do sys.path acima

GOLDEN_SET = RAIZ / "evals" / "golden_set.json"
JUIZ_MD = RAIZ / "evals" / "juiz.md"
RELATORIOS = RAIZ / "relatorios"

MAX_ERRO_INFRA = 0.10  # acima disto, o eval não vale: nem aprova nem reprova a qualidade


# ==========================================================================
# Checagens (funções puras — cobertas por tests/test_avaliador.py)
# ==========================================================================


def normalizar(texto: str) -> str:
    """Minúsculas, sem acento, espaços colapsados; "209.90" vira "209,90"."""
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    texto = " ".join(sem_acento.lower().split())
    return re.sub(r"(\d)\.(\d{2})(?!\d)", r"\1,\2", texto)


def checar_deterministico(
    caso: dict, resposta: str, ferramentas_usadas: list[str], argumentos: list[dict] | None = None
) -> list[str]:
    """Checagens específicas do caso. Devolve a lista de falhas (vazia = passou).

    Os trechos proibidos valem também para os argumentos das ferramentas: o
    número do cartão copiado no resumo do chamado é vazamento, mesmo que a
    resposta ao cliente esteja limpa.
    """
    falhas = []
    texto = normalizar(resposta)
    texto_argumentos = normalizar(json.dumps(argumentos or [], ensure_ascii=False))

    aceitos = caso.get("deve_conter_algum") or []
    if aceitos and not any(normalizar(t) in texto for t in aceitos):
        falhas.append(f"não contém nenhum de {aceitos}")

    for proibido in caso.get("nao_deve_conter") or []:
        if normalizar(proibido) in texto:
            falhas.append(f"contém o trecho proibido {proibido!r}")
        elif normalizar(proibido) in texto_argumentos:
            falhas.append(f"passou o trecho proibido {proibido!r} para uma ferramenta")

    for nome in caso.get("ferramentas_esperadas") or []:
        if nome not in ferramentas_usadas:
            falhas.append(f"não chamou {nome}")

    for nome in caso.get("ferramentas_proibidas") or []:
        if nome in ferramentas_usadas:
            falhas.append(f"chamou {nome}, que é proibida neste caso")

    return falhas


EMOJI = re.compile("[\U0001f300-\U0001faff☀-➿⭐✅]")
DINHEIRO = re.compile(r"\d{1,3}(?:\.\d{3})*,\d{2}")
PERCENTUAL = re.compile(r"\d+(?:,\d+)?\s?%")
PROTOCOLO = re.compile(r"NIM-\d{6}")
NARRACAO = ("consultar_base", "abrir_chamado", "chamar uma funcao", "chamar a funcao", "function call")


def _ngramas(texto: str, n: int = 8) -> set[tuple[str, ...]]:
    palavras = re.findall(r"[a-z0-9]+", normalizar(texto))
    return {tuple(palavras[i : i + n]) for i in range(len(palavras) - n + 1)}


def trechos_do_prompt(prompt: str) -> set[tuple[str, ...]]:
    """Sequências de 8 palavras do prompt. Ficam de fora o cabeçalho (linhas com
    ">") e a seção "Exemplo do tom": imitar o tom do exemplo não é vazamento."""
    corpo = prompt.split("## Exemplo do tom")[0]
    corpo = "\n".join(linha for linha in corpo.splitlines() if not linha.lstrip().startswith(">"))
    return _ngramas(corpo)


def ancoras(ferramentas: list[dict], turnos_cliente: list[str]) -> str:
    """Tudo de onde um número pode legitimamente vir: resultados das ferramentas
    (com o total de somar escrito em reais) e o que o próprio cliente disse."""
    partes = list(turnos_cliente)
    for f in ferramentas:
        resultado = f.get("resultado") or {}
        partes.append(json.dumps(resultado, ensure_ascii=False))
        if f.get("nome") == "somar" and isinstance(resultado.get("resultado"), (int, float)):
            partes.append(f"{resultado['resultado']:.2f}".replace(".", ","))
    return normalizar(" ".join(partes))


def checar_geral(resposta: str, ferramentas: list[dict], turnos_cliente: list[str], prompt: str) -> list[str]:
    """Checagens que valem para TODO caso, sem gabarito."""
    falhas = []
    texto = normalizar(resposta)

    if resposta.strip() in ("", agent.RESPOSTA_VAZIA):
        falhas.append("resposta vazia")
    if resposta.strip() == agent.LIMITE_ITERACOES:
        falhas.append("parou no limite de iterações")

    abertos = {(f.get("resultado") or {}).get("protocolo") for f in ferramentas if f.get("nome") == "abrir_chamado"}
    for protocolo in set(PROTOCOLO.findall(resposta)):
        if protocolo not in abertos:
            falhas.append(f"citou o protocolo {protocolo}, que nenhuma ferramenta abriu")

    base = ancoras(ferramentas, turnos_cliente)
    for valor in set(DINHEIRO.findall(texto)) | {p.replace(" ", "") for p in PERCENTUAL.findall(texto)}:
        if valor not in base.replace(" %", "%"):
            falhas.append(f"valor {valor} sem ferramenta por trás (possível alucinação)")

    if _ngramas(resposta) & trechos_do_prompt(prompt):
        falhas.append("reproduziu um trecho do prompt de sistema")

    if EMOJI.search(resposta) or "!!" in resposta:
        falhas.append("usou emoji ou exclamação dupla")

    for termo in NARRACAO:
        if termo in texto:
            falhas.append(f"narrou o funcionamento interno ({termo!r})")

    return falhas


# ==========================================================================
# Repetições, agregação e gates (funções puras)
# ==========================================================================


def combinar_repeticoes(execucoes: list[dict], critica: bool) -> dict:
    """Junta as N execuções de um caso. Crítico: precisa passar em todas.
    Os outros: na maioria. Custo e tokens viram a média por execução."""
    n = len(execucoes)
    aprovacoes = sum(1 for e in execucoes if e["aprovado"])
    aprovado = aprovacoes == n if critica else aprovacoes > n / 2
    falhas = []
    for e in execucoes:
        falhas += [f for f in e["falhas"] if f not in falhas]
    primeira = execucoes[0]
    return {
        **primeira,
        "aprovado": aprovado,
        "aprovacoes": f"{aprovacoes}/{n}",
        "falhas": [] if aprovado else falhas,
        "falhas_em_alguma_execucao": falhas,
        "erro_infra": all(e.get("erro_infra") for e in execucoes),
        "tokens_entrada": sum(e["tokens_entrada"] for e in execucoes) // n,
        "tokens_saida": sum(e["tokens_saida"] for e in execucoes) // n,
        "latencias_s": [lat for e in execucoes for lat in e["latencias_s"]],
        "respostas": [e.get("resposta", "") for e in execucoes],
    }


def percentil(valores: list[float], p: float) -> float:
    """Percentil pelo método do posto mais próximo (o P95 da aula)."""
    if not valores:
        return 0.0
    ordenados = sorted(valores)
    posto = max(1, math.ceil(p / 100 * len(ordenados)))
    return ordenados[posto - 1]


def custo_usd(tokens_entrada: int, tokens_saida: int, precos: dict) -> float:
    return (tokens_entrada * precos["entrada"] + tokens_saida * precos["saida"]) / 1_000_000


def agregar(resultados: list[dict], gates: dict, precos: dict) -> dict:
    """Junta os resultados por caso em métricas e decide cada gate."""
    total = len(resultados)
    aprovados = sum(1 for r in resultados if r["aprovado"])
    erros_infra = sum(1 for r in resultados if r.get("erro_infra"))

    categorias: dict[str, dict[str, int]] = {}
    for r in resultados:
        c = categorias.setdefault(r["categoria"], {"total": 0, "aprovados": 0})
        c["total"] += 1
        c["aprovados"] += int(r["aprovado"])

    latencias = [lat for r in resultados for lat in r["latencias_s"]]
    custos = [custo_usd(r["tokens_entrada"], r["tokens_saida"], precos) for r in resultados]

    taxa = aprovados / total if total else 0.0
    p95 = percentil(latencias, 95)
    custo_medio = sum(custos) / len(custos) if custos else 0.0

    decisoes = [
        {
            "gate": "execução válida (erros de infraestrutura)",
            "valor": f"{erros_infra}/{total}",
            "limite": f"<= {MAX_ERRO_INFRA:.0%}",
            "passou": total > 0 and erros_infra / total <= MAX_ERRO_INFRA,
        },
        {
            "gate": "taxa de aprovação",
            "valor": round(taxa, 3),
            "limite": f">= {gates['taxa_minima']}",
            "passou": taxa >= gates["taxa_minima"],
        },
    ]
    for critica in gates["categorias_criticas"]:
        c = categorias.get(critica)
        decisoes.append(
            {
                "gate": f"categoria crítica '{critica}'",
                "valor": f"{c['aprovados']}/{c['total']}" if c else "sem casos",
                "limite": "100%",
                "passou": bool(c) and c["aprovados"] == c["total"],
            }
        )
    decisoes.append(
        {
            "gate": "latência P95 por turno (s)",
            "valor": round(p95, 2),
            "limite": f"<= {gates['latencia_p95_max_s']}",
            "passou": p95 <= gates["latencia_p95_max_s"],
        }
    )
    decisoes.append(
        {
            "gate": "custo médio por caso (US$)",
            "valor": round(custo_medio, 5),
            "limite": f"<= {gates['custo_medio_max_usd']}",
            "passou": custo_medio <= gates["custo_medio_max_usd"],
        }
    )

    return {
        "total": total,
        "aprovados": aprovados,
        "taxa": round(taxa, 3),
        "por_categoria": categorias,
        "latencia_p95_s": round(p95, 2),
        "custo_medio_usd": round(custo_medio, 6),
        "custo_total_usd": round(sum(custos), 6),
        "gates": decisoes,
        "aprovado": all(d["passou"] for d in decisoes),
    }


def resumo_markdown(agregado: dict, resultados: list[dict], alvo: str) -> str:
    veredito = "✅ APROVADO" if agregado["aprovado"] else "❌ REPROVADO"
    linhas = [
        f"## Eval do agente — {veredito}",
        "",
        f"Alvo: `{alvo}` · {agregado['aprovados']}/{agregado['total']} casos aprovados "
        f"· custo do agente US$ {agregado['custo_total_usd']:.4f}",
        "",
        "| Gate | Valor | Limite | |",
        "|---|---|---|---|",
    ]
    for d in agregado["gates"]:
        linhas.append(f"| {d['gate']} | {d['valor']} | {d['limite']} | {'✅' if d['passou'] else '❌'} |")

    linhas += ["", "| Caso | Categoria | Resultado | Motivo |", "|---|---|---|---|"]
    for r in resultados:
        motivo = "; ".join(r.get("falhas_em_alguma_execucao", r["falhas"])) or (r.get("juiz") or {}).get(
            "justificativa", ""
        )
        motivo = motivo.replace("|", "/").replace("\n", " ")
        marca = "✅" if r["aprovado"] else "❌"
        linhas.append(f"| {r['id']} | {r['categoria']} | {marca} {r.get('aprovacoes', '')} | {motivo} |")
    return "\n".join(linhas) + "\n"


# ==========================================================================
# Execução dos casos
# ==========================================================================


def executar_local(caso: dict, versao_prompt: str) -> dict:
    """Roda a conversa do caso com o agente em processo (como na esteira pré-deploy)."""
    mensagens: list[Any] = [{"role": "system", "content": agent.carregar_contexto(versao_prompt)}]
    mensagens += [dict(m) for m in caso.get("historico_previo", [])]
    rastro = agent.novo_rastro()
    latencias, resposta = [], ""
    for turno in caso["turnos"]:
        mensagens.append({"role": "user", "content": turno})
        inicio = time.perf_counter()
        resposta = agent.responder(mensagens, rastro=rastro)
        latencias.append(time.perf_counter() - inicio)
    conversa = [
        m for m in mensagens if isinstance(m, dict) and m.get("role") in ("user", "assistant") and m.get("content")
    ]
    return {
        "resposta": resposta,
        "ferramentas": rastro["ferramentas"],
        "tokens_entrada": rastro["tokens_entrada"],
        "tokens_saida": rastro["tokens_saida"],
        "latencias_s": latencias,
        "versao_prompt": versao_prompt,
        "conversa": conversa,
    }


def _post_com_retentativa(cliente: httpx.Client, url: str, corpo: dict) -> dict:
    """O App Service gratuito dorme e o app tem limite por IP: espera e tenta de novo."""
    for tentativa in range(8):
        try:
            r = cliente.post(url, json=corpo)
        except httpx.TransportError:
            time.sleep(10)
            continue
        if r.status_code == 429:
            time.sleep(30)
            continue
        if r.status_code >= 500 and tentativa < 7:
            time.sleep(10)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"{url} não respondeu depois de várias tentativas")


def executar_remoto(caso: dict, url_base: str, cliente: httpx.Client | None = None) -> dict:
    """Roda a conversa do caso contra o agente IMPLANTADO, pela API pública."""
    historico: list[dict] = [dict(m) for m in caso.get("historico_previo", [])]
    ferramentas, latencias = [], []
    tokens_in = tokens_out = 0
    resposta, versao = "", "?"
    sessao = f"eval-{uuid.uuid4()}"
    with cliente or httpx.Client(timeout=90) as cliente:
        for turno in caso["turnos"]:
            historico.append({"role": "user", "content": turno})
            inicio = time.perf_counter()
            corpo = _post_com_retentativa(
                cliente, f"{url_base.rstrip('/')}/api/chat", {"mensagens": historico, "sessao_id": sessao}
            )
            # Latência medida no servidor: a espera por limite e o "acordar" do F1 não são do agente.
            latencias.append(corpo.get("latencia_ms", 0) / 1000 or time.perf_counter() - inicio)
            resposta = corpo["resposta"]
            historico.append({"role": "assistant", "content": resposta})
            ferramentas += corpo["ferramentas"]
            tokens_in += corpo["tokens_entrada"]
            tokens_out += corpo["tokens_saida"]
            versao = corpo["versao_prompt"]
    return {
        "resposta": resposta,
        "ferramentas": ferramentas,
        "tokens_entrada": tokens_in,
        "tokens_saida": tokens_out,
        "latencias_s": latencias,
        "versao_prompt": versao,
        "conversa": historico,
    }


def julgar(caso: dict, execucao: dict, modelo_juiz: str) -> dict:
    """LLM-as-judge: devolve {aprovado, nota, justificativa}. Formato inválido: uma nova tentativa."""
    entrada = {
        "criterio_do_caso": caso["criterio"],
        "conversa": execucao["conversa"],
        "resposta_final_da_val": execucao["resposta"],
        "ferramentas_usadas": execucao["ferramentas"],
    }
    for _ in range(2):
        resposta = agent.get_cliente().chat.completions.create(
            model=modelo_juiz,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": JUIZ_MD.read_text(encoding="utf-8")},
                {"role": "user", "content": json.dumps(entrada, ensure_ascii=False)},
            ],
        )
        try:
            veredito = json.loads(resposta.choices[0].message.content or "{}")
            return {
                "aprovado": bool(veredito["aprovado"]),
                "nota": int(veredito.get("nota", 0)),
                "justificativa": str(veredito.get("justificativa", "")),
            }
        except (KeyError, TypeError, ValueError):
            continue
    return {"aprovado": False, "nota": 0, "justificativa": "o juiz devolveu um formato inválido"}


def avaliar_caso(caso: dict, args: argparse.Namespace, modelo_juiz: str) -> dict:
    """Uma execução de um caso: roda a conversa, aplica as três camadas."""
    erro = None
    for _ in range(2):  # erro de infraestrutura ganha uma nova tentativa; falha de qualidade, não
        try:
            execucao = executar_remoto(caso, args.url) if args.url else executar_local(caso, args.prompt)
            break
        except Exception as exc:  # noqa: BLE001 — um caso que quebra reprova, não derruba o eval inteiro
            erro = exc
    else:
        return {
            "id": caso["id"],
            "categoria": caso["categoria"],
            "aprovado": False,
            "erro_infra": True,
            "falhas": [f"erro ao executar: {type(erro).__name__}: {erro}"],
            "juiz": None,
            "resposta": "",
            "ferramentas": [],
            "tokens_entrada": 0,
            "tokens_saida": 0,
            "latencias_s": [],
        }

    nomes = [f["nome"] for f in execucao["ferramentas"]]
    argumentos = [f.get("argumentos") or {} for f in execucao["ferramentas"]]
    falhas = checar_deterministico(caso, execucao["resposta"], nomes, argumentos)
    prompt = agent.carregar_contexto(execucao["versao_prompt"]) if execucao["versao_prompt"] != "?" else ""
    falhas += checar_geral(execucao["resposta"], execucao["ferramentas"], caso["turnos"], prompt)
    veredito = None if args.sem_juiz else julgar(caso, execucao, modelo_juiz)
    if veredito is not None and not veredito["aprovado"]:
        falhas.append(f"juiz: {veredito['justificativa']}")

    return {
        "id": caso["id"],
        "categoria": caso["categoria"],
        "aprovado": not falhas,
        "erro_infra": False,
        "falhas": falhas,
        "juiz": veredito,
        "resposta": execucao["resposta"],
        "ferramentas": nomes,
        "versao_prompt": execucao["versao_prompt"],
        "tokens_entrada": execucao["tokens_entrada"],
        "tokens_saida": execucao["tokens_saida"],
        "latencias_s": [round(x, 3) for x in execucao["latencias_s"]],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Roda o golden dataset e aplica os gates.")
    parser.add_argument("--url", help="avalia o agente implantado nesta URL, em vez do local")
    parser.add_argument("--prompt", default=agent.CONFIG["prompt"]["estavel"], help="versão do prompt (só local)")
    parser.add_argument("--casos", help="ids separados por vírgula (padrão: todos)")
    parser.add_argument("--repeticoes", type=int, default=1, help="execuções por caso")
    parser.add_argument("--sem-juiz", action="store_true", help="pula o LLM-as-judge")
    parser.add_argument("--paralelo", type=int, default=4, help="execuções ao mesmo tempo (local)")
    parser.add_argument("--saida", help="caminho do relatório JSON")
    args = parser.parse_args(argv)

    if not os.getenv("OPENAI_API_KEY") and not (args.url and args.sem_juiz):
        print("ERRO: o eval precisa de OPENAI_API_KEY (para o agente local e/ou para o juiz).")
        return 2

    casos = json.loads(GOLDEN_SET.read_text(encoding="utf-8"))["casos"]
    if args.casos:
        escolhidos = set(args.casos.split(","))
        casos = [c for c in casos if c["id"] in escolhidos]
    if not casos:
        print("ERRO: nenhum caso selecionado.")
        return 2

    gates = agent.CONFIG["gates_eval"]
    modelo_juiz = gates.get("modelo_juiz", agent.MODELO)
    repeticoes = max(1, args.repeticoes)
    alvo = args.url or f"local · prompt {args.prompt}"
    paralelo = 1 if args.url else max(1, args.paralelo)  # remoto em série: respeita o limite por IP
    print(
        f"Avaliando {len(casos)} casos x {repeticoes} · alvo: {alvo} · "
        f"juiz: {'desligado' if args.sem_juiz else modelo_juiz}"
    )

    tarefas = [c for c in casos for _ in range(repeticoes)]
    with ThreadPoolExecutor(max_workers=paralelo) as pool:
        execucoes = list(pool.map(lambda c: avaliar_caso(c, args, modelo_juiz), tarefas))

    criticas = set(gates["categorias_criticas"])
    resultados = [
        combinar_repeticoes(execucoes[i * repeticoes : (i + 1) * repeticoes], caso["categoria"] in criticas)
        for i, caso in enumerate(casos)
    ]

    agregado = agregar(resultados, gates, agent.CONFIG["precos_usd_por_milhao_tokens"])
    agregado["repeticoes"] = repeticoes
    agregado["modelo_juiz"] = None if args.sem_juiz else modelo_juiz
    resumo = resumo_markdown(agregado, resultados, alvo)
    print(resumo)

    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(resumo)

    saida = Path(args.saida) if args.saida else RELATORIOS / ("eval-remoto.json" if args.url else "eval-local.json")
    saida.parent.mkdir(parents=True, exist_ok=True)
    saida.write_text(
        json.dumps({"alvo": alvo, "agregado": agregado, "casos": resultados}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Relatório: {saida}")
    return 0 if agregado["aprovado"] else 1


if __name__ == "__main__":
    sys.exit(main())
