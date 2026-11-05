#!/usr/bin/env bash
# Provisiona a infraestrutura do agente e liga o GitHub a ela. Idempotente:
# rodar de novo só reaplica o estado descrito aqui (Git como fonte da verdade).
#
#   Azure:  1 resource group; 1 Azure OpenAI com os modelos do config/agente.json
#           (agente e juiz do eval), cada um fixado na versão datada; 2 App Service
#           Plans F1 (gratuitos, Linux) e 2 Web Apps Python 3.12 — staging e
#           produção —, cada um no seu plano, porque o F1 mantém um app por plano.
#   GitHub: environments staging e production (produção com aprovação obrigatória),
#           segredos, variáveis e a proteção da main.
#
# Pré-requisitos: `az login` feito; `gh` com acesso de admin ao repositório.
# O script grava no .env local (fora do Git) o endereço e a chave do Azure OpenAI,
# para rodar os evals na máquina.
#
#   REPO=SabrinaOtoni/agentebase-cicd bash infra/provisionar.sh
#
# Variáveis opcionais: LOCAL (região dos apps, padrão brazilsouth), LOCAL_IA (região
# do Azure OpenAI, padrão = LOCAL), CAPACIDADE e CAPACIDADE_JUIZ (mil tokens/min, padrão
# 150 e 100; a cota de estudante para o gpt-4.1-mini é 200),
# RG, SUFIXO, REVISOR.

set -euo pipefail

RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
: "${REPO:?defina REPO=dono/repositorio}"
LOCAL="${LOCAL:-brazilsouth}"
LOCAL_IA="${LOCAL_IA:-$LOCAL}"
CAPACIDADE="${CAPACIDADE:-150}"
CAPACIDADE_JUIZ="${CAPACIDADE_JUIZ:-100}"
RG="${RG:-rg-agentebase-cicd}"
REVISOR="${REVISOR:-${REPO%%/*}}"
STARTUP="python -m uvicorn api:app --host 0.0.0.0 --port 8000"
TAGS=(projeto=agentebase-cicd disciplina=deploy-cicd turma=3AIER)

passo() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

# Os modelos vêm do estado desejado no Git: "gpt-4o-mini-2024-07-18" = modelo + versão.
read -r MODELO JUIZ < <(python3 -c "
import json; c = json.load(open('$RAIZ/config/agente.json'))
print(c['modelo'], c['gates_eval'].get('modelo_juiz', c['modelo']))")

ASSINATURA="$(az account show --query id -o tsv)"
# Sufixo estável por assinatura: nome de Web App precisa ser único no Azure inteiro.
SUFIXO="${SUFIXO:-$(printf '%s' "$ASSINATURA" | shasum | cut -c1-6)}"
APP_STG="val-nimbus-stg-$SUFIXO"
APP_PRD="val-nimbus-$SUFIXO"

passo "Azure: assinatura $(az account show --query name -o tsv) · região $LOCAL"
az provider register --namespace Microsoft.Web --wait >/dev/null
az group create -n "$RG" -l "$LOCAL" --tags "${TAGS[@]}" -o none

AOAI="aoai-val-$SUFIXO"
passo "Azure OpenAI: $AOAI ($LOCAL_IA)"
az provider register --namespace Microsoft.CognitiveServices --wait >/dev/null
if ! az cognitiveservices account show -g "$RG" -n "$AOAI" -o none 2>/dev/null; then
  az cognitiveservices account create -g "$RG" -n "$AOAI" -l "$LOCAL_IA" --kind OpenAI --sku S0 \
    --custom-domain "$AOAI" --tags "${TAGS[@]}" --yes -o none
fi

publicar_modelo() {
  # O nome do deployment é o próprio modelo com a versão: o que está no config é o que roda.
  # "create" é um PUT: rodar de novo só reaplica a versão e a capacidade.
  local deployment="$1" capacidade="$2" nome="${1%-????-??-??}" versao="${1: -10}"
  echo "publicando $nome versão $versao (capacidade: $capacidade mil tokens/min)"
  az cognitiveservices account deployment create -g "$RG" -n "$AOAI" --deployment-name "$deployment" \
    --model-name "$nome" --model-version "$versao" --model-format OpenAI \
    --sku-name GlobalStandard --sku-capacity "$capacidade" -o none
}
publicar_modelo "$MODELO" "$CAPACIDADE"
[ "$JUIZ" = "$MODELO" ] || publicar_modelo "$JUIZ" "$CAPACIDADE_JUIZ"

OPENAI_BASE_URL="$(az cognitiveservices account show -g "$RG" -n "$AOAI" --query properties.endpoint -o tsv)openai/v1/"
OPENAI_API_KEY="$(az cognitiveservices account keys list -g "$RG" -n "$AOAI" --query key1 -o tsv)"

# .env local para rodar os evals na máquina (o arquivo é ignorado pelo Git).
umask 077
printf '%s\n' "# Gerado por infra/provisionar.sh — Azure OpenAI $AOAI. Não vai para o Git." \
  "OPENAI_API_KEY=$OPENAI_API_KEY" "OPENAI_BASE_URL=$OPENAI_BASE_URL" >"$RAIZ/.env"

criar_app() {
  local app="$1" ambiente="$2" plano="plan-$1"
  passo "Azure: $app ($ambiente)"
  az appservice plan create -g "$RG" -n "$plano" -l "$LOCAL" --sku F1 --is-linux --tags "${TAGS[@]}" -o none
  if ! az webapp show -g "$RG" -n "$app" -o none 2>/dev/null; then
    az webapp create -g "$RG" -p "$plano" -n "$app" --runtime "PYTHON:3.12" --tags "${TAGS[@]}" ambiente="$ambiente" -o none
  fi
  az webapp config set -g "$RG" -n "$app" --startup-file "$STARTUP" --ftps-state Disabled --min-tls-version 1.2 -o none
  az webapp update -g "$RG" -n "$app" --https-only true -o none
  # A saída do comando lista as configurações; vai para /dev/null para a chave não aparecer.
  az webapp config appsettings set -g "$RG" -n "$app" -o none --settings \
    SCM_DO_BUILD_DURING_DEPLOYMENT=true AMBIENTE="$ambiente" \
    OPENAI_API_KEY="$OPENAI_API_KEY" OPENAI_BASE_URL="$OPENAI_BASE_URL" >/dev/null
  # O deploy por publish profile usa autenticação básica no SCM (só nele; FTP fica desligado).
  az resource update -g "$RG" --namespace Microsoft.Web --resource-type basicPublishingCredentialsPolicies \
    --parent "sites/$app" -n scm --set properties.allow=true -o none
}

criar_app "$APP_STG" staging
criar_app "$APP_PRD" production

URL_STG="https://$(az webapp show -g "$RG" -n "$APP_STG" --query defaultHostName -o tsv)"
URL_PRD="https://$(az webapp show -g "$RG" -n "$APP_PRD" --query defaultHostName -o tsv)"

passo "GitHub: configurações do repositório $REPO"
gh repo edit "$REPO" --delete-branch-on-merge --enable-squash-merge \
  --enable-merge-commit=false --enable-rebase-merge=false >/dev/null

if gh api "repos/$REPO/branches/main" >/dev/null 2>&1; then
  passo "GitHub: proteção da main (PR, checks Build/Testes/Eval, revisão do CODEOWNER)"
  gh api -X PUT "repos/$REPO/branches/main/protection" --input - >/dev/null <<'JSON'
{
  "required_status_checks": { "strict": true, "contexts": ["Build", "Testes", "Eval"] },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "required_approving_review_count": 1,
    "require_code_owner_reviews": true,
    "dismiss_stale_reviews": true
  },
  "restrictions": null,
  "required_linear_history": true,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
else
  echo "A main ainda não existe no GitHub: rode este script de novo depois do primeiro push para proteger a main."
fi

passo "GitHub: environments (só a main publica; produção com aprovação de @$REVISOR)"
REVISOR_ID="$(gh api "users/$REVISOR" -q .id)"
SO_MAIN='{ "protected_branches": false, "custom_branch_policies": true }'
gh api -X PUT "repos/$REPO/environments/staging" --input - >/dev/null <<JSON
{ "deployment_branch_policy": $SO_MAIN }
JSON
gh api -X PUT "repos/$REPO/environments/production" --input - >/dev/null <<JSON
{ "reviewers": [{ "type": "User", "id": $REVISOR_ID }], "deployment_branch_policy": $SO_MAIN }
JSON
for amb in staging production; do
  if ! gh api "repos/$REPO/environments/$amb/deployment-branch-policies" -q '.branch_policies[].name' | grep -qx main; then
    gh api -X POST "repos/$REPO/environments/$amb/deployment-branch-policies" -f name=main -f type=branch >/dev/null
  fi
done

passo "GitHub: segredos e variáveis"
printf '%s' "$OPENAI_API_KEY" | gh secret set OPENAI_API_KEY --repo "$REPO"
gh variable set OPENAI_BASE_URL --repo "$REPO" --body "$OPENAI_BASE_URL"
# O publish profile vai do Azure direto para o GitHub, sem passar por arquivo.
az webapp deployment list-publishing-profiles -g "$RG" -n "$APP_STG" --xml |
  gh secret set AZURE_WEBAPP_PUBLISH_PROFILE --env staging --repo "$REPO"
az webapp deployment list-publishing-profiles -g "$RG" -n "$APP_PRD" --xml |
  gh secret set AZURE_WEBAPP_PUBLISH_PROFILE --env production --repo "$REPO"
gh variable set STAGING_APP --repo "$REPO" --body "$APP_STG"
gh variable set STAGING_URL --repo "$REPO" --body "$URL_STG"
gh variable set PRODUCTION_APP --repo "$REPO" --body "$APP_PRD"
gh variable set PRODUCTION_URL --repo "$REPO" --body "$URL_PRD"

passo "Pronto"
echo "modelo:   $OPENAI_BASE_URL ($MODELO, juiz $JUIZ)"
echo "staging:  $URL_STG"
echo "produção: $URL_PRD"
echo "Próximo passo: um push na main (ou Actions → Esteira do agente → Run workflow)."
