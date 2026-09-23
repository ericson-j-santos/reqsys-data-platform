# Qualidade de dados

Regras de qualidade devem ser automatizadas, mensuráveis e versionadas por contrato.

## Implementação v1

O módulo `data_quality.validator` valida bancos SQLite em modo somente leitura e
produz somente evidência agregada, sem registrar valores de negócio.

Dimensões suportadas:
- completude: colunas obrigatórias não podem ser nulas ou texto vazio;
- unicidade: chaves configuradas não podem possuir grupos duplicados;
- integridade referencial: referências configuradas devem existir;
- validade de domínio: valores devem pertencer ao conjunto permitido;
- consistência temporal: `start <= end` para pares configurados;
- idempotência: chaves de idempotência não podem duplicar.

## Contrato

Exemplo mínimo:

```json
{
  "schema_version": 1,
  "tables": {
    "demands": {
      "required": ["external_key", "title", "status"],
      "unique": [["external_key"]],
      "domains": {"status": ["OPEN", "DONE"]},
      "temporal": [{"start": "created_at", "end": "updated_at"}],
      "foreign_keys": [{
        "columns": ["owner_id"],
        "ref_table": "owners",
        "ref_columns": ["id"]
      }],
      "idempotency_keys": [["external_key"]]
    }
  }
}
```

## Execução

```bash
python -m data_quality.validator \
  --source caminho/banco.sqlite \
  --contract caminho/quality-contract.json \
  --correlation-id quality-20260923-001 \
  --environment local
```

Códigos de saída:
- `0`: contrato atendido;
- `2`: violações de qualidade detectadas;
- `3`: contrato, fonte ou pré-condição inválida.

## Segurança e evidência

- a origem é aberta com `mode=ro` e `query_only`;
- WAL pendente é rejeitado no v1 para evitar fingerprint ambíguo;
- o arquivo é re-hasheado após a validação para detectar alteração concorrente;
- a evidência contém SHA-256 do contrato e da fonte, contagens e número de
  violações, mas não inclui valores das linhas;
- ambientes aceitos no v1: `local`, `dev`, `ci` e `test`.

Falhas são fail-closed e reproduzíveis por `correlation_id` + SHA da automação.
