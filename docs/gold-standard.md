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

## 6. Estado evidenciado em 23/09/2026

O repositório atende aos critérios deste documento no estado abaixo:

- `main`: `a992d15f6fc4a642ee417ffd93576cccba342c37`;
- Data Safety Gate no mesmo SHA: os quatro jobs obrigatórios concluíram com sucesso;
- branch protection: `protected=true`;
- required status checks: enforcement `everyone` para `repository-hygiene`, `backup-restore-e2e`, `sqlite-postgres-e2e` e `data-quality-e2e`;
- Pull Request obrigatório, administradores sujeitos à proteção, force-push bloqueado e exclusão da branch bloqueada;
- GitHub Actions fixadas por SHA, checkout sem persistência de credenciais, timeouts e permissões mínimas;
- Dependabot ativo para GitHub Actions e Python;
- CODEOWNERS versionado;
- backup/restore, SQLite→PostgreSQL e qualidade de dados contratual implementados e cobertos por E2E;
- issue canônica do P0 de governança: #4, encerrada como concluída;
- execução governada que aplicou e validou a proteção: `reqsys-v2-enterprise-real` run `35943866177`, conclusão `success`;
- evidência sanitizada da proteção: artifact `reqsys-data-platform-main-protection-pc24x7-35943866177`, digest `sha256:687d1c3e8b7db7f934b9871e703aba45a333df4123c47131ef40437460f1ec97`.

Com essas evidências, o estado atual é **padrão ouro concluído**. Mudanças posteriores devem preservar estes critérios; regressão em qualquer item rebaixa o estado até nova evidência no SHA corrente.
