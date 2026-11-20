# Dívida técnica do NEXUS

**Atualizado:** 2026-11-20
**Registro detalhado:** [`docs/NEXUS_TECHNICAL_DEBT.md`](docs/NEXUS_TECHNICAL_DEBT.md)

Este arquivo resume os pontos ativos. O registro detalhado contém histórico,
contexto, mitigação e próximas ações por item.

## Itens que afetam o produto

| Prioridade | Itens em aberto |
|---|---|
| P0 | TD-001/010: nomes e loggers legados `zeus` |
| P1 | TD-007: token local legível pelo mesmo usuário; TD-008: SQLite síncrono em caminhos async |
| P2 | TD-005: catálogo com sete diagnósticos read-only; TD-009: sandbox usa UID 0 interno; TD-019: scheduler fora do runtime; TD-020: criptografia do Task Store opcional; TD-021: limites de durabilidade do Event Fabric |
| P3 | TD-004: sem shell desktop GTK4 nativo |
| P4 | TD-006: modelo e desempenho do servidor Acer ainda precisam de benchmark |

## Entregas parcialmente concluídas

- **T7 — Host e rede:** units e script de backup existem no workspace. Falta
  instalar e validar no Acer Nitro V15, configurar a rede e concluir restore.
- **T8 — Identidade:** T8.1–T8.4 têm implementação no workspace. Pairing,
  integração do gate e clientes por dispositivo seguem pendentes.
- **Control Plane Rust:** TD-013/015/016/022 estão resolvidos no código; a
  ativação, o fail-closed e a recuperação precisam de validação operacional no
  Acer. O modo Rust permanece desativado por padrão.

O Dell Inspiron 15 3511 é a estação de desenvolvimento; o Acer Nitro V15 é o
servidor alvo. A lista de T7/T8 e as dependências estão em [`ROADMAP.md`](ROADMAP.md).
