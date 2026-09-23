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

## SQLite -> PostgreSQL v2

O contrato agora é dirigido pelo schema SQLite efetivamente materializado. O
analisador `sqlite_schema_contract.py` lê apenas metadados e falha fechado para
semânticas que ainda não possuem conversão explícita.

Contratos suportados no v2, extraídos da `main` atual do ReqSys:
- `DATE` e `DATETIME` (DATETIME é tratado como UTC e migrado para TIMESTAMPTZ);
- JSON -> JSONB;
- NUMERIC/DECIMAL preservando precisão e escala;
- defaults `CURRENT_TIMESTAMP`, literais, números e NULL;
- índices secundários e índices únicos;
- foreign keys, inclusive ações ON UPDATE/ON DELETE permitidas pelo SQLite;
- PK inteira simples como identity PostgreSQL, com sequence sincronizada após a carga.

A fonte do contrato do produto fica em
`contracts/reqsys-product-schema-source.json`. O CI faz checkout do SHA fixado,
materializa `Base.metadata` em SQLite usando o próprio ReqSys, analisa o schema,
migra todas as tabelas para PostgreSQL 16 e verifica tabelas, índices, FKs e
replay idempotente por leitura independente.

### Analisar um SQLite

```bash
python migrations/sqlite_schema_contract.py \
  --source /caminho/base.sqlite \
  --require-supported \
  --json /tmp/schema-report.json
```

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
registros. Reusar o `correlation_id` com origem diferente falha fechado.

### Limites e rollback

Partial/expression indexes, colunas geradas, tipos sem mapeamento explícito e
referências FK fora do escopo continuam bloqueados.

Antes do commit, qualquer falha desfaz automaticamente a transação PostgreSQL.
Após uma migração concluída, o rollback técnico continua sendo remover somente o
schema isolado criado para a migração, em ação governada separada.

HML/STG/PROD não são habilitados por este código. O SQLite de origem deve
permanecer disponível em modo somente leitura durante validação/cutover.
