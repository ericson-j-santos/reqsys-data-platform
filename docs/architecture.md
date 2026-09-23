# Arquitetura

## Responsabilidades

A Data Platform fornece capacidades compartilhadas de persistência e governança.
O ReqSys Produto continua dono do comportamento funcional e do modelo transacional
consumido diretamente pela aplicação.

## Fluxo desejado

```text
ReqSys Produto
  -> contrato/schema compatível
  -> PostgreSQL
  -> backup verificável
  -> restore testado
  -> métricas/evidência
```

## Estratégia de banco

1. PostgreSQL: referência para ambientes compartilhados.
2. SQLite: apenas local/isolado quando adequado.
3. SQL Server: somente quando houver requisito objetivo que justifique adoção.

## Regra de migração

Toda migração entre engines deve ter:
- origem e destino identificados;
- contagem e integridade antes/depois;
- idempotência ou mecanismo explícito de retomada;
- rollback documentado;
- `correlation_id`;
- evidência vinculada ao ambiente e SHA.
