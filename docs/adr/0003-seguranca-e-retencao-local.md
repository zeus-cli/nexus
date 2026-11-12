# ADR 0003 — Dados locais, bind da API e retenção

> **Nota de atualização (2026-11-12):** decisão histórica preservada; as contagens de testes referem-se à data original. Estado atual e validação consolidados em `docs/adr/0014-endurecimento-auditoria-2026-11-12.md`.

- **Status:** aceito
- **Data:** 2026-11-03
- **Escopo:** permissões do diretório de dados, transporte HTTP local e retenção do event store
- **Substitui parcialmente:** defaults operacionais do ADR 0001; o histórico de decisão permanece válido para a Fase 1.

## Contexto

A revisão do backend encontrou arquivos de eventos e logs legíveis por outras
contas locais, possibilidade de bind da API em interfaces de rede sem TLS e
remoção periódica de eventos após 90 dias mesmo sem uma decisão recente do
usuário. Esses comportamentos não atendem ao objetivo de privacidade local-first.

## Decisões

1. **Arquivos privados por padrão.** O diretório configurado de dados e seus
   filhos imediatos usam `0700`; arquivos regulares existentes nesse nível
   usam `0600`. Banco, backups, logs e logs rotacionados são criados com `0600`.
   Helpers recusam symlinks nos caminhos de arquivo gerenciados pelo runtime.
2. **API somente em loopback.** `api.host` precisa ser um literal de IP de
   loopback (`127.0.0.1` ou `::1`, incluindo outros endereços loopback). A
   configuração e a opção `serve --host` passam pela mesma regra. A API não
   possui TLS; hosts externos são recusados.
3. **Retenção desativada inicialmente.** `events.retention_days = 0` preserva
   eventos sem poda automática. Um valor positivo habilita a política. O estado
   efetivo aparece em `nexus status`; a poda manual continua explícita e o
   backup SQLite pode ser feito com `nexus db backup`.

## Consequências

- Dados em `data/` deixam de ser compartilhados com outros usuários locais.
- Consumidores de API remotos precisam esperar por uma implementação TLS
  revisada; não devem contornar a validação com proxy sem uma decisão própria.
- O banco pode crescer sem limite quando retenção estiver desativada. O operador
  deve configurar dias de retenção ou criar backups e remover dados de forma
  deliberada.
- A privacidade por modo Unix cobre contas locais separadas, não processos
  executados como o mesmo usuário nem cópias feitas para destinos externos.
