"""query_documents: семантический запрос к известным документам локальной базы.

Публичный метод второго контура TerraLogicX: первый контур передаёт
идентификаторы документов (version_id или DocumentRef) и высокоуровневый
запрос («Верни зоны ВРИ из этого документа»), второй контур сам решает,
как открыть документы и извлечь сведения. LLM — control plane, чтение
выполняет детерминированный инструмент read_document (MCP).

Протокол ответа агента: последняя строка stdout `=== RESULT === {json}`;
один retry при невалидном JSON/схеме, затем status=failed. Правило
промпта — extract, don't infer: каждое поле data подтверждается цитатой
в evidence. Каждый терминальный исход (success … failed) фиксируется
в таблице query_log базы — аудит запросов; сбой записи аудита не роняет
ответ, а добавляет warning.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..models import DocumentRef
from ..store import DocumentStore
from .ask import _ask_executor
from .config import AgentTierConfig

RESULT_MARKER = "=== RESULT ==="
_UNKNOWN_VERSION_DATE = "unknown"  # как в store.py: редакция без даты

_TERMINAL_STATUSES = (
    "success",
    "partial",
    "not_found",
    "ambiguous",
    "insufficient_evidence",
)


class QueryStatus(str, Enum):
    """Итог семантического запроса к документам."""

    SUCCESS = "success"
    PARTIAL = "partial"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    FAILED = "failed"


class Evidence(BaseModel):
    """Provenance извлечённого факта: документ → файл/страница → цитата."""

    version_id: int | None = None
    file: str | None = None
    page: int | None = None
    section: str | None = None
    quote: str | None = None


class ResolvedDocument(BaseModel):
    """Документ, резолвнутый в конкретную версию локальной базы."""

    version_id: int
    municipality: str
    doc_type: str
    number: str
    version_date: str
    title: str | None = None


class QueryResult(BaseModel):
    """Структурированный ответ второго контура первому."""

    status: QueryStatus
    data: Any = None
    evidence: list[Evidence] = Field(default_factory=list)
    answer_text: str | None = None
    documents: list[ResolvedDocument] = Field(default_factory=list)
    executor: str | None = None
    warnings: list[str] = Field(default_factory=list)
    duration_seconds: float = 0.0


QUERY_PROMPT_TEMPLATE = """\
Ты — аналитик локальной базы муниципальных правовых документов geodocs (ПЗЗ, генпланы, ЗОУИТ Московской области). Отвечаешь на запрос ТОЛЬКО по перечисленным документам базы.
ЗАПРОС: {query}
ДОКУМЕНТЫ (обращаться по version_id):
{cards}
ПОРЯДОК ДЕЙСТВИЙ:
1) Для каждого релевантного документа вызови read_document(version_id, query=<ключевые слова запроса: код зоны, вид использования, норма, дата>) — инструмент сам проверит готовые структурированные фрагменты (таблицы ВРИ) и найдёт места в полном тексте. При необходимости повтори с уточнённым query или вызови read_document(version_id) без query — карточка документа и начало текста.
2) Извлеки запрошенные сведения. ЖЁСТКОЕ ПРАВИЛО: EXTRACT, DON'T INFER — каждое поле ответа подтверждай точной цитатой из документа (evidence.quote); ничего не додумывай и не дополняй из собственных знаний. Сведений нет — status="not_found"; документы противоречат друг другу — status="ambiguous".
{schema_block}
ФИНАЛ — последней строкой строго:
=== RESULT === {{"status": "success|partial|not_found|ambiguous|insufficient_evidence", "data": <JSON-объект или null>, "evidence": [{{"version_id": N, "file": "название файла", "page": N или null, "section": "раздел или null", "quote": "точная цитата из документа"}}], "answer_text": "краткий ответ на русском"}}
Статусы: success — всё найдено; partial — найдено частично; not_found — сведений нет в документах; ambiguous — противоречия между документами; insufficient_evidence — ответ без цитат-подтверждений.
"""


def build_query_prompt(
    documents: list[ResolvedDocument],
    query: str,
    response_schema: dict[str, Any] | None = None,
) -> str:
    """Промт семантического запроса: карточки документов + схема результата."""
    cards = "\n".join(
        f"- version_id={doc.version_id} | {doc.municipality} | {doc.doc_type}"
        f" № {doc.number} от {doc.version_date} | {doc.title or 'без названия'}"
        for doc in documents
    )
    if response_schema is not None:
        schema_json = json.dumps(response_schema, ensure_ascii=False, indent=2)
        schema_block = (
            "ФОРМАТ data: JSON-объект, обязательно валидный по схеме:\n"
            f"```json\n{schema_json}\n```"
        )
    else:
        schema_block = "ФОРМАТ data: произвольный JSON-объект со сведениями по запросу."
    return QUERY_PROMPT_TEMPLATE.format(
        query=query, cards=cards, schema_block=schema_block
    )


def parse_result(stdout: str) -> dict[str, Any] | None:
    """Извлекает JSON-результат из stdout агента (как parse_manifest).

    Ищет последнюю строку, начинающуюся с `=== RESULT ===`: JSON либо в той
    же строке после маркера, либо на следующих строках до конца вывода.
    """
    lines = stdout.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        stripped = lines[index].lstrip()
        if not stripped.startswith(RESULT_MARKER):
            continue
        candidates = (
            stripped[len(RESULT_MARKER) :].strip(),
            "\n".join(lines[index + 1 :]).strip(),
        )
        for candidate in candidates:
            if not candidate:
                continue
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            return data if isinstance(data, dict) else None
        return None
    return None


def _resolve_home(home: str | Path | None) -> Path:
    if home is None:
        return Path(os.environ.get("GEODOCS_HOME", str(Path.home() / ".geodocs")))
    return Path(home)


def _persist_query(home_path: Path, query: str, result: QueryResult) -> None:
    """Аудит запроса в query_log; сбой записи — warning, не исключение."""
    try:
        store = DocumentStore(
            home_path / "geodocs.sqlite3", files_dir=home_path / "files"
        )
        try:
            store.log_query(
                query=query,
                status=result.status.value,
                version_ids=[doc.version_id for doc in result.documents],
                data=result.data,
                evidence=[item.model_dump(mode="json") for item in result.evidence],
                answer_text=result.answer_text,
                executor=result.executor,
                duration_seconds=result.duration_seconds,
                warnings=result.warnings,
            )
        finally:
            store.close()
    except Exception as exc:  # noqa: BLE001 — аудит не должен ронять ответ
        result.warnings.append(f"query_log не записан: {exc}")


def _version_row(store: DocumentStore, version_id: int) -> dict[str, Any] | None:
    row = store.connection.execute(
        "SELECT v.id, d.municipality, d.doc_type, d.number, v.version_date,"
        " COALESCE(v.title, d.title) AS title"
        " FROM document_versions v JOIN documents d ON d.id = v.document_id"
        " WHERE v.id = ?",
        (version_id,),
    ).fetchone()
    return dict(zip(row.keys(), row, strict=True)) if row is not None else None


def _resolve_documents(
    store: DocumentStore, documents: list[int | DocumentRef] | tuple[int | DocumentRef, ...]
) -> tuple[list[ResolvedDocument], list[str]]:
    """int = version_id напрямую; DocumentRef — по version_id или реквизитам."""
    resolved: list[ResolvedDocument] = []
    warnings: list[str] = []
    seen: set[int] = set()
    for document in documents:
        row: dict[str, Any] | None = None
        if isinstance(document, int):
            row = _version_row(store, document)
            if row is None:
                warnings.append(f"version_id {document} не найден в базе")
        elif isinstance(document, DocumentRef):
            if document.version_id is not None:
                row = _version_row(store, document.version_id)
                if row is None:
                    warnings.append(
                        f"version_id {document.version_id} из DocumentRef не найден"
                    )
            else:
                record = store.find_version(
                    municipality=document.municipality,
                    doc_type=document.doc_type,
                    number=document.number,
                    version_date=document.version_date or _UNKNOWN_VERSION_DATE,
                )
                if record is None:
                    warnings.append(
                        f"документ {document.municipality} {document.doc_type.value}"
                        f" № {document.number} от {document.version_date or '?'}"
                        " не найден в базе"
                    )
                else:
                    row = _version_row(store, record.id)
        else:
            warnings.append(f"неподдерживаемый документ: {type(document).__name__}")
        if row is not None and row["id"] not in seen:
            seen.add(row["id"])
            resolved.append(
                ResolvedDocument(
                    version_id=row["id"],
                    municipality=row["municipality"],
                    doc_type=row["doc_type"],
                    number=row["number"],
                    version_date=row["version_date"],
                    title=row["title"],
                )
            )
    return resolved, warnings


def _validate_data(data: Any, schema: dict[str, Any]) -> str | None:
    """Валидация data по JSON Schema; None = ок. Без jsonschema — без проверки."""
    try:
        import jsonschema
    except ImportError:
        return None
    try:
        jsonschema.validate(instance=data, schema=schema)
    except jsonschema.ValidationError as exc:
        return exc.message
    return None


def _check_payload(
    payload: dict[str, Any], response_schema: dict[str, Any] | None
) -> str | None:
    """Проверка RESULT-протокола; None = валиден, иначе текст ошибки."""
    status = payload.get("status")
    if status not in _TERMINAL_STATUSES:
        return f"неизвестный status: {status!r} (нужен один из {_TERMINAL_STATUSES})"
    data = payload.get("data")
    if status in ("success", "partial") and data is None:
        return "data обязателен при status=success/partial"
    if response_schema is not None and data is not None:
        return _validate_data(data, response_schema)
    return None


def _payload_to_result(
    payload: dict[str, Any],
    documents: list[ResolvedDocument],
    executor: str,
    warnings: list[str],
    duration: float,
) -> QueryResult:
    evidence: list[Evidence] = []
    for item in payload.get("evidence") or []:
        if not isinstance(item, dict):
            warnings.append("пропущен невалидный evidence-элемент (не объект)")
            continue
        try:
            evidence.append(
                Evidence(
                    **{
                        key: value
                        for key, value in item.items()
                        if key in Evidence.model_fields
                    }
                )
            )
        except ValueError:
            warnings.append(f"пропущен невалидный evidence-элемент: {item!r:.200}")
    answer = payload.get("answer_text")
    return QueryResult(
        status=QueryStatus(payload["status"]),
        data=payload.get("data"),
        evidence=evidence,
        answer_text=answer if isinstance(answer, str) else None,
        documents=documents,
        executor=executor,
        warnings=warnings,
        duration_seconds=duration,
    )


def query_documents(
    documents: list[int | DocumentRef] | tuple[int | DocumentRef, ...],
    query: str,
    response_schema: dict[str, Any] | None = None,
    evidence_required: bool = True,
    *,
    home: str | Path | None = None,
    executor_name: str = "hermes",
    config: AgentTierConfig | None = None,
    workdir: Path | None = None,
) -> QueryResult:
    """Семантический запрос к документам локальной базы через LLM-агента.

    documents — version_id (int) и/или DocumentRef (по version_id либо
    реквизитам municipality/doc_type/number/version_date). response_schema —
    JSON Schema словарём: data валидируется по ней (один retry при ошибке).
    evidence_required: success/partial без цитат понижается до
    insufficient_evidence механически.
    """
    started = time.monotonic()
    home_path = _resolve_home(home)
    store = DocumentStore(
        home_path / "geodocs.sqlite3", files_dir=home_path / "files"
    )
    try:
        resolved, warnings = _resolve_documents(store, documents)
    finally:
        store.close()
    if not resolved:
        result = QueryResult(
            status=QueryStatus.NOT_FOUND,
            warnings=warnings or ["ни один документ не резолвится в базе"],
            duration_seconds=time.monotonic() - started,
        )
        _persist_query(home_path, query, result)
        return result

    executor = _ask_executor(home_path, executor_name, config)
    prompt = build_query_prompt(resolved, query, response_schema)

    def _run(current_prompt: str) -> Any:
        if workdir is not None:
            return executor.run(current_prompt, workdir)
        with tempfile.TemporaryDirectory(prefix="geodocs-query-") as tmp:
            return executor.run(current_prompt, Path(tmp))

    exec_result = _run(prompt)
    error: str | None = None
    payload: dict[str, Any] | None = None
    if exec_result.returncode != 0:
        error = f"{executor.name} завершился с кодом {exec_result.returncode}"
    else:
        payload = parse_result(exec_result.stdout)
        if payload is None:
            error = "RESULT не найден в ответе агента или невалиден"
        else:
            error = _check_payload(payload, response_schema)

    if error is not None and exec_result.returncode == 0:
        # один retry с текстом ошибки протокола
        retry_prompt = (
            f"{prompt}\n\nПРЕДЫДУЩИЙ ОТВЕТ ОТКЛОНЁН: {error}\n"
            "Верни исправленный RESULT: валидный JSON после маркера"
            " === RESULT === с полями status/data/evidence/answer_text."
        )
        retry = _run(retry_prompt)
        if retry.returncode == 0:
            retry_payload = parse_result(retry.stdout)
            if retry_payload is None:
                error = "RESULT не найден в ответе агента или невалиден"
            else:
                retry_error = _check_payload(retry_payload, response_schema)
                if retry_error is None:
                    payload, exec_result, error = retry_payload, retry, None
                else:
                    error = retry_error

    duration = time.monotonic() - started
    if error is not None or payload is None:
        result = QueryResult(
            status=QueryStatus.FAILED,
            documents=resolved,
            executor=executor.name,
            warnings=[*warnings, error or "агент не вернул результат"],
            duration_seconds=duration,
        )
        _persist_query(home_path, query, result)
        return result

    result = _payload_to_result(payload, resolved, executor.name, warnings, duration)
    if (
        evidence_required
        and result.status in (QueryStatus.SUCCESS, QueryStatus.PARTIAL)
        and not result.evidence
    ):
        result.status = QueryStatus.INSUFFICIENT_EVIDENCE
        result.warnings.append(
            "evidence_required: success/partial без цитат понижен до"
            " insufficient_evidence"
        )
    _persist_query(home_path, query, result)
    return result
