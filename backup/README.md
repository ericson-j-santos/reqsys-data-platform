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
