#!/usr/bin/env bash
# Provisiona a infraestrutura do agente e liga o GitHub a ela. Idempotente:
# rodar de novo só reaplica o estado descrito aqui (Git como fonte da verdade).
#
#   Azure:  1 resource group, 2 App Service Plans F1 (gratuitos, Linux) e 2 Web Apps
#           Python 3.12 — staging e produção —, cada um no seu plano, porque o F1
#           mantém um app por plano.
#   GitHub: environments staging e production (produção com aprovação obrigatória),
#           segredos, variáveis e a proteção da main.
#
# Pré-requisitos: `az login` feito; `gh` com acesso de admin ao repositório; .env
# com OPENAI_API_KEY na raiz do projeto.
#
#   REPO=SabrinaOtoni/agentebase-cicd bash infra/provisionar.sh
#
# Variáveis opcionais: LOCAL (região, padrão brazilsouth), RG, SUFIXO, REVISOR.

set -euo pipefail

RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
: "${REPO:?defina REPO=dono/repositorio}"
LOCAL="${LOCAL:-brazilsouth}"
RG="${RG:-rg-agentebase-cicd}"
REVISOR="${REVISOR:-${REPO%%/*}}"
STARTUP="python -m uvicorn api:app --host 0.0.0.0 --port 8000"
TAGS=(projeto=agentebase-cicd disciplina=deploy-cicd turma=3AIER)

passo() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

OPENAI_API_KEY="$(grep -E '^OPENAI_API_KEY=' "$RAIZ/.env" | head -1 | cut -d= -f2- | tr -d '"'"'"' \r')"
[ -n "$OPENAI_API_KEY" ] || { echo "OPENAI_API_KEY vazia em $RAIZ/.env" >&2; exit 1; }

ASSINATURA="$(az account show --query id -o tsv)"
# Sufixo estável por assinatura: nome de Web App precisa ser único no Azure inteiro.
SUFIXO="${SUFIXO:-$(printf '%s' "$ASSINATURA" | shasum | cut -c1-6)}"
APP_STG="val-nimbus-stg-$SUFIXO"
APP_PRD="val-nimbus-$SUFIXO"

passo "Azure: assinatura $(az account show --query name -o tsv) · região $LOCAL"
az provider register --namespace Microsoft.Web --wait >/dev/null
az group create -n "$RG" -l "$LOCAL" --tags "${TAGS[@]}" -o none

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
    SCM_DO_BUILD_DURING_DEPLOYMENT=true AMBIENTE="$ambiente" OPENAI_API_KEY="$OPENAI_API_KEY" >/dev/null
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
echo "staging:  $URL_STG"
echo "produção: $URL_PRD"
echo "Próximo passo: um push na main (ou Actions → Esteira do agente → Run workflow)."
