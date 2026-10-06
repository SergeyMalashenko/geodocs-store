# geodocs

Общее хранилище градостроительных документов (ПЗЗ, генпланы, ГПЗУ, режимы
ЗОУИТ) для `pyrgis-agents` и `pynspd-agents`. Документ муниципалитета
скачивается и парсится один раз и обслуживает все кадастровые номера округа.

Хранилище — SQLite в WAL-режиме (`DocumentStore`), файлы — в `<home>/files`
с дедупликацией по sha256. Идентичность документа:
`(municipality, doc_type, number)` + `version_date` редакции; кадастровые
номера лишь ссылаются на версии (многие-ко-многим), происхождение
фиксируется в `document_sources`.

```python
from geodocs import DocumentRef, DocumentStore, DocType, SourceName

with DocumentStore.from_env() as store:  # каталог из GEODOCS_HOME
    version_id = store.register_ref(DocumentRef(
        municipality="Городской округ Солнечногорск",
        doc_type=DocType.PZZ, number="592", version_date="2026-04-09",
        source=SourceName.RGIS, source_object_id="13881025700",
    ))
    store.link_parcel("50:09:0000000:198995", version_id)
```

```bash
uv run pytest -q
uv run ruff check src tests
```

## Агентный ярус

Документы, которые статика не добрала (`not_found`, `pending`), добывают
внешние LLM-агенты по одному на задачу: `geodocs-agent list|run|recover`.
Исполнители сменные, порядок failover и квотные паттерны — в
`$GEODOCS_HOME/agents.yaml` (env-override пути: `GEODOCS_AGENTS_CONFIG`;
файл отсутствует — встроенный дефолт: один исполнитель `kimi`):

```yaml
defaults:
  workers: 1               # 1 = последовательно; >1 — ThreadPoolExecutor
  retry_attempts: 2        # полных проходов цепочки по задаче
  retry_pause_seconds: 60  # пауза между проходами
run_after_sync: false      # читает внешний sync-harness (см. run_agent_tier)
chain: [kimi]
executors:
  kimi:
    type: kimi             # type определяет адаптер (см. рецепт ниже)
    command: kimi
    args: ["-p"]
    timeout_seconds: 900
    quota_patterns:        # regex по stdout+stderr неуспешного запуска
      - "(?i)quota"
      - "429"
```

Семантика прогона: проход = перебор исполнителей в порядке `chain`, успех =
гейт принял inbox. Исполнитель, исчерпавший квоту, вычёркивается на весь
текущий запуск. Финальная неудача эскалирует в статус `manual_required`
(result-JSON в `<GEODOCS_HOME>/agent/results/`), задача остаётся видна в
`recover`. Провайдер реально успевшего исполнителя фиксируется в БД
(`kimi-agent`; recover-приём — `manual`).

```bash
GEODOCS_HOME=$HOME/.geodocs uv run geodocs-agent run [--chain <имена из agents.yaml>] \
    [--workers N] [--dry-run] [--limit N] [--status not_found,pending]
GEODOCS_HOME=$HOME/.geodocs uv run geodocs-agent stats [--json]
```

### Новый исполнитель — без правок ядра

Адаптер — класс с интерфейсом `Executor` (~20 строк при наследовании
базового subprocess-адаптера). Регистрация делает его доступным в
agents.yaml; цепочка, failover, retry и квоты работают без изменений:

```python
from geodocs.agent.executors import SubprocessExecutor, register_executor_type

class MyAgentExecutor(SubprocessExecutor):
    provider = SourceName.MANUAL  # или свой SourceName для provenance в БД

register_executor_type("myagent", MyAgentExecutor)
```

```yaml
chain: [kimi, myagent]
executors:
  myagent:
    type: myagent
    command: my-agent
    args: ["--prompt"]
    timeout_seconds: 900
    quota_patterns: ["(?i)quota"]
```

Если агент не CLI (сетевой API и т.п.), адаптер реализует протокол
`Executor` (`name`, `provider`, `run(prompt, workdir) -> ExecutionResult`)
с нуля и бросает `QuotaExceeded` при исчерпании квоты — цепочка сама
переключится на следующего исполнителя.

Статический sync (`geodocs sync`) живёт в pyrgis-agents (`sync_parcel_documents`:
RGIS discovery → `register_ref` → `link_parcel` → fetch). Отдельного реестра
документов нет: общая SQLite-база — единый реестр обоих ярусов. Версии,
добранные агентным ярусом (провайдеры `kimi-agent`/`manual`), сразу доступны
всем кадастровым номерам муниципалитета: sync читает
`store.unlinked_versions_for_municipality(municipality, cadastral_number)` и
привязывает подходящие версии к номеру через `link_parcel`. Повторный поиск
документа, уже имеющегося в базе, не требуется ни одним ярусом. После
синхронизации sync вызывает `geodocs.agent.run_agent_tier(home,
only_missing=True)` — самостоятельно или по флагу `run_after_sync` в
agents.yaml.
