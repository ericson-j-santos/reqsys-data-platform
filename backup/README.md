# Backup

Objetivo: produzir backups verificáveis, criptografados quando aplicável e
armazenados fora do banco primário.

Todo processo de backup deve registrar:
- engine e ambiente;
- timestamp;
- SHA da automação;
- tamanho;
- hash de integridade;
- RPO aplicável;
- `correlation_id`.

Backup sem teste de restauração não comprova recuperabilidade.

## SQLite v1

`sqlite_backup.py` cria um snapshot consistente usando a API nativa de backup do
SQLite e grava um manifesto JSON com SHA-256, tamanho e contagem por tabela.

Exemplo local com banco sintético:

```bash
python backup/sqlite_backup.py \
  --source /tmp/source.sqlite \
  --output-dir /tmp/backup \
  --correlation-id demo-001 \
  --environment local
```

O artefato de banco e o manifesto são saídas de runtime e nunca devem ser
versionados neste repositório.
