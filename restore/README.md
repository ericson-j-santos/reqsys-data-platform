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
