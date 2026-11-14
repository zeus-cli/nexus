# Contratos NEXUS v1.0.0 — Python ↔ Rust

Este diretório é a **fonte da verdade** dos contratos serializados entre o
runtime Python (`nexus_core`) e o Control Plane Rust (`core/nexus-daemon`).
Schemas e fixtures são versionados juntos; nenhum dos lados pode alterar um
contrato sem atualizar esta árvore e os testes dos dois lados.

- **Versão do contrato:** `1.0.0` (declarada em `fixtures/manifest.json`)
- **Dialeto:** JSON Schema Draft 2020-12 (`$schema` em todos os arquivos)
- **Namespace:** `urn:nexus:contracts:v1:<nome>`
- **Validação Python:** `nexus_core/contracts.py` (biblioteca `jsonschema`)
- **Validação Rust:** `core/nexus-daemon/src/contracts.rs` (dev-dependency
  `jsonschema`, sem features de rede)

## Estrutura

```
contracts/
├── README.md
├── schemas/                    # 8 schemas normativos
│   ├── event.schema.json
│   ├── authorization_request.schema.json
│   ├── authorization_result.schema.json
│   ├── approval_request.schema.json
│   ├── approval_record.schema.json
│   ├── approval_decision.schema.json
│   ├── execution_result.schema.json
│   └── tool_spec.schema.json
└── fixtures/
    ├── manifest.json           # índice normativo (válidos, inválidos, vetores)
    ├── valid/                  # 1 fixture válida por schema
    ├── invalid/                # fixtures inválidas (inclui timestamp e severidade)
    └── vectors/                # vetores de hash canônico
        ├── event_dedup.json
        ├── event_dedup_numbers.json
        ├── approval_hash.json
        ├── approval_hash_unicode.json
        └── approval_hash_numbers.json
```

## Regras gerais

1. **Campos desconhecidos são rejeitados.** Todos os schemas usam
   `additionalProperties: false`; os tipos Rust usam `deny_unknown_fields`.
   Campos novos exigem nova versão de contrato.
2. **Wire não transporta campos derivados.** `event.dedup_key` é calculado a
   partir do conteúdo e **não** faz parte do wire. O wire usa `dedup: bool`
   para indicar se o evento participa da deduplicação.
3. **Timestamps de evento são strings ISO-8601 UTC.** O Python valida com
   `parse_timestamp`; o Rust valida com `format: date-time`.
4. **Timestamps de aprovação são números (epoch em segundos).** Isso preserva o
   formato já persistido pelo `ApprovalManager` e pelo journal.
5. **Enums são fechados.** Valores fora do enum são rejeitados nos dois lados.
6. **Hashes exigem domínio I-JSON.** Inteiros fora de ±(2^53−1) e números não
   finitos não podem ser assinados em aprovações (`ValueError`). Em eventos,
   payloads fora desse domínio não recebem `dedup_key` (não são deduplicados,
   mas continuam registráveis). Isso mantém o comportamento idêntico em Python
   e Rust.
7. **`approval_record.params_hash` é SHA-256 hexadecimal minúsculo** com 64
   caracteres, calculado pelo algoritmo canônico abaixo.

## Canonicalização normativa

### `approval_request_hash`

O hash vincula a aprovação à operação exata (tool, parâmetros, tarefa, plano e
escopo). O algoritmo é idêntico em Python (`approval_request_hash`) e Rust
(`approval_hash` nos testes de contrato):

1. Serializar `params` em **JSON Canônico RFC 8785 (JCS)**, gerando
   `params_json` (bytes UTF-8). O JCS ordena chaves, não escapa não-ASCII e
   normaliza números conforme ECMAScript/I-JSON.
2. Montar o objeto:

   ```json
   {
     "tool_id": "...",
     "params": "<params_json como string>",
     "task_id": "...",
     "plan_id": "...",
     "execution_mode": "HOST",
     "access_level": "WRITE",
     "fs_scope": "...",
     "net_scope": "...",
     "resource_limits": "...",
     "policy_version": "..."
   }
   ```

3. Serializar esse objeto em JCS.
4. Aplicar SHA-256 sobre os bytes UTF-8 e codificar em hexadecimal minúsculo.

Vetores normativos em `fixtures/vectors/`:

```
approval_hash.json          d1b1f67006b59323c2726227a35c232e482caada812d8e9656cbde75ca4369c6
approval_hash_unicode.json  05b4ec866868fd06699f25488b5e772c787a08236fb850ea1d4909a992e8d300
approval_hash_numbers.json  4a99c0a0f24bb9c43314d74ee6e9ff61555cfc3424c2b9736d1d6b0c31713125
```

Os vetores fixam três pontos que já divergiram entre as linguagens: ASCII,
não-ASCII e números em notação científica. O contrato v1.0.0 exige **I-JSON**:
inteiros no intervalo seguro de IEEE 754 (±(2^53−1)), sem `NaN`/`Infinity`.
Aprovações antigas com parâmetros fora desse domínio, ou cuja representação
numérica mude sob JCS, precisam ser reemitidas (fail-closed). Aprovações com
strings/booleanos/inteiros seguros mantêm o hash.

### `event.dedup_key`

Derivado apenas quando `dedup == true` e o payload está no domínio I-JSON. A
definição é compartilhada por `nexus_core/events.py` e
`events::types::Event::dedup_key` no Rust:

1. Serializar `payload` em **JSON Canônico RFC 8785 (JCS)**.
2. Montar `kind`, `source`, `severity` e o payload canônico unidos pelo
   separador de unidade ASCII `\x1f` (U+001F), nesta ordem.
3. Aplicar SHA-256 sobre os bytes UTF-8 e codificar em hexadecimal minúsculo.

Vetores normativos em `fixtures/vectors/`:

```
event_dedup.json         25da862c41539bc409492b532e0c543320b9e5410daa6109103146395d76a1c0
event_dedup_numbers.json 0c01448829e3aa8108e4f4d5f547ab26bd194d7e5fec5be1210012ae33eb7d25
```

O `canonical_payload()` do Python continua sendo uma projeção de persistência
compatível com dados antigos; ele não é o material do `dedup_key`.

## Compatibilidade

- **Aditivo sem quebra:** somente em `1.x` se o campo for opcional e ambos os
  lados ignorarem sua ausência. Como campos desconhecidos são rejeitados, um
  campo novo normalmente exige `1.<n+1>` e atualização dos dois lados.
- **Remoção, renomeação ou mudança de tipo/semântica:** exige `2.0.0` e novo
  namespace `urn:nexus:contracts:v2:*`.
- **Mudança de canonicalização de hash:** é quebra de contrato mesmo que o
  schema não mude; exige nova versão e migração explícita de aprovações. O
  contrato v1.0.0 usa RFC 8785 (JCS) e domínio I-JSON.
- **Toda alteração** deve atualizar `manifest.json`, os fixtures afetados e os
  testes Python e Rust no mesmo commit.

## Validação

Python (a partir da raiz do repositório):

```bash
.venv/bin/python -m pytest tests/test_contracts.py -q
```

Rust:

```bash
cd core/nexus-daemon
cargo test contracts
```

Os dois lados leem os **mesmos** arquivos deste diretório. O teste Rust localiza
a raiz via `CARGO_MANIFEST_DIR/../../contracts`; o Python via
`nexus_core/contracts.py::CONTRACTS_ROOT`.

Para validar uma instância manualmente:

```python
from nexus_core.contracts import validate_contract
validate_contract("event", payload)
```

## Escopo atual e próximo passo

Os tipos Rust de contrato são **test-only** (`#[cfg(test)] mod contracts;`) até
o T4. O objetivo é garantir que o wire congelado em v1.0.0 seja implementado
pelo IPC Rust sem divergência. A autoridade de política, aprovação e gate
continua em Python até T4–T6; a migração deve preservar estes contratos e os
vetores de hash.
