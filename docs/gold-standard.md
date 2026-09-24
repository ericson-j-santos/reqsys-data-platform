# Padrão ouro — ReqSys Data Platform

Este documento define o critério objetivo para classificar o repositório como padrão ouro.

## 1. Fronteira e segurança

- Nenhum dado real, dump, banco, backup, segredo ou payload sensível versionado.
- Dados de teste exclusivamente sintéticos.
- Models SQLAlchemy e migrations Alembic transacionais do produto permanecem no repositório do ReqSys Produto.
- Evidências contêm somente metadados não sensíveis, hashes, contagens e identificadores técnicos.

## 2. Engenharia

- Mudanças pequenas, idempotentes e reversíveis quando aplicável.
- Backup só é considerado válido com restore testado.
- Migração entre engines exige pré-validação, pós-validação e controle de duplicidade.
- Contratos de dados são versionados e compatibilidade é validada.
- Falhas são explícitas e fail-closed.

## 3. CI e cadeia de suprimento

O Data Safety Gate deve comprovar no SHA atual:

- `repository-hygiene`;
- `backup-restore-e2e`;
- `sqlite-postgres-e2e`;
- `data-quality-e2e`.

Além disso:

- GitHub Actions devem usar referência imutável por SHA completo;
- checkout deve usar `persist-credentials: false`;
- jobs devem possuir timeout;
- dependências devem possuir atualização automatizada;
- permissões do workflow devem seguir menor privilégio.

## 4. Governança de repositório

A `main` precisa ter enforcement real com:

- Pull Request obrigatório;
- required status checks estritos para os quatro jobs do Data Safety Gate;
- administradores sujeitos à proteção;
- force-push bloqueado;
- exclusão da branch bloqueada;
- CODEOWNERS versionado.

A prova obrigatória é uma leitura independente da API retornando `protected=true`.

## 5. Validação sem falso positivo

Todo incremento funcional deve:

- usar evidência do mesmo SHA;
- executar caso positivo;
- executar controle negativo quando aplicável;
- realizar leitura independente do efeito;
- validar idempotência/replay quando aplicável;
- não reutilizar evidência antiga ou residual.

## 6. Estado conhecido em 23/09/2026

- Data Safety Gate pós-merge do SHA `721a1c57c21b0732bfaa1fac62a8a1af531fe6d0`: aprovado.
- Backup/restore, SQLite→PostgreSQL e qualidade de dados contratual: implementados na `main`.
- Bloqueio P0: proteção efetiva da `main` ainda precisa ser comprovada por `protected=true`.
- Issue canônica do bloqueio: #4.

Enquanto esse último item não estiver comprovado, o estado correto é **parcial**, não "padrão ouro concluído".
