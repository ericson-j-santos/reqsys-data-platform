# SQL Server 2025 — teste E2E em laboratório isolado

Escopo exclusivamente de laboratório: SQL Server 2025 Developer em contêiner descartável em runner GitHub-hosted, sem rede, portas publicadas, volume persistente ou conexão PC24x7/HML/PROD. O runtime ReqSys não é alterado.

Workflow de CI restrito aos arquivos deste laboratório e ao disparo explícito. Sem polling ou agenda recorrente. A imagem oficial da Microsoft é fixada por digest já utilizado no contrato de migração DEV. Senha sintética gerada por execução e não registrada.

A conexão dentro do contêiner usa sqlcmd com confiança no certificado autoassinado APENAS no laboratório descartável. Em HML, exige-se TLS e validação de certificado, sem exceção.

Casos executados no motor real: versão 17 e Developer; compatibilidade 170; Query Store READ_WRITE; OPENJSON positivo; OPENJSON inválido bloqueado; insert sintético idempotente com leitura em nova conexão; rollback com leitura independente; SESSION_CONTEXT na conexão original e isolamento em nova conexão.

O artefato registra SHA exato, run_id, digest, versão do engine, resultados e flags hml_verified=false e collector_odbc_e2e_verified=false. Um CI verde não comprova conectividade HML nem autenticação corporativa. A coleta HML requer alvo real, DSN protegido, menor privilégio e executor com Session Launcher/Command Gateway autorizados.

Fonte: https://learn.microsoft.com/en-us/sql/linux/quickstart-install-connect-docker?view=sql-server-ver17
TODO: https://app.notion.com/p/3f74c12bae7c8105a17fe7bcf1d4ff94
