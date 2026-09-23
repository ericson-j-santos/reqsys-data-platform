# Contrato de integração com ReqSys Produto

## Repositórios

- Produto: `ericson-j-santos/reqsys-v2-enterprise-real`
- Dados: `ericson-j-santos/reqsys-data-platform`

## Propriedade

O Produto é proprietário das regras de negócio e do modelo transacional diretamente
usado pela aplicação. A Data Platform é proprietária das capacidades compartilhadas
de governança, proteção, migração, backup/restore, qualidade e observabilidade.

## Dependência

O Produto pode consumir contratos versionados da Data Platform. A Data Platform
não deve depender do código de runtime do Produto para definir sua própria operação.

## Compatibilidade

Mudanças compartilhadas devem:
1. declarar versão;
2. possuir teste de compatibilidade;
3. manter retrocompatibilidade ou fornecer migração explícita;
4. ser validadas contra o consumidor antes da adoção;
5. registrar evidência no SHA exato.

## Banco e migrations

Enquanto a fronteira não for formalmente promovida:
- models SQLAlchemy permanecem no Produto;
- migrations Alembic transacionais permanecem no Produto;
- migrations entre engines e ferramentas de cutover ficam na Data Platform.

## Segurança

Nenhum contrato pode transportar segredo ou dado real. Exemplos devem ser
sintéticos e mínimos.
