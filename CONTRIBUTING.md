# Contribuição

## Fluxo obrigatório

1. Ler `AGENTS.md` e as regras canônicas em `ericson-j-santos/chatgpt-operational-rules`.
2. Partir da `main` corrente e registrar o SHA-base.
3. Trabalhar em branch dedicada.
4. Fazer a menor mudança segura, reversível e testável.
5. Executar o Data Safety Gate no SHA atual.
6. Para incremento funcional, executar E2E no maior escopo disponível, com controle negativo quando aplicável.
7. Abrir Pull Request com evidência vinculada ao mesmo SHA.
8. Revalidar mergeabilidade, checks e proteção da `main` antes do merge.

## Regra de dados

Nunca versionar dados reais, bancos, dumps, backups, credenciais, tokens, chaves, payloads reais de integrações ou exportações de ambientes.

## Regra de governança

A existência de um PR, de um check verde ou da palavra "draft" não substitui enforcement.

Se a leitura independente da API do GitHub indicar `main.protected=false`:

- manter o PR em draft;
- incluir a indicação **NUNCA MERGEAR**;
- registrar o bloqueio em issue;
- não fazer merge manual, via API ou automação até a proteção ser efetiva.

## Critério de conclusão

Uma mudança só é concluída quando código, documentação, CI e evidência correspondem ao mesmo SHA, o comportamento foi comprovado no maior escopo executável e nenhum bloqueio de governança permanece oculto.
