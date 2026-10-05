#!/bin/sh
# Grava no Vault de desenvolvimento os segredos que, num ambiente real, a equipe da
# plataforma provisiona. Roda uma vez a cada `docker compose up` (o Vault em modo dev
# guarda tudo em memória).
#
# - llmgw/providers/foundry: credencial do Foundry SIMULADO (serviço fake-foundry).
#   Num ambiente real: endpoint https://<recurso>.services.ai.azure.com, api_key do
#   recurso ou service principal (tenant_id/client_id/client_secret) e, para descobrir
#   os deployments, project_endpoint ou subscription_id/resource_group/account_name.
# - llmgw/_system/ldap: senha da conta de serviço que consulta o diretório.
set -e

until vault status >/dev/null 2>&1; do sleep 1; done

FOUNDRY=http://fake-foundry:8070

vault kv put -mount=secret llmgw/providers/foundry \
  endpoint="$FOUNDRY" \
  auth=api_key \
  api_key=chave-falsa \
  tenant_id=tenant-1 \
  client_id=cliente-falso \
  client_secret=segredo-falso \
  authority="$FOUNDRY" \
  project_endpoint="$FOUNDRY/api/projects/projeto-1"

vault kv put -mount=secret llmgw/_system/ldap bind_password=leitor-senha

echo "segredos de desenvolvimento gravados"
