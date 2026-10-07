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

## Публичный API второго контура

Два метода скрывают от первого контура (terralogic-engine, pyrgis/pynspd)
всю механику поиска, скачивания, кэширования, чтения и извлечения:

```python
from geodocs import acquire_documents, query_documents

# 1) обеспечить наличие документа в локальном хранилище (cache-first)
acquired = acquire_documents(
    "Городской округ Коломна", "pzz", number="1198", version_date="2026-04-17",
)
# status: cached (уже в базе, агент не запускался) | acquired | not_found | failed
version_id = acquired.refs[0].version_id

# 2) семантический запрос к известным документам
result = query_documents(
    documents=[version_id],           # int = version_id; также DocumentRef
    query="Верни ВРИ территориальной зоны СХ-2",
    response_schema={                  # опционально: JSON Schema словарём
        "type": "object",
        "properties": {
            "zone_code": {"type": "string"},
            "permitted_uses": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["zone_code", "permitted_uses"],
    },
)
# result.status: success | partial | not_found | ambiguous
#              | insufficient_evidence | failed
# result.data — валидирован по response_schema; result.evidence — цитаты
# (version_id, file, page, section, quote) для каждого факта.
```

Инварианты, зашитые в код (не зависят от решения LLM): cache-first в
`acquire_documents`; в `query_documents` — протокол `=== RESULT === {json}`
с одним retry при невалидном JSON/схеме, правило «extract, don't infer»
(каждое поле `data` подтверждено цитатой в `evidence`) и механическое
понижение success/partial без цитат до `insufficient_evidence`.

Аудит зашит тоже: после неуспешной агентной попытки `acquire_documents`
переводит версию из `pending` в `not_found`/`search_failed` (честный исход
вместо «не пытались»; batch-режим по умолчанию подбирает оба статуса на
повтор), а `query_documents` пишет каждый терминальный исход в таблицу
`query_log` (запрос, статус, version_ids, data, evidence, исполнитель,
длительность; сбой записи аудита не роняет ответ — добавляет warning).

Первый контур не знает: где лежит файл, PDF это или DOCX, какой parser
используется, есть ли готовые extractions, какая LLM и какие tools
вызываются внутри. `ask_document` (см. ниже) остаётся тонким сахаром для
discovery-Q&A без перечня документов.

## Агентный ярус

Документы, которые статика не добрала (`not_found`, `pending`), добывают
внешние LLM-агенты по одному на задачу: `geodocs-agent list|run|recover`.
Исполнители сменные, порядок failover и квотные паттерны — в
`$GEODOCS_HOME/agents.yaml` (env-override пути: `GEODOCS_AGENTS_CONFIG`;
файл отсутствует — встроенный дефолт: один исполнитель `hermes`):

```yaml
defaults:
  workers: 1               # 1 = последовательно; >1 — ThreadPoolExecutor
  retry_attempts: 2        # полных проходов цепочки по задаче
  retry_pause_seconds: 60  # пауза между проходами
run_after_sync: false      # читает внешний sync-harness (см. run_agent_tier)
chain: [hermes]
executors:
  hermes:
    type: hermes           # type определяет адаптер (см. рецепт ниже)
    command: hermes
    args: ["-z"]
    timeout_seconds: 2400
    quota_patterns:        # regex по stdout+stderr неуспешного запуска
      - "(?i)quota"
      - "429"
    skills_dirs: []        # доп. каталоги скилов (пакетные подключены всегда)
    mcp:                   # MCP-инструменты порталов (по умолчанию включены)
      enabled: true
```

Семантика прогона: проход = перебор исполнителей в порядке `chain`, успех =
гейт принял inbox. Исполнитель, исчерпавший квоту, вычёркивается на весь
текущий запуск. Финальная неудача эскалирует в статус `manual_required`
(result-JSON в `<GEODOCS_HOME>/agent/results/`), задача остаётся видна в
`recover`. Провайдер реально успевшего исполнителя фиксируется в БД
(`hermes-agent`; recover-приём — `manual`; `kimi-agent` — provenance
архивных записей прежнего backend).

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
chain: [hermes, myagent]
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

### Hermes harness: статические инструменты вместо свободного веб-поиска

HermesExecutor на каждую задачу материализует изолированный HERMES_HOME
(`<GEODOCS_HOME>/.hermes-home/<slug>`): `config.yaml` — копия конфига
основного профиля (`$HERMES_HOME/config.yaml`, по умолчанию
`~/.hermes/config.yaml`) с пропатченным `mcp_servers.geodocs` (stdio
MCP-сервер `geodocs-agent-mcp`, `python -m geodocs.agent.mcp`, зависимость
extra: `pip install 'geodocs[mcp]'`); `auth.json`/`.env` — симлинки на
основной профиль (креды провайдера и `FIRECRAWL_API_KEY`); `skills/` —
копии пакетных скилов. Запуск — `hermes -z <промт>` с `--accept-hooks` и
`--skills`. Агент работает по промпту строго в порядке: локальная база →
инструменты порталов → скачивание → свободный веб-поиск штатными
веб-инструментами Hermes (Firecrawl, последнее звено) → MANIFEST.

Агенту видны ровно три инструмента — вся механика зашита внутри них, модель
принимает только семантические решения:

| Инструмент | Что делает |
|---|---|
| `find_document` | Cache-first discovery: локальная база (версия `downloaded` сразу готова к чтению), при промахе — поиск по всем порталам реестра, merged-кандидаты с тегами |
| `import_document` | Внешний URL (файл или HTML-страница со ссылками) → локальный документ: скачивание, гейт, регистрация в базе, возврат `version_id` |
| `read_document` | Чтение локального документа: карточка, файлы, готовые extractions (приоритет) и релевантные фрагменты текста с номерами страниц |

Низкоуровневые блоки (`search_document`, `download_document`, `fetch_page`,
`read_document_text` и др.) остаются в `geodocs.agent.mcp` как внутренние
строительные кирпичи и точки тестирования — в MCP-сервере они не
регистрируются.

### Q&A по локальной базе: `ask_document`

Единый метод извлечения сведений из скачанных документов — свободный
текстовый запрос, ответ агента, основанный только на базе:

```bash
GEODOCS_HOME=$HOME/.geodocs uv run geodocs-agent ask \
    "Верни ВРИ для зоны СХ-2 городского округа Коломна"
```

```python
from geodocs.agent import ask_document

result = ask_document("Верни ВРИ для документа № 1198 Коломна")
print(result.answer)   # ответ + источник (муниципалитет, №, дата, файл)
```

Агент работает инструментами `find_document` (выбор версии по реквизитам)
и `read_document` (готовые extractions раньше полного текста).

Порталы-доноры (`src/geodocs/agent/portals/`, регистрация —
`register_portal`): **meganorm** (нормо-база; fetch отказывается сохранять
HTML-карточки — ловушка прошлых прогонов), **cntd** (полный текст блоками
с docs.cntd.ru), **fgistp** (карточки ФГИС ТП по URL из веб-поиска;
эвристика — доступ к материалам с сентября 2026 ограничен,
[разбор](https://geo-risk.ru/blog/fgis-tp-zakryli-dostup-chto-delat)),
**pravo** (publication.pravo.gov.ru), **mosreg** (data.mosreg.ru),
**municipal** (универсальный читатель страниц, без поиска). Парсеры
поисковой выдачи meganorm/pravo/mosreg — эвристики, требуют проверки на
пилоте; HTTP-адаптеры покрыты тестами с замоканной выдачей.

Новый портал/скил — без правок ядра: адаптер по протоколу `PortalAdapter`
(`name`, `async search`, `async fetch`) → `register_portal("имя", ...)`
(доступен в `search_document` автоматически); скил — каталог с `SKILL.md`
(frontmatter: name, description, whenToUse) в `--skills-dir`-каталоге
или в `skills_dirs` агента. Скилы пакета: meganorm-search, cntd-search,
municipal-navigation, document-requisites.

Порядок fallback целиком: статика (rgis-карточки) → агентные
инструменты/порталы → свободный веб-поиск агентом → ручной добор
(`manual_required`, виден в `recover`).

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
