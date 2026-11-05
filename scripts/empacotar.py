"""
Build: gera o pacote que vai para o Azure, carimbado com a versão.

    python scripts/empacotar.py --versao 1.0.42

Cria dist/agente-<versao>.zip com só o que roda em produção (sem testes, evals
ou docs) e um version.json com commit, data e link da execução da esteira. O
MESMO pacote vai para staging e para produção, e fica guardado na release do
GitHub: rollback é publicar de novo um pacote que já existe.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent

# O que o app precisa para rodar. Tudo que não está aqui fica fora do pacote.
INCLUIR = ["api.py", "agent.py", "requirements.txt", "config", "prompts", "dados", "web"]


def commit_atual() -> str:
    if os.getenv("GITHUB_SHA"):
        return os.environ["GITHUB_SHA"]
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=RAIZ, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "desconhecido"


def montar_versao(versao: str) -> dict:
    servidor = os.getenv("GITHUB_SERVER_URL", "https://github.com")
    repo = os.getenv("GITHUB_REPOSITORY")
    run_id = os.getenv("GITHUB_RUN_ID")
    return {
        "versao": versao,
        "commit": commit_atual(),
        "data_build": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repositorio": f"{servidor}/{repo}" if repo else None,
        "execucao": f"{servidor}/{repo}/actions/runs/{run_id}" if repo and run_id else None,
    }


def arquivos_do_pacote() -> list[Path]:
    arquivos = []
    for item in INCLUIR:
        caminho = RAIZ / item
        if caminho.is_dir():
            arquivos += [p for p in sorted(caminho.rglob("*")) if p.is_file() and "__pycache__" not in p.parts]
        elif caminho.is_file():
            arquivos.append(caminho)
        else:
            raise FileNotFoundError(f"item obrigatório do pacote não existe: {item}")
    return arquivos


def empacotar(versao: str, destino: Path) -> Path:
    destino.mkdir(parents=True, exist_ok=True)
    pacote = destino / f"agente-{versao}.zip"
    info = montar_versao(versao)
    with zipfile.ZipFile(pacote, "w", zipfile.ZIP_DEFLATED) as zf:
        for arquivo in arquivos_do_pacote():
            zf.write(arquivo, arquivo.relative_to(RAIZ).as_posix())
        zf.writestr("version.json", json.dumps(info, ensure_ascii=False, indent=2))
    return pacote


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--versao", required=True)
    parser.add_argument("--destino", default=str(RAIZ / "dist"))
    args = parser.parse_args()
    pacote = empacotar(args.versao, Path(args.destino))
    print(f"Pacote: {pacote} ({pacote.stat().st_size // 1024} KB)")
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"pacote={pacote}\n")


if __name__ == "__main__":
    main()
