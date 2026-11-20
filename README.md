# NEXUS

Plataforma cognitiva local-first para Linux, escrita em Python e Rust. O
repositório reúne raciocínio e planejamento, memória persistente, modelos locais,
agentes e execução de ferramentas governada por política e aprovação humana.

## Estado do projeto — 2026-11-20

- O núcleo cognitivo, o Event Fabric, a memória híbrida, o Knowledge Graph, os
  agentes, a simulação, a API local e o Web HUD estão implementados em diferentes
  níveis de maturidade.
- O T6 entrega o Execution Gate autoritativo no daemon Rust. O modo autoritativo
  é opt-in e segue desativado por padrão em `config/nexus.toml`.
- O T7 prepara o Acer Nitro V15 como servidor. O Dell Inspiron 15 3511 é a
  estação de desenvolvimento; a implantação de serviços, rede, GPU/Ollama e
  backup ainda precisa ser validada no Acer.
- A fundação de identidade T8.1–T8.4 está no workspace. Pairing real dos
  dispositivos, integração completa com o gate e clientes móveis ainda estão
  pendentes.
- O catálogo de automação atual tem sete ferramentas de diagnóstico somente
  leitura. Ferramentas de escrita seguras são trabalho futuro.

O resumo completo está em [`docs/README.md`](docs/README.md), o progresso em
[`ROADMAP.md`](ROADMAP.md) e a topologia real em
[`docs/NEXUS_MASTER_ARCHITECTURE.md`](docs/NEXUS_MASTER_ARCHITECTURE.md).

## Arquitetura

O NEXUS mantém um runtime Python local para cognição e interfaces, com um daemon
Rust para controle determinístico. SQLite é o armazenamento transacional
principal; FTS5 e LanceDB sustentam a busca híbrida. O Ollama pode fornecer
inferência local. O diagrama multi-dispositivo é o alvo de T7–T20, não uma
implantação já validada.

O API FastAPI/WebSocket aceita apenas endereços de loopback e usa um token local.
O daemon Rust assume a autoridade final quando `[control_plane] enabled = true`;
falhas nesse modo negam execução. O token único da API ainda não representa
identidades separadas por pessoa ou dispositivo.

Veja [`ARCHITECTURE.md`](ARCHITECTURE.md) para um resumo e a
[arquitetura mestre](docs/NEXUS_MASTER_ARCHITECTURE.md) para detalhes por plano.

## Requisitos e instalação local

- Linux
- Python 3.11 ou superior
- Rust toolchain para compilar daemon e sentinel
- `bubblewrap` para execução isolada de ferramentas
- Ollama opcional para inferência local

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
python -m nexus_core status
```

Para iniciar a API local:

```bash
python -m nexus_core serve
```

O token da API é criado em `data/secrets/api_token` quando necessário. Consulte
[`SECURITY.md`](SECURITY.md) antes de habilitar execução de ferramentas ou
provedores remotos.

## Comandos úteis

```bash
python -m nexus_core --help
python -m nexus_core status
python -m nexus_core llm models
python -m nexus_core task run --prompt "Analise os logs e sugira melhorias"
```

O arquivo `config/nexus.toml` é um exemplo de configuração local. Confira quais
tags estão instaladas no Ollama do host antes de usar a cadeia configurada. O
provedor remoto de exemplo vem desabilitado e sem credenciais.

## Dados e segurança

- A API escuta em `127.0.0.1:8765`; não há TLS nem autenticação de dispositivo
  para acesso remoto nesta etapa.
- Aprovações são vinculadas à ação e consumidas antes da execução para impedir
  replay. A execução passa pelo gate, política e trilha de auditoria.
- O daemon usa IPC local por Unix Domain Socket. Não exponha esse socket pela
  rede.
- Provedores LLM remotos exigem HTTPS e `authorized = true`; a política de
  egress considera os níveis `PUBLIC`, `PRIVATE` e `SECRET`.
- O armazenamento padrão é local. Criptografia do Task Store é opcional e deve
  ser configurada explicitamente.

## Desenvolvimento e validação

```bash
pytest tests/
cargo test --manifest-path core/nexus-daemon/Cargo.toml
cargo test --manifest-path core-rust/Cargo.toml
```

A última execução completa documentada no ADR 0021 (T6, 2026-11-16) registrou
516 testes Python, 12 subtests e 86 testes Rust no daemon. Essa contagem é um
baseline histórico: as alterações T8 posteriores ainda precisam de uma
execução completa no Acer antes de o T7 ser fechado. Consulte
[`docs/README.md`](docs/README.md) para distinguir resultados históricos de
validações atuais.

## Documentação

| Documento | Conteúdo |
|---|---|
| [`docs/README.md`](docs/README.md) | Índice e estado resumido |
| [`ROADMAP.md`](ROADMAP.md) | Entregas e dependências |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Arquitetura resumida |
| [`docs/NEXUS_MASTER_ARCHITECTURE.md`](docs/NEXUS_MASTER_ARCHITECTURE.md) | Estado real por plano |
| [`docs/NEXUS_ARCHITECTURE_DECISIONS.md`](docs/NEXUS_ARCHITECTURE_DECISIONS.md) | Índice de ADRs |
| [`docs/NEXUS_TECHNICAL_DEBT.md`](docs/NEXUS_TECHNICAL_DEBT.md) | Dívida técnica detalhada |
| [`docs/NEXUS_T7_EXECUTION_PLAN.md`](docs/NEXUS_T7_EXECUTION_PLAN.md) | Plano de host e rede |
| [`docs/runbooks/T7_VALIDATION.md`](docs/runbooks/T7_VALIDATION.md) | Validação operacional do T7 no Acer |
| [`docs/NEXUS_T8_IDENTITY_DESIGN.md`](docs/NEXUS_T8_IDENTITY_DESIGN.md) | Identidade e permissões |
| [`SECURITY.md`](SECURITY.md) | Controles e riscos residuais |
