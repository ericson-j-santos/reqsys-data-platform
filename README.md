# ReqSys Data Platform

Plataforma de dados do ecossistema ReqSys.

## Missão

Centralizar capacidades reutilizáveis de governança, contratos, migração, proteção,
qualidade, observabilidade, backup e recuperação de dados, sem acoplar a evolução
funcional do produto ao ciclo desta plataforma.

## Fronteira inicial

Este repositório contém:
- contratos e schemas compartilhados;
- ferramentas de migração entre engines;
- backup, restore e evidências de recuperação;
- retenção, minimização e qualidade de dados;
- observabilidade de persistência;
- seeds e dados sintéticos;
- documentação arquitetural e testes da plataforma.

Permanece em `reqsys-v2-enterprise-real`:
- models SQLAlchemy transacionais do produto;
- migrations Alembic da aplicação;
- regras de negócio, APIs, frontend e integrações funcionais.

## Segurança

É proibido versionar dados reais, dumps de produção, bancos SQLite reais, backups,
dados pessoais, tokens, credenciais, chaves criptográficas ou exportações de ambientes.

Como este repositório pode ser público, somente exemplos sintéticos e conteúdo
não confidencial são permitidos.

## Arquitetura alvo

```text
ReqSys Produto
      |
      v
contratos versionados
      |
      v
ReqSys Data Platform
  |      |       |
schema  backup  qualidade
  |      |       |
  +------+-------+
         |
     evidências
```

PostgreSQL é a referência preferencial para ambientes compartilhados. SQLite deve
ficar restrito a cenários locais/isolados quando tecnicamente adequado. SQL Server
é uma opção de destino quando houver requisito objetivo.

## Estrutura

- `contracts/` — contratos de dados e compatibilidade.
- `schemas/` — documentação e definições compartilhadas de schema.
- `backup/` — política e automação de backup.
- `restore/` — recuperação e testes de restauração.
- `retention/` — retenção, minimização e descarte.
- `migrations/` — migrações entre engines e ferramentas de cutover.
- `data-quality/` — regras e testes de qualidade.
- `observability/` — métricas, logs e evidências.
- `seed/` — somente dados sintéticos.
- `tests/` — testes da própria plataforma.

## Regra de evolução

Primeiro extrair capacidades compartilhadas. Não mover models SQLAlchemy nem
migrations transacionais do produto até existir contrato estável e teste de
compatibilidade entre os repositórios.
