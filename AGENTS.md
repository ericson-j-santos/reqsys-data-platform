# AGENTS.md

## Escopo

Este repositório é a plataforma de dados compartilhada do ReqSys.

Antes de alterar conteúdo:
1. aplicar as regras canônicas de `ericson-j-santos/chatgpt-operational-rules`;
2. preservar a fronteira com `reqsys-v2-enterprise-real`;
3. não introduzir dados reais, segredos, dumps ou identificadores sensíveis;
4. preferir mudanças pequenas, reversíveis, testáveis e idempotentes;
5. validar compatibilidade de contratos antes de publicar mudanças;
6. registrar evidência vinculada ao SHA alterado.

## Não mover sem decisão arquitetural explícita

- models SQLAlchemy transacionais do produto;
- migrations Alembic da aplicação;
- regras de negócio;
- dados de produção.

## Princípios

- PostgreSQL como referência preferencial para ambientes compartilhados.
- Migrações devem possuir validação pré/pós e estratégia de rollback.
- Backup só é concluído quando restore é testável.
- Qualidade e retenção devem falhar de forma observável.
- Dados de teste devem ser sintéticos.
