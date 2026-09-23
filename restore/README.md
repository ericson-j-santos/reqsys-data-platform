# Restore

Restauração é parte do critério de conclusão de backup.

Testes devem validar:
- leitura do artefato;
- integridade/hash;
- restauração em destino isolado;
- consistência mínima do schema e dados;
- tempo observado versus RTO;
- ausência de alteração no ambiente de origem.

Nenhum teste destrutivo deve ser executado em produção.

## SQLite v1

`sqlite_restore.py` falha fechado quando o SHA-256, o nome do artefato, a
integridade do SQLite ou a contagem de linhas divergem do manifesto. O destino
é construído primeiro em arquivo temporário e só depois promovido atomicamente.

```bash
python restore/sqlite_restore.py \
  --artifact /tmp/backup/demo-001.sqlite \
  --manifest /tmp/backup/demo-001.manifest.json \
  --target /tmp/restore/demo-001.sqlite
```

Use `--replace` somente em destino isolado quando a repetição idempotente for
intencional.
