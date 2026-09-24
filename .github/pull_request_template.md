## Objetivo

Descreva a menor mudança necessária e o problema que ela resolve.

## Segurança de dados

- [ ] Não inclui dados reais, dumps, bancos, backups, segredos ou payloads sensíveis.
- [ ] Dados de teste são sintéticos.
- [ ] A mudança preserva a fronteira com `reqsys-v2-enterprise-real`.

## Evidência

- [ ] HEAD/SHA da branch registrado.
- [ ] Branch atualizada em relação à `main`.
- [ ] Data Safety Gate executado no SHA atual.
- [ ] E2E positivo executado quando aplicável.
- [ ] Caso negativo/controle executado quando aplicável.
- [ ] Idempotência/replay validado quando aplicável.
- [ ] Evidência não contém valores de negócio sensíveis.

## Governança

- [ ] `main` está com proteção/ruleset efetivo antes do merge.
- [ ] Required checks correspondem aos nomes reais do Data Safety Gate.
- [ ] Sem force-push, bypass de gate ou alteração administrativa implícita.

> Se a API indicar `main.protected=false`, este PR deve permanecer draft com indicação explícita **NUNCA MERGEAR**.
