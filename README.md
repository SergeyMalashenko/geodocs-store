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
