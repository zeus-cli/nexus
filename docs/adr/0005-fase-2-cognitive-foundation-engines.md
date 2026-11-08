# ADR 0005 — Cognitive Foundation: motores cognitivos discretos (Fase 2)

- **Status:** aceito
- **Data:** 2026-11-07
- **Escopo:** `nexus_core/intent_engine.py`, `context_engine.py`, `reasoning_engine.py`, `decision_engine.py`, `planner_engine.py`, `knowledge_engine.py` + integração opcional em `cognitive.py`
- **Depende de:** ADR 0001–0004

---

## Contexto

A Fase 1 entregou o `nexus-daemon` (Control Plane em Rust) e preservou o backend Python existente. A Cognitive Core atual (`cognitive.py`) é um loop adaptativo monolítico que combina planejamento, execução, reflexão e síntese em um único fluxo baseado em LLM.

A especificação mestre (seção 7) exige motores cognitivos discretos e substituíveis:

* **Intent Engine** — interpretação e classificação de solicitações (objetivo, intenção, restrições, resultado esperado, informação necessária, riscos, capacidades).
* **Context Engine** — construção de representações contextuais enxutas, sem carregar toda a memória em cada requisição (retrieval contextual + ranqueamento de relevância).
* **Reasoning Engine** — raciocínio estruturado (decomposição, evidências, hipóteses, alternativas, incerteza, contradições) sem expor CoT privado.
* **Decision Engine** — comparação de decisões candidatas (resultado esperado, qualidade da evidência, risco, recursos, reversibilidade, restrições, confiança, aprovação humana).
* **Planner Engine** — conversão de objetivos em planos executáveis (ID, etapas, dependências, executor, ferramentas, recursos, risco, pré-condições, critérios de sucesso, tratamento de falha, estado).
* **Knowledge Engine** — conhecimento com proveniência, distinguindo fato / suposição / hipótese / predição / simulação; nunca promove simulação a fato automaticamente.

---

## Decisão 1 — Motores como módulos Python individuais em `nexus_core/`

Criado seis módulos individuais (não subpacotes vazios) dentro do pacote existente `nexus_core`:

* `intent_engine.py`
* `context_engine.py`
* `reasoning_engine.py`
* `decision_engine.py`
* `planner_engine.py`
* `knowledge_engine.py`

Motivos: consistência com a estrutura atual do `nexus_core/` (arquivos diretos como `cognitive.py`, `reflection.py`, `automation.py`); evita criar diretórios vazios; cada módulo tem interface, responsabilidade e testes próprios.

---

## Decisão 2 — Integração opcional no `Cognitive Core` (não destrutiva)

O construtor de `CognitiveCore` ganhou parâmetros opcionais (`intent_engine`, `context_engine`, `knowledge_engine`). Quando fornecidos, são usados para enriquecer o fluxo (classificação de intenção, contexto enxuto, registro de conhecimento). Quando ausentes, o comportamento existente é preservado integralmente.

Isso garante regressão zero: todos os 173 testes existentes continuam passando.

---

## Decisão 3 — Intent Engine com classificação heurística determinística

Classifica por palavras-chave (execução, simulação, planejamento, análise, memória, consulta) com pontuação e confiança. Não exige LLM para classificação simples. O LLM é usado apenas quando a intenção não puder ser resolvida por regras (futuro).

---

## Decisão 4 — Context Engine com ranqueamento de relevância

Busca memória (FTS5 + vetorial quando disponível), eventos recentes, estado do sistema e tarefas ativas. Ranqueia blocos por sobreposição de palavras-chave com o objetivo da intenção. Trunca blocos e total para não exceder a janela de contexto (máximo 8 blocos, 2400 chars totais por padrão).

---

## Decisão 5 — Reasoning Engine sem expor CoT privado

Produz saídas estruturadas (conclusão, etapas, confiança, suposições, evidências, contradições, alternativas). O LLM, quando disponível, retorna apenas a conclusão final em JSON — nunca expõe o chain-of-thought privado. Armazena conclusões, evidências e suposições, não CoT bruto.

---

## Decisão 6 — Decision Engine com pontuação ponderada

Compara decisões candidatas usando pesos: qualidade da evidência (20%), risco operacional (20%), reversibilidade (15%), recursos (10%), confiança (15%), resultado esperado (10%), restrições (10%). Define requisito de aprovação (NONE / POLICY / HUMAN / DENIED) com base no nível de risco.

---

## Decisão 7 — Knowledge Engine com proveniência e não-promoção de simulação

Cada registro tem `kind` (FACT/ASSUMPTION/HYPOTHESIS/PREDICTION/SIMULATION) e `source` (USER/OBSERVATION/EXTERNAL/MODEL_INFERENCE/SIMULATION_OUTPUT/VALIDATED_EXPERIENCE). `validate_fact()` promove a fato apenas de ASSUMPTION/HYPOTHESIS; registros SIMULATION nunca são promovidos a fato automaticamente.

---

## Consequências

* **Positivo:** seis motores cognitivos discretos, testáveis e substituíveis; integração não destrutiva no Cognitive Core; regressão zero (173 testes passando).
* **Negativo (Fase 2):** a integração com o PlannerEngine e Decision Engine no fluxo principal do cognitive.py fica para a Fase 2 completa, quando as interfaces dos motores forem consumidas pelo loop adaptativo de forma mais profunda.
* **Risco conhecido:** o `KnowledgeEngine` e `PlannerEngine` mantêm estado em memória (dict). Persistência em SQLite será adicionada na Fase 2/3 quando esses motores forem consumidos pelo loop principal.

---

## Testes

* `tests/test_cognitive_engines.py`: 16 testes cobrindo todos os seis motores.
* Regressão completa: 173 testes passando.
