# ADR 0016 — Contratos Python ↔ Rust v1.0.0 (T1)

- **Status:** aceito
- **Data:** 2026-11-13
- **Escopo:** T1 do roadmap — congelar os contratos serializados entre
  `nexus_core` (Python) e `core/nexus-daemon` (Rust)
- **Depende de:** ADR 0004 (core Rust), ADR 0006 (Secure Agency), ADR 0015
  (Execution Gate obrigatório)

---

## Contexto

O Python e o Rust já trocavam dados de evento e de execução, mas cada lado tinha
sua própria noção de formato:

- `nexus_core/events.py` definia `Event` e `dedup_key`, enquanto o Rust tinha
  `events::types::Event` com validação própria;
- o `ApprovalManager` calculava o hash canônico dentro de um método privado;
- não havia schema versionado, fixtures compartilhados nem garantia de que os
  dois lados rejeitassem as mesmas instâncias inválidas;
- o IPC Rust (T4) ainda não existia, mas precisaria de um wire estável para não
  reinventar o formato.

Sem um contrato congelado, mover autoridade para o Rust (T4–T6) arriscava
divergência silenciosa: um lado aceitando `dedup_key` no wire, outro rejeitando;
um lado canonicalizando `params` de forma diferente e invalidando aprovações
legítimas.

Restrições:

- não alterar assinaturas públicas do runtime Python;
- manter os dados já persistidos legíveis; mudanças de canonicalização de hash
  são fail-closed e documentadas;
- validação determinística, sem depender de LLM;
- os mesmos fixtures devem ser validados pelos dois lados;
- não expor API remota antes do gate Rust.

---

## Decisão

1. **JSON Schema Draft 2020-12 como fonte da verdade.** Oito schemas em
   `contracts/schemas/` cobrem evento, solicitação de autorização, resultado de
   autorização, solicitação de aprovação, registro de aprovação, decisão de
   aprovação, resultado de execução e tool spec. Todos usam
   `additionalProperties: false` e `$id` no namespace
   `urn:nexus:contracts:v1:*`.
2. **Fixtures compartilhados e normativos.** `contracts/fixtures/manifest.json`
   indexa fixtures válidas e inválidas por schema, além dos vetores de hash
   de evento e aprovação (ASCII, não-ASCII e numérico). Python e Rust leem os
   mesmos arquivos.
3. **Wire de evento sem campos derivados.** O contrato usa `dedup: bool`;
   `dedup_key` continua derivado e não trafega. `Event.to_dict()` permanece uma
   projeção de UI, não o wire.
4. **Hash de aprovação centralizado e canônico.** `approval_request_hash()` em
   `nexus_core/approval.py` é a implementação normativa; o
   `ApprovalManager._canonicalize_and_hash()` delega para ela. A
   canonicalização v1.0.0 usa **JSON Canônico RFC 8785 (JCS)** nos dois lados
   (`rfc8785` no Python, `serde_jcs` no Rust), com domínio I-JSON. Três vetores
   fixam ASCII, não-ASCII e números em notação científica. `event.dedup_key`
   também passou a usar JCS para paridade Python ↔ Rust.
5. **Validação explícita nos dois lados.** Python usa `jsonschema` com um
   checker de `date-time` baseado em `parse_timestamp`; Rust usa
   `jsonschema` como dev-dependency (`default-features = false`, sem rede). O
   módulo Rust `contracts.rs` é **test-only** (`#[cfg(test)]`) até o T4.
6. **Versão única `1.0.0`.** `CONTRACT_VERSION` no Python e no Rust deve casar
   com `manifest.json`; os testes falham se houver divergência.

---

## Consequências

- O wire v1.0.0 está congelado e coberto por testes que falham se qualquer lado
  divergir de schema, fixture ou canonicalização.
- Aprovações existentes com strings, booleanos e inteiros no domínio seguro
  mantêm o hash. Parâmetros não-ASCII, floats em notação científica ou números
  fora do domínio I-JSON (±(2^53−1), sem `NaN`/`Infinity`) podem mudar de
  representação e precisam ser reemitidos (fail-closed).
- O `dedup_key` de eventos passa a usar JCS para o payload. Eventos já
  persistidos mantêm seu `dedup_key` gravado; apenas novos cálculos usam a
  canonicalização v1. Isso evita duplicatas entre Python e Rust no T2.
- O Rust ainda não é autoridade: os tipos de contrato são compilados apenas em
  testes. Promovê-los a produção é parte do T4, junto com o IPC.
- Campos desconhecidos passam a ser rejeitados explicitamente. Isso é
  intencional e fail-closed, mas exige disciplina de versionamento.
- A dependência `jsonschema` entra no runtime Python e como dev-dependency Rust.
  O lock Python foi atualizado; o Rust usa features mínimas para não introduzir
  cliente HTTP/TLS no daemon. `rfc8785` entra no runtime Python e `serde_jcs` no
  runtime Rust para a canonicalização JCS.

---

## Validação

- `tests/test_contracts.py`: cobertura de versão/manifesto, fixtures válidas e
  inválidas, roundtrip de evento, rejeição de `dedup_key` no wire, parsing real
  de timestamp, objetos de domínio (`GateResult`, `ActionResult`, `ToolSpec`),
  aprovação e os vetores de hash de evento e aprovação (ASCII, não-ASCII e
  numérico).
- `core/nexus-daemon/src/contracts.rs`: 7 testes cobrindo a mesma versão,
  fixtures, desserialização nos tipos Rust (incluindo `events::types::Event` de
  produção) e os vetores de hash de evento e aprovação.
- `cargo test` no daemon: 34 testes passando.
- Suíte Python completa executada após o T1 (contagem no `README.md`).

---

## Próximo passo

T2 — Event Fabric único: outbox → publish, retry/backoff, dead-letter e
consumidor idempotente, com bridge Python ↔ Rust sobre o contrato de evento
v1.0.0. O contrato de autorização só será exercido pelo IPC no T4.
