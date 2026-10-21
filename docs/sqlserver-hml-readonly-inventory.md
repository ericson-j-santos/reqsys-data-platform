# Inventário SQL Server — homologação (somente leitura)

**Estado:** coletor implementado e testado apenas com conexões simuladas. Não há confirmação de instância HML acessível; não é homologação E2E.

## Objetivo e limites

Registrar versão/build, edição, compatibility level, estado do Query Store e criptografia TLS em uma instância SQL Server HML já autorizada. Não modifica dados, configuração, banco, logins nem infraestrutura. Consultas fixas usam somente `SERVERPROPERTY`, `DB_NAME`, `IS_MEMBER`, `sys.databases`, `sys.database_query_store_options` e `sys.dm_exec_connections`; nunca consulta tabelas de negócio.

O executor precisa cumprir as regras em `ericson-j-santos/chatgpt-operational-rules` (preflight, `SESSION_LAUNCH_OK`, Command Gateway, SHA exato e escopo). A presença deste código **não** autoriza acesso físico, mudança de ambiente, execução em produção ou criação de secrets.

## Configuração exigida no canal governado

| Variável | Uso |
|---|---|
| `SQLSERVER_INVENTORY_ENV` | Obrigatoriamente `HML` |
| `SQLSERVER_INVENTORY_APPROVED` | Marcador `HML-READONLY`, depois de autorização real pelo controle de execução; não é autorização isolada |
| `SQLSERVER_EXPECTED_SERVER` | Nome SQL Server retornado por `SERVERPROPERTY('ServerName')` para comparação estrita |
| `SQLSERVER_EXPECTED_DATABASE` | Nome da base HML autorizada; bancos de sistema são rejeitados |
| `SQLSERVER_HML_DSN` | Somente por secret store/injeção protegida; ODBC 17/18, `Encrypt=Yes`, `TrustServerCertificate=no`, `ApplicationIntent=ReadOnly`. Sem impressão em logs |
| `SQLSERVER_INVENTORY_SOURCE_SHA` | SHA Git completo (40 caracteres) do código executado |
| `SQLSERVER_CORRELATION_ID` | Identificador único e rastreável da execução |

O DSN só é aceito com campos simples separados por ponto e vírgula; formatos ODBC com valores entre chaves contendo ponto e vírgula falham fechados. Antes de abrir uma conexão, o valor `Server=` do DSN precisa corresponder a `SQLSERVER_EXPECTED_SERVER` (sem distinção entre maiúsculas e minúsculas); divergência produz `dsn_server_mismatch` e impede qualquer consulta ao servidor incorreto. A identidade real continua sendo comparada após a conexão, pois essa conferência estática não autentica o destino. Identidade administrativa (sysadmin, db_owner, db_datawriter ou db_ddladmin) é recusada. Esses controles **não substituem** validação de permissões no servidor nem garantem ausência de grants diretos.

## Procedimento seguro

1. Identificar a instância e base **HML reais** via fonte de infraestrutura governada; verificar acesso e driver ODBC sem imprimir credenciais.
2. Confirmar que a sessão autorizada opera no SHA esperado e que os parâmetros acima vêm de configuração protegida; caso contrário, interromper sem conexão.
3. O executor invoca `scripts/sqlserver_hml_inventory.py` com as variáveis de ambiente aprovadas, sem argumentos livres e sem fallback por terminal irrestrito.
4. Registrar JSON sanitizado como artifact associado a `correlation_id`, SHA, ambiente e horário. Conferir valores em leitura independente.
5. Reportar build, nível de compatibilidade, Query Store e TLS. `openjson_compatible=false` é resultado de inventário, não falha do coletor. Acesso negado à DMV/TLS ou diferença de servidor bloqueiam a certificação.
6. Só então planejar testes de `OPENJSON`, `SESSION_CONTEXT`, permissões e regressões. Não alterar compatibility level, não executar DDL/DML nem promover produção.

## Resultado e critérios

A saída `status=observed` contém fingerprints SHA-256 truncados para servidor/banco, `product_version`, `edition`, `compatibility_level`, `openjson_compatible`, estado/razão do Query Store, TLS e SHA/correlação. Não registra hostname, nome de banco ou DSN em claro. Erros retornam apenas `status=blocked` e código sanitizado; falha de driver/permissão permanece bloqueio, não sucesso.

Os testes unitários usam conexão simulada, com cenário esperado, ambiente PROD rejeitado antes da conexão, certificado/TLS inválidos, identidade privilegiada, alvo divergente, compatibility level 120, idempotência e sanitização de erros. São **insuficientes para comprovar SQL Server real**. O gate HML exige execução no alvo governado e readback independente.

Checkpoint: [TODO-37](https://app.notion.com/p/3f74c12bae7c8105a17fe7bcf1d4ff94). Matriz: [SQL Server 2022–2025](https://docs.google.com/spreadsheets/d/1J0Xq7Iu11B5KX90TZC7Q3NYJR3oYjhjU2pouDRtkKho/edit).
