# Migrações entre engines

Este diretório contém ferramentas de migração/cutover entre mecanismos de banco.

Prioridade inicial: consolidar a transição de SQLite para PostgreSQL onde ainda
existir runtime compartilhado em SQLite.

Toda migração deve implementar:
- preflight;
- validação de origem/destino;
- execução retomável;
- comparação pós-migração;
- controle negativo;
- rollback documentado;
- evidência por ambiente, SHA e `correlation_id`.
