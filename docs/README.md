# Documentação do NEXUS

Esta página é o índice da documentação do projeto. Para o estado atual, use o
`ROADMAP.md` e a seção de estado da arquitetura mestre; auditorias e ADRs
antigos registram o que era verdadeiro na data indicada neles.

## Comece por aqui

| Documento | Para que serve |
|---|---|
| [`README.md`](../README.md) | Visão geral, instalação local e comandos principais |
| [`ROADMAP.md`](../ROADMAP.md) | Etapas, dependências e estado de entrega |
| [`NEXUS_MASTER_ARCHITECTURE.md`](NEXUS_MASTER_ARCHITECTURE.md) | Arquitetura implementada e limites conhecidos |
| [`NEXUS_ARCHITECTURE_DECISIONS.md`](NEXUS_ARCHITECTURE_DECISIONS.md) | Índice das decisões arquiteturais |
| [`NEXUS_TECHNICAL_DEBT.md`](NEXUS_TECHNICAL_DEBT.md) | Registro detalhado de dívida técnica |

## Implantação e operação

| Documento | Para que serve |
|---|---|
| [`NEXUS_T7_EXECUTION_PLAN.md`](NEXUS_T7_EXECUTION_PLAN.md) | Tarefas para preparar o Acer Nitro V15 como servidor |
| [`runbooks/T7_VALIDATION.md`](runbooks/T7_VALIDATION.md) | Comandos e evidências para validar o T7 no Acer |
| [`NEXUS_T8_IDENTITY_DESIGN.md`](NEXUS_T8_IDENTITY_DESIGN.md) | Design e plano de entrega de identidade e permissões |
| [`../SECURITY.md`](../SECURITY.md) | Controles e limites de segurança do runtime |

## Referência e histórico

| Documento | Escopo |
|---|---|
| [`../ARCHITECTURE.md`](../ARCHITECTURE.md) | Resumo curto da arquitetura atual |
| [`../TECHNICAL_DEBT.md`](../TECHNICAL_DEBT.md) | Resumo da dívida técnica e link para o registro detalhado |
| [`../PERFORMANCE_BASELINE.md`](../PERFORMANCE_BASELINE.md) | Medições históricas; não representa o Acer Nitro V15 |
| [`../SECURITY_REVIEW.md`](../SECURITY_REVIEW.md) | Revisão histórica e estado pós-T6 |
| [`../PROJECT_AUDIT.md`](../PROJECT_AUDIT.md) | Auditoria histórica com atualização de estado atual |
| [`../RELEASE_AUDIT.md`](../RELEASE_AUDIT.md) | Auditoria da entrega de 2026-11-12; não é aprovação da revisão atual |
| [`audit/NEXUS_REPOSITORY_AUDIT.md`](audit/NEXUS_REPOSITORY_AUDIT.md) | Registro histórico da auditoria inicial |
| [`../contracts/README.md`](../contracts/README.md) | Contratos wire compartilhados entre Python e Rust |
| [`../contracts/identity/README.md`](../contracts/identity/README.md) | Catálogo versionado de papéis e escopos |

Os documentos em [`adr/`](adr/) preservam decisões e validações por data. As
contagens de testes neles são históricas; consulte a seção “Validação” do ADR
mais recente para saber a que revisão cada número se refere.

## Estado resumido — 2026-11-20

- O núcleo cognitivo, a memória híbrida, o Event Fabric, os agentes e a
  simulação têm implementação no repositório.
- O T6 implementa a autoridade de execução no daemon Rust, mas
  `control_plane.enabled` permanece `false` por padrão.
- O T7 está em andamento. O Dell Inspiron é a estação de desenvolvimento; o
  Acer Nitro V15 continua sendo o servidor alvo. Instalação, rede, GPU/Ollama e
  restore do backup ainda precisam de validação no Acer.
- A fundação T8.1–T8.4 está no repositório. Pairing dos dispositivos,
  integração da identidade ao gate e clientes do diagrama continuam pendentes.
- Gateway multi-dispositivo, mesh de LLMs, DR completo, voz e robótica são
  etapas futuras do roadmap.

## Convenções editoriais

- Datas de estado usam `AAAA-MM-DD`.
- O roadmap é a fonte para progresso e dependências; a arquitetura mestre
  descreve o código presente; runbooks descrevem operações verificáveis.
- Um resultado de teste sempre deve trazer a data, o comando e o escopo. Não
  reutilize contagens antigas como se fossem uma execução atual.
- Planos e diagramas-alvo devem ser identificados como planejados até que haja
  evidência de implantação.
