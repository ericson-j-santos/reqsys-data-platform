# Migrações entre engines

Este diretório contém ferramentas de migração/cutover entre mecanismos de banco.

Prioridade inicial: consolidar a transição de SQLite para PostgreSQL onde ainda
existir runtime compartilhado em SQLite.

Toda migração deve implementar:
- preflight;
- validação de origem/destino;
- execução retomável;
- comparação pós-migração;
- controle negativo;
- rollback documentado;
- evidência por ambiente, SHA e `correlation_id`.

## SQLite -> PostgreSQL v1

`sqlite_to_postgres.py` migra tabelas compatíveis para um **schema isolado** no
PostgreSQL. O incremento inicial é deliberadamente fail-closed: somente contratos
que podem ser reproduzidos com segurança são aceitos.

### Garantias

- abre a origem SQLite em modo somente leitura;
- exige `PRIMARY KEY` em toda tabela para replay idempotente;
- rejeita tipos, defaults, FKs e índices secundários ainda não contratados;
- aceita somente `local`, `dev`, `ci` e `test`; HML/STG/PROD ficam bloqueados;
- usa `correlation_id` + fingerprint da origem como chave de idempotência;
- faz UPSERT por chave primária;
- compara quantidade e SHA-256 canônico de todas as linhas após a carga;
- grava evidência em `<schema>._migration_runs`;
- executa criação/carga/verificação em uma transação PostgreSQL.

### Preflight

A conexão PostgreSQL é lida da variável de ambiente `POSTGRES_DSN`; não passe
credenciais na linha de comando.

```bash
python migrations/sqlite_to_postgres.py \
  --source /caminho/base.sqlite \
  --schema reqsys_migration_dev \
  --correlation-id migration-001 \
  --environment dev \
  --preflight-only
```

### Migração

```bash
python migrations/sqlite_to_postgres.py \
  --source /caminho/base.sqlite \
  --schema reqsys_migration_dev \
  --correlation-id migration-001 \
  --environment dev
```

Repetir exatamente a mesma entrada e o mesmo `correlation_id` não duplica
registros. Reusar o `correlation_id` com uma origem diferente falha fechado.

### Rollback

Antes do commit, qualquer falha desfaz automaticamente a transação PostgreSQL.
Após uma migração concluída, o rollback técnico deste v1 é remover **apenas o
schema isolado criado para a migração**, depois de preservar a evidência
necessária. Essa remoção é destrutiva e deve ser uma ação governada separada.

O SQLite de origem deve permanecer disponível em modo somente leitura durante o
período de validação/cutover. Este repositório não promove automaticamente HML,
STG ou PROD.
