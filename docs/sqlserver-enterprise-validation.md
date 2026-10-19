# SQL Server — validação empresarial e cobertura de implantação

## Estado em 19/10/2026

**PARCIAL.** Este incremento implementa uma sonda somente leitura e controles de
cobertura. Não instala SQL Server, aplica CU/GDR, altera compatibilidade, cria
índices, concede privilégios, configura Azure, restaura dados nem promove produção.

Checkpoint canônico: [TODO-37](https://app.notion.com/p/3f74c12bae7c8105a17fe7bcf1d4ff94).
Regras consultadas: `chatgpt-operational-rules` 1.6.11,
SHA `1dc0e4f07e52dbc634033878e2e98e67af8bd337`.
Base desta implementação: `683b68c9fcfb40a5906109acfda2b13e036c15b9`.

A PR #11 foi integrada em 13/10. Seu laboratório descartável não é evidência de
HML. A leitura atual do TODO-37 contém evidência física de 13/10, não uma nova
observação do Desktop: motor não comprovado e configuração HML pendente.
Este incremento não repete canários sem mudança de pré-condição nem modifica
o runtime existente.

## O que é executável neste incremento

`src` lógico: módulo `scripts.sqlserver_enterprise_validation`.

- Reutiliza integralmente os controles do coletor `sqlserver_hml_inventory`:
  HML explícita, alvo esperado, SHA, correlação, ODBC permitido, TLS e certificado.
- Abre duas conexões, repete inventário e compara versão, edição, alvo,
  compatibilidade, Query Store, TLS e configurações relevantes.
- Impõe timeout de consulta de 15 segundos; falha ao configurá-lo bloqueia a sonda.
- Usa somente consultas SELECT fixas e JSON sintético. Não recebe SQL livre.
- Contém oito controles JSON/OPENJSON: esquema explícito e Unicode, nulo/ausente,
  posição e valores de array, sensibilidade a maiúsculas no caminho, ISJSON
  inválido, rejeição de JSON malformado, strict ausente e conversão inválida.
- Para motor 17.x, acrescenta três controles de JSON nativo: round-trip,
  JSON_VALUE e rejeição de escalar.
- Exige códigos nativos esperados nos controles negativos. Timeout, falha de
  permissão e indisponibilidade não contam como rejeição JSON correta.
- Fecha conexões em erro e sucesso; a entrada principal desativa pooling.
- Não imprime DSN, host, banco nem mensagens brutas do driver.
- Mantém `hml_verified=false`, `enterprise_status=PARTIAL` e doze gates pendentes.

A expressão legada `inventory.openjson_compatible` é uma estimativa por nível.
`openjson_runtime_verified` registra o comportamento testado. A configuração
`ALLOW_BUILTIN_TVF_IN_ALL_COMPAT_LEVELS` pode permitir OPENJSON abaixo de 130;
não elevar automaticamente compatibilidade para corrigir aquela estimativa.

A checagem de membros de roles administrativas não comprova ausência de grants
diretos, permissões em objetos, ownership chaining ou módulos assinados.
O aceite de menor privilégio continua exigindo inventário efetivo e controles
negativos reais em objetos de teste autorizados.

## Segurança da execução real

A configuração é a mesma do inventário HML existente, somente por canal
protegido. São obrigatórios Session Launcher/preflight, `SESSION_LAUNCH_OK`,
Command Gateway, SHA exato, identidade aprovada e alvo identificado.
O marcador `HML-READONLY` não substitui autorização real.

Este incremento não registra uma nova action no Gateway nem disponibiliza um
novo transporte físico. Sem action/rota governada e configuração HML resolvidas,
a execução real permanece bloqueada. Não usar shell, SSH, GUI ou segredo no chat
como alternativa.

O fluxo de CI executa apenas os testes Python com driver simulado, em um passo
adicional do job `repository-hygiene`. Preserva os demais jobs existentes e não
introduz contêiner, instalação, upload de artifact, agenda ou deploy.

## Matriz completa: mudança, risco e aceite

Todos os IDs abaixo são exigidos pela função de cobertura. A matriz não é uma
declaração de que os executores de todas essas frentes já foram implementados.

| Gate | Implementação presente | Validação real ainda exigida |
|---|---|---|
| `build_servicing_and_support` | Reuso do inventário e repetição independente; fontes oficiais abaixo | Identificar branch CU/GDR, build antes/depois, edição e cobertura de suporte/ESU. Aprovar KB exata, rollback e janela antes de patch. |
| `cu_known_issue_regressions` | Gate de cobertura; sem injeção automática de falha | SESSION_CONTEXT com paralelismo/reuso, linked server MSDASQL sob conta não administrativa e monitoramento durante recuperação, somente em laboratório isolado autorizado. Conferir logs/dumps. |
| `json_contract_and_ingestion_replay` | Oito controles somente leitura implementados | Executar no HML; testar ingestão com chave idempotente, replay, documento inválido em quarentena e leitura independente da persistência. |
| `native_json_and_index_benchmark` | Três controles nativos para 17.x | Comparar nvarchar/max e json, extração e índices sobre os mesmos dados; medir CPU, leituras, memória, log, armazenamento, custo de escrita e P50/P95/P99. |
| `compatibility_160_170_oppo` | Leitura de compatibilidade e configuração OPPO | Capturar Query Store, manter CL anterior após upgrade, comparar 160/170 separadamente; parâmetros NULL, seletivos, não seletivos e enviesados; planos e invariantes sem regressão. |
| `concurrency_and_engine_performance` | Gate de cobertura | Carga representativa, warm-up fora da medição, concorrência fixa, bloqueios/deadlocks, timeout/retry, grants, spill e version store; limites de regressão aprovados antes do teste. |
| `tls_authentication_and_least_privilege` | Reuso da exigência de TLS/certificado e bloqueio de roles amplas | Certificado válido aceito, inválido/expirado/hostname incorreto rejeitado; grants efetivos, DDL/DML proibidos, auditoria e identidades separadas comprovados. |
| `drivers_and_integrations` | Gate de cobertura | Inventariar ODBC/OLE DB/JDBC/SqlClient e integrações usadas; autenticação, TLS, pooling, transações, retry, linked servers, Agent, ETL/BI, replicação e log shipping com readback no destino. |
| `azure_sql_applicability_and_migration` | Gate de aplicabilidade; nenhuma migração | Inventário autorizado de recursos, SKU/hardware e update policy. Revalidar anúncios Fsv2/AMQP antes de qualquer decisão; Event Hubs/MI link somente se usados, com replay, duplicação, lag, failover e custo medidos. |
| `tooling_and_agent_permissions` | Gate de cobertura e baseline SSMS atualizado | SSMS/SqlClient/ODBC sob identidade restrita; Entra quando usado, metadados JSON, schema compare, formatação/recompilação T-SQL, auditoria de agente/MCP e rejeição de operação proibida. |
| `backup_restore_and_ha` | Gate de cobertura; nenhum backup SQL Server declarado | Backup real protegido, restauração em destino independente, verificação de integridade/dados, RPO/RTO e reconexão/failover quando aplicável. Não aceitar RESTORE VERIFYONLY isolado. |
| `business_e2e_and_rollback` | Gate de cobertura; nunca autoriza promoção | Transação de negócio representativa até destino final, controle negativo, repetição sem duplicação e rollback ensaiado, todos no mesmo build/SHA/ambiente. |

Não executar reproduções capazes de provocar crash no serviço HML compartilhado.
Não aplicar trace flags, TrustServerCertificate, privilégios administrativos ou
mudanças de hardware como contorno indiscriminado.

## Cobertura não é autenticidade

`coverage(receipts, source_sha, correlation_id)` rejeita gate ausente, SHA/correlação
divergente, evidência CI apresentada como HML, `skipped`, falha e falta de hashes
de artifact/readback. `NOT_APPLICABLE` exige motivo e referência de revisão.

Mesmo um conjunto estruturalmente completo retorna `REVIEW_REQUIRED`,
`evidence_authenticity_verified=false`, `promotion_allowed=false` e
`hml_verified=false`. Os hashes declarados não são autenticados por esta função:
é obrigatório obter e ler artifacts reais por fonte independente, conferir
conteúdo, integridade, escopo, horário e build. Não usar recibos autorrelatados
como certificação nem conectar essa função diretamente a um deploy.

## Validação deste incremento

37 testes unitários com driver simulado foram executados após a última alteração.
Cobrem cenário positivo, rejeições, sanitização, fechamento, timeout, ordenação de
array, compatibilidade legada, versão nativa, deriva entre conexões, cobertura e
idempotência da sonda. O teste de deriva foi também exercitado por mutação
controlada: remover a comparação causou falha; o código foi restaurado e retestado.

Sintaxe Python e estrutura YAML verificadas. O CI da branch deve ser relido no SHA
publicado e seus logs devem mostrar a execução do novo passo. Nenhum destes
resultados comprova sintaxe/comportamento T-SQL no motor real, desempenho,
permissões SQL efetivas, backup/restauração ou HML. Essa lacuna permanece explícita.

## Fontes oficiais revalidadas em 19/10/2026

- [KB321185: histórico de builds](https://support.microsoft.com/en-us/servicing/sql/kb321185-download-and-install-latest-updates):
  2025 CU9+GDR `17.0.5015.4`, 2025 GDR `17.0.1145.4`,
  2022 CU27+GDR `16.0.4305.5`, publicados em 13/10/2026.
- [KB5126967](https://support.microsoft.com/en-us/servicing/sql/sql-server-2025/cumulative-update/kb5126967-cu9-gdr-october-2026)
  e [KB5126934](https://support.microsoft.com/en-us/servicing/sql/sql-server-2022/cumulative-update/kb5126934-cu27-gdr-october-2026):
  vulnerabilidades e problemas conhecidos; não são ordens de instalação.
- [JSON nativo](https://learn.microsoft.com/en-us/sql/t-sql/data-types/json-data-type?view=sql-server-ver17):
  a documentação atual declara tipo json e índices JSON geralmente disponíveis.
  Corrige a classificação como preview do briefing anterior.
- [OPENJSON](https://learn.microsoft.com/en-us/sql/t-sql/functions/openjson-transact-sql?view=sql-server-ver17):
  CL130 ou a exceção configurada `ALLOW_BUILTIN_TVF_IN_ALL_COMPAT_LEVELS`.
- [OPPO](https://learn.microsoft.com/en-us/sql/relational-databases/performance/optional-parameter-optimization?view=sql-server-ver17):
  exige CL170 e configuração habilitada; confirmar elegibilidade pelos planos.
- [Novidades SQL Server 2025](https://learn.microsoft.com/en-us/sql/sql-server/what-s-new-in-sql-server-2025?view=sql-server-ver17):
  extensão multi-banco AG/MI link permanece preview desde CU9.
- [SSMS 22](https://learn.microsoft.com/en-us/ssms/release-notes-22):
  versão corrente observada `22.11.0`, 13/10/2026; SqlClient `6.1.6`,
  MSODBCSQL `18.7.1.1`. Corrige o baseline incompleto em 22.10.2.
- [Ciclo SQL Server 2016](https://learn.microsoft.com/en-us/lifecycle/products/sql-server-2016)
  e [SQL Server 2017](https://learn.microsoft.com/en-us/lifecycle/products/sql-server-2017):
  conferir datas/fuso e cobertura ESU contratada por instância. Suporte estendido
  padrão de 2016 já terminou; não confundir disponibilidade de ESU com adesão.
- Azure Fsv2/AMQP: anúncios do briefing ainda sem nova confirmação nesta execução;
  não usar datas anteriores como pré-condição de uma migração automática.

## Próxima ação e dívida de automação

Resolver por inventário/secret store governados o alvo SQL HML e a identidade
somente leitura, confirmar transporte físico e vincular esta sonda a uma action
tipada no Gateway. Em seguida executar uma vez no SHA publicado e obter readback.
Só depois habilitar as frentes mutantes da matriz, cada uma com autorização
específica, proteção de concorrência, rollback e E2E. Atualizar o mesmo TODO-37;
não abrir outro TODO para substituir o bloqueio.
