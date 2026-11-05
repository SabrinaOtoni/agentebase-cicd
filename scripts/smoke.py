"""
Smoke test pós-deploy: o agente que acabou de ir para o ar responde?

    python scripts/smoke.py https://app.azurewebsites.net --commit <sha>

Três checagens, da mais barata para a mais cara:
  1. /api/health responde (espera o App Service acordar: o plano gratuito dorme);
  2. /api/info mostra o commit esperado — prova que a versão NOVA está no ar,
     e não a anterior em cache;
  3. uma pergunta real em /api/chat volta com resposta e com a ferramenta
     consultar_base usada.

Sai com código 1 na primeira falha. A esteira usa isso para não promover (e,
em produção, para propor o rollback).
"""

from __future__ import annotations

import argparse
import time

import httpx


def esperar_saude(cliente: httpx.Client, url: str, minutos: float) -> None:
    limite = time.monotonic() + minutos * 60
    tentativa = 0
    while True:
        tentativa += 1
        try:
            r = cliente.get(f"{url}/api/health")
            if r.status_code == 200 and r.json().get("status") == "ok":
                print(f"[1/3] health ok (tentativa {tentativa})")
                return
            print(f"      health respondeu {r.status_code}, aguardando…")
        except httpx.HTTPError as exc:
            print(f"      ainda subindo ({type(exc).__name__}), aguardando…")
        if time.monotonic() > limite:
            raise SystemExit(f"FALHA: {url}/api/health não respondeu em {minutos} min")
        time.sleep(10)


def conferir_versao(cliente: httpx.Client, url: str, commit: str | None, minutos: float) -> dict:
    # Logo depois do deploy, o app antigo pode responder por alguns segundos.
    limite = time.monotonic() + minutos * 60
    while True:
        info = cliente.get(f"{url}/api/info").json()
        if not commit or info.get("commit") == commit:
            break
        if time.monotonic() > limite:
            raise SystemExit(f"FALHA: no ar está o commit {info.get('commit')}, esperado {commit}")
        print(f"      no ar ainda está {str(info.get('commit'))[:7]}, aguardando a versão nova…")
        time.sleep(10)
    if not info.get("chave_configurada"):
        raise SystemExit("FALHA: o app está sem OPENAI_API_KEY nas configurações do Azure")
    print(f"[2/3] versão {info['versao']} · commit {str(info['commit'])[:7]} · ambiente {info['ambiente']}")
    return info


def conversar(cliente: httpx.Client, url: str) -> None:
    corpo = {"mensagens": [{"role": "user", "content": "Quais planos de internet vocês têm?"}], "sessao_id": "smoke"}
    for _ in range(3):
        r = cliente.post(f"{url}/api/chat", json=corpo)
        if r.status_code not in (429, 502, 503):
            break
        time.sleep(15)
    if r.status_code != 200:
        raise SystemExit(f"FALHA: /api/chat respondeu {r.status_code}: {r.text[:300]}")
    dados = r.json()
    ferramentas = [f["nome"] for f in dados["ferramentas"]]
    if not dados["resposta"].strip():
        raise SystemExit("FALHA: o agente devolveu resposta vazia")
    if "consultar_base" not in ferramentas:
        raise SystemExit(f"FALHA: o agente respondeu sem consultar a base (ferramentas: {ferramentas})")
    print(f"[3/3] chat ok em {dados['latencia_ms']} ms · ferramentas {ferramentas}")
    print(f"      resposta: {dados['resposta'][:160]!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke test pós-deploy")
    parser.add_argument("url")
    parser.add_argument("--commit", help="commit que precisa estar no ar")
    parser.add_argument("--espera-min", type=float, default=6, help="quanto esperar o app acordar")
    args = parser.parse_args()
    url = args.url.rstrip("/")
    print(f"Smoke test em {url}")
    with httpx.Client(timeout=90, follow_redirects=True) as cliente:
        esperar_saude(cliente, url, args.espera_min)
        conferir_versao(cliente, url, args.commit, args.espera_min)
        conversar(cliente, url)
    print("SMOKE OK")


if __name__ == "__main__":
    main()  # SystemExit("FALHA: ...") já imprime a mensagem e sai com código 1
