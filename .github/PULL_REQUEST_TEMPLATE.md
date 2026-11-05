<!-- Preencha antes de pedir revisão. PR sem contexto não é revisável. -->

## O que mudou e por quê

<!-- Descreva a causa, não só o sintoma. -->

## Origem da mudança

- [ ] Autoria humana
- [ ] Gerada por agente de IA — qual agente: ______ · prompt/tarefa que originou: ______

## Tipo de mudança

- [ ] Comportamento do agente (prompt em `prompts/`, `config/agente.json`, base em `dados/`)
- [ ] Código (`agent.py`, `api.py`, `web/`)
- [ ] Testes ou evals (`tests/`, `evals/`)
- [ ] Esteira ou infraestrutura (`.github/`, `infra/`)

## Como foi validado

- [ ] Testes automatizados novos ou atualizados cobrindo a mudança
- [ ] `pytest` passou localmente
- [ ] `python evals/avaliar.py` passou localmente (obrigatório se mudou prompt, modelo ou base)
- [ ] Se criou um prompt novo: arquivo novo em `prompts/` (versões antigas não se editam)

## Checklist

- [ ] Nenhuma chave ou credencial no diff
- [ ] O modelo continua fixado em versão datada (nada de alias que muda sozinho)
- [ ] Mudou o contrato da API ou das ferramentas? Front e testes de contrato atualizados
- [ ] README/docs atualizados, se o comportamento mudou

## Risco e rollback

**Risco (baixo / médio / alto):**

**Como reverter:** `git revert` deste PR, ou Actions → Rollback para a release anterior.
