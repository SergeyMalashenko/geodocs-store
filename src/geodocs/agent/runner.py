"""Запуск внешних агентов по задачам: failover-цепочка исполнителей и приём результата."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import threading
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel

from ..models import FileRecord, SourceName
from ..store import DocumentStore
from .config import AgentTierConfig, load_config
from .executors import (
    ExecutionTimeout,
    Executor,
    ExecutorError,
    QuotaExceeded,
    build_executor,
)
from .gate import DOCUMENT_SUFFIXES, FileVerdict, gate_pass, verify_files
from .prompt import build_prompt
from .tasks import AgentTask, list_pending_tasks

MANIFEST_MARKER = "=== MANIFEST ==="
_POLITE_DELAY_S = 5
_TAIL_LEN = 1000

TaskStatus = Literal[
    "downloaded",
    "not_found",
    "agent_error",
    "gate_failed",
    "manual_required",
]


class TaskResult(BaseModel):
    """Итог прогона одной задачи через цепочку внешних агентов."""

    task: AgentTask
    manifest: dict[str, Any] | None
    verdicts: list[FileVerdict]
    status: TaskStatus
    error: str | None
    duration_s: float
    stdout_tail: str | None = None
    stderr_tail: str | None = None
    executor: str | None = None
    attempts: int = 1
    quota_exhausted: list[str] = []


class _QuotaTracker:
    """Исполнители, исчерпавшие квоту в текущем запуске: общие для всех потоков."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._exhausted: set[str] = set()

    def mark(self, name: str) -> None:
        with self._lock:
            self._exhausted.add(name)

    def is_exhausted(self, name: str) -> bool:
        with self._lock:
            return name in self._exhausted

    def snapshot(self) -> list[str]:
        with self._lock:
            return sorted(self._exhausted)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tail(text: str | bytes | None) -> str | None:
    """Последние ~1000 символов вывода агента для диагностики."""
    if text is None:
        return None
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    return text[-_TAIL_LEN:]


def parse_manifest(stdout: str) -> dict[str, Any] | None:
    """Извлекает JSON-манифест из stdout агента.

    Ищет последнюю строку, начинающуюся с `=== MANIFEST ===`: JSON либо в той
    же строке после маркера, либо на следующих строках до конца вывода.
    Возвращает dict или None, если маркера нет или JSON битый.
    """
    lines = stdout.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        stripped = line.lstrip()
        if not stripped.startswith(MANIFEST_MARKER):
            continue
        candidates = (
            stripped[len(MANIFEST_MARKER) :].strip(),
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


def _target_dir(store: DocumentStore, task: AgentTask) -> Path:
    """Каталог файлов версии в хранилище, как у save_file()."""
    number = task.number.replace("/", "_").replace("-", "_")
    return (
        store.files_dir
        / task.municipality.replace(" ", "_")
        / f"{task.doc_type}_{number}"
    )


def _claimed_names(manifest: dict[str, Any]) -> list[str]:
    """Имена файлов из манифеста, очищенные от каталогов (анти-траверсал)."""
    raw = manifest.get("files") or []
    names: list[str] = []
    for item in raw:
        name = PurePosixPath(str(item).replace("\\", "/")).name
        if name in ("", ".", ".."):
            continue
        if name not in names:
            names.append(name)
    return names


def inbox_dir(home: Path, task: AgentTask) -> Path:
    """Каталог inbox задачи, куда агент кладёт скачанные файлы."""
    return home / "inbox" / task.slug


def _inbox_names(inbox: Path) -> list[str]:
    """Все файлы inbox (без подкаталогов) — улики при потерянном манифесте."""
    if not inbox.is_dir():
        return []
    return sorted(path.name for path in inbox.iterdir() if path.is_file())


def _inbox_document_names(inbox: Path) -> list[str]:
    """Файлы inbox, похожие на документы: без page_text.txt и прочего мусора."""
    return [
        name
        for name in _inbox_names(inbox)
        if Path(name).suffix.casefold() in DOCUMENT_SUFFIXES
    ]


def _collect_files(
    names: list[str],
    inbox: Path,
    store: DocumentStore,
    task: AgentTask,
) -> tuple[list[FileVerdict], list[Path]]:
    """Копирует заявленные файлы из inbox в хранилище и верифицирует их."""
    verdicts: list[FileVerdict] = []
    copied: list[Path] = []
    target_dir = _target_dir(store, task)
    for name in names:
        source = inbox / name
        if not source.is_file():
            verdicts.append(
                FileVerdict(
                    path=str(source),
                    size=0,
                    kind="missing",
                    ok=False,
                    reason="заявленный файл отсутствует в inbox",
                )
            )
            continue
        target_dir.mkdir(parents=True, exist_ok=True)
        destination = target_dir / name
        shutil.copy2(source, destination)
        copied.append(destination)
    verdicts.extend(verify_files(copied))
    return verdicts, copied


def _gate_and_register(
    verdicts: list[FileVerdict],
    copied: list[Path],
    store: DocumentStore,
    task: AgentTask,
    source_url: str | None,
    provider: SourceName,
) -> bool:
    """Общий шаг приёма: гейт по вердиктам и регистрация в БД. True = принято."""
    if not gate_pass(verdicts):
        return False
    store.record_agent_fetch(
        task.version_id,
        files=[_file_record(path) for path in copied],
        source_url=source_url,
        fetched_at=_utcnow(),
        source_provider=provider,
    )
    return True


def _failed_reasons(verdicts: list[FileVerdict]) -> str:
    failed = [f"{Path(v.path).name}: {v.reason}" for v in verdicts if not v.ok]
    return "; ".join(failed)


def _manifest_source_url(manifest: dict[str, Any]) -> str | None:
    url = manifest.get("source_url")
    return url if isinstance(url, str) and url else None


def _file_record(path: Path) -> FileRecord:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return FileRecord(
        path=str(path),
        size=path.stat().st_size,
        sha256=digest.hexdigest(),
        title=path.name,
    )


def _save_result(home: Path, slug: str, result: TaskResult) -> Path:
    out_dir = home / "agent" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{slug}.json"
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(result.model_dump(mode="json"), fh, ensure_ascii=False, indent=2)
    return out_path


def _attempt_with_executor(
    task: AgentTask,
    store: DocumentStore,
    *,
    executor: Executor,
    home: Path,
) -> TaskResult:
    """Одна попытка одного исполнителя: промт → запуск → гейт → запись в БД.

    QuotaExceeded пробрасывается наружу (обрабатывает цепочка). Успех =
    гейт принял inbox; версия переходит в downloaded (record_agent_fetch с
    provider реально отработавшего исполнителя). Без манифеста, но с валидным
    inbox результат дожимается по уликам из inbox — в том числе при таймауте
    (медленная локальная модель могла успеть скачать файлы до убийства).
    В recovery-режимах учитываются только файлы-документы (DOCUMENT_SUFFIXES):
    page_text.txt от fetch_page и прочий мусор гейт не отравляют.
    """
    inbox = inbox_dir(home, task)
    inbox.mkdir(parents=True, exist_ok=True)
    prompt = build_prompt(task, inbox)

    started = time.monotonic()
    manifest: dict[str, Any] | None = None
    verdicts: list[FileVerdict] = []
    status: TaskStatus = "agent_error"
    error: str | None = None
    stdout_tail: str | None = None
    stderr_tail: str | None = None

    try:
        exec_result = executor.run(prompt, home)
    except ExecutionTimeout as exc:
        error = str(exc)
        # агент убит по таймауту, но мог успеть скачать валидные файлы
        verdicts, copied = _collect_files(
            _inbox_document_names(inbox), inbox, store, task
        )
        if _gate_and_register(verdicts, copied, store, task, None, executor.provider):
            status = "downloaded"
            error += "; recovered from inbox evidence after timeout"
    except ExecutorError as exc:
        error = str(exc)
    else:
        stdout_tail = _tail(exec_result.stdout)
        stderr_tail = _tail(exec_result.stderr)
        if exec_result.returncode != 0:
            status = "agent_error"
            error = (
                f"{executor.name} завершился с кодом {exec_result.returncode}:"
                f" {exec_result.stderr.strip()[:400]}"
            )
        else:
            manifest = parse_manifest(exec_result.stdout)
            if manifest is None:
                # манифест потерян (типично: квота убила агента после
                # скачивания) — принимаем по уликам из inbox
                verdicts, copied = _collect_files(
                    _inbox_document_names(inbox), inbox, store, task
                )
                if _gate_and_register(
                    verdicts, copied, store, task, None, executor.provider
                ):
                    status = "downloaded"
                    error = "manifest missing; recovered from inbox evidence"
                else:
                    error = "агент не вернул валидный MANIFEST"
                    reasons = _failed_reasons(verdicts)
                    if reasons:
                        error += f"; inbox не прошёл гейт: {reasons}"
            elif manifest.get("status") == "not_found":
                status = "not_found"
            else:
                verdicts, copied = _collect_files(
                    _claimed_names(manifest), inbox, store, task
                )
                if _gate_and_register(
                    verdicts,
                    copied,
                    store,
                    task,
                    _manifest_source_url(manifest),
                    executor.provider,
                ):
                    status = "downloaded"
                else:
                    status = "gate_failed"
                    error = (
                        _failed_reasons(verdicts) or "агент не заявил ни одного файла"
                    )

    return TaskResult(
        task=task,
        manifest=manifest,
        verdicts=verdicts,
        status=status,
        error=error,
        duration_s=time.monotonic() - started,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        executor=executor.name,
    )


def _single_pass_config() -> AgentTierConfig:
    """Конфиг по умолчанию для run_task: один проход без пауз."""
    return AgentTierConfig(
        chain=[], executors={}, retry_attempts=1, retry_pause_seconds=0
    )


def _run_task_chained(
    task: AgentTask,
    store: DocumentStore,
    *,
    chain: Sequence[Executor],
    config: AgentTierConfig,
    tracker: _QuotaTracker,
    home: Path,
) -> TaskResult:
    """Проходы по цепочке исполнителей: failover, пауза, retry до исчерпания.

    Исполнитель, бросивший QuotaExceeded, вычёркивается трекером на весь
    текущий запуск (для остальных задач его уже не звать). Финал: not_found,
    если хотя бы один исполнители достоверно ответил «не найдено» и больше
    не было ошибок, иначе manual_required (задача видна в recover).
    """
    started = time.monotonic()
    notes: list[str] = []
    quota_seen: list[str] = []
    not_found_names: list[str] = []
    last_executor: str | None = None
    last_result: TaskResult | None = None
    passes_done = 0

    for attempt in range(1, config.retry_attempts + 1):
        available = [ex for ex in chain if not tracker.is_exhausted(ex.name)]
        if not available:
            notes.append("все исполнители цепочки исчерпали квоту")
            break
        passes_done = attempt
        for executor in available:
            last_executor = executor.name
            try:
                result = _attempt_with_executor(
                    task, store, executor=executor, home=home
                )
            except QuotaExceeded as exc:
                tracker.mark(executor.name)
                quota_seen.append(executor.name)
                notes.append(str(exc))
                continue
            last_result = result
            if result.status == "not_found":
                not_found_names.append(executor.name)
                continue
            if result.status == "downloaded":
                result.attempts = attempt
                result.quota_exhausted = quota_seen
                result.duration_s = time.monotonic() - started
                return result
            notes.append(f"{executor.name}: {result.error or result.status}")
        if attempt < config.retry_attempts:
            if not any(not tracker.is_exhausted(ex.name) for ex in chain):
                notes.append("все исполнители цепочки исчерпали квоту")
                break  # дальше некому дожимать: пауза не имеет смысла
            time.sleep(config.retry_pause_seconds)

    status: TaskStatus = (
        "not_found" if not_found_names and not notes else "manual_required"
    )
    if status == "not_found" and last_result is not None:
        # честный «не найдено»: возвращаем манифест последней попытки
        last_result.attempts = passes_done
        last_result.quota_exhausted = quota_seen
        last_result.duration_s = time.monotonic() - started
        return last_result
    error: str | None = None
    if status == "manual_required":
        error = "; ".join(notes) or "исполнители недоступны"
        if not_found_names:
            error += f"; not_found от: {', '.join(not_found_names)}"
    return TaskResult(
        task=task,
        manifest=None,
        verdicts=list(last_result.verdicts) if last_result is not None else [],
        status=status,
        error=error,
        duration_s=time.monotonic() - started,
        stdout_tail=last_result.stdout_tail if last_result is not None else None,
        stderr_tail=last_result.stderr_tail if last_result is not None else None,
        executor=last_executor,
        attempts=passes_done or config.retry_attempts,
        quota_exhausted=quota_seen,
    )


def run_task(
    task: AgentTask,
    store: DocumentStore,
    *,
    executors: Sequence[Executor],
    config: AgentTierConfig | None = None,
    geodocs_home: Path | None = None,
    quota_tracker: _QuotaTracker | None = None,
) -> TaskResult:
    """Запускает одну задачу через цепочку исполнителей и сохраняет result-JSON."""
    home = Path(geodocs_home) if geodocs_home is not None else store.db_path.parent
    result = _run_task_chained(
        task,
        store,
        chain=list(executors),
        config=config or _single_pass_config(),
        tracker=quota_tracker or _QuotaTracker(),
        home=home,
    )
    _save_result(home, task.slug, result)
    return result


def recover_inbox(
    task: AgentTask,
    store: DocumentStore,
    *,
    geodocs_home: Path | None = None,
) -> TaskResult:
    """Детерминированный приём готовых файлов из inbox без запуска агента.

    Для задач, чьи агенты скачали файлы, но не дожили до печати манифеста.
    source_url неизвестен — record_agent_fetch получает None, провайдер —
    manual (файлы принял оператор через recover).
    """
    home = Path(geodocs_home) if geodocs_home is not None else store.db_path.parent
    inbox = inbox_dir(home, task)
    started = time.monotonic()

    verdicts, copied = _collect_files(_inbox_document_names(inbox), inbox, store, task)
    if _gate_and_register(verdicts, copied, store, task, None, SourceName.MANUAL):
        status: TaskStatus = "downloaded"
        error: str | None = None
    else:
        status = "gate_failed"
        error = _failed_reasons(verdicts) or "inbox пуст"

    result = TaskResult(
        task=task,
        manifest=None,
        verdicts=verdicts,
        status=status,
        error=error,
        duration_s=time.monotonic() - started,
        executor="recover",
    )
    _save_result(home, task.slug, result)
    return result


def run_pending(
    store: DocumentStore,
    *,
    limit: int | None = None,
    statuses: Sequence[str] = ("not_found", "pending"),
    geodocs_home: Path | None = None,
    config: AgentTierConfig | None = None,
    executors: Mapping[str, Executor] | None = None,
) -> list[TaskResult]:
    """Прогон очереди задач через цепочку исполнителей.

    workers=1 — последовательно с вежливой паузой; workers>1 — потоки по
    задачам (каждый со своим соединением SQLite), result-JSON пишутся строго
    в порядке задач. Квота-исчерпания исполнителей общие на весь запуск.
    """
    home = Path(geodocs_home) if geodocs_home is not None else store.db_path.parent
    cfg = config or load_config(home)
    if executors is None:
        # home общей базы проводим в конфиг исполнителя: MCP-инструменты
        # (check_local_store) должны смотреть в неё, а не в рабочий каталог.
        executor_map = {
            name: build_executor(dataclasses.replace(entry, home=home))
            for name, entry in cfg.executors.items()
        }
    else:
        executor_map = dict(executors)
    chain = [executor_map[name] for name in cfg.chain if name in executor_map]
    if not chain:
        raise ValueError("ни один исполнитель из chain не построен")

    tasks = list_pending_tasks(store, statuses=tuple(statuses))
    if limit is not None:
        tasks = tasks[:limit]
    tracker = _QuotaTracker()

    def work(index: int, task: AgentTask) -> tuple[int, TaskResult]:
        # sqlite-соединение нельзя делить между потоками: в потоках — своя копия
        local_store = (
            DocumentStore(store.db_path, files_dir=store.files_dir)
            if cfg.workers > 1
            else store
        )
        try:
            result = _run_task_chained(
                task, local_store, chain=chain, config=cfg, tracker=tracker, home=home
            )
        finally:
            if local_store is not store:
                local_store.close()
        return index, result

    if not tasks:
        print("нет задач в очереди", flush=True)
        return []

    if cfg.workers > 1:
        with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
            done = dict(pool.map(lambda item: work(*item), enumerate(tasks)))
        results: list[TaskResult] = []
        total = len(tasks)
        for index, task in enumerate(tasks):
            result = done[index]
            _save_result(home, task.slug, result)
            results.append(result)
            print(f"[{index + 1}/{total}] {task.slug} ...", flush=True)
            detail = f"{result.status} за {result.duration_s:.1f} с"
            if result.error:
                detail += f" ({result.error})"
            if result.executor:
                detail += f" [{result.executor}]"
            print(f"    {detail}", flush=True)
        return results

    results = []
    total = len(tasks)
    for index, task in enumerate(tasks, start=1):
        print(f"[{index}/{total}] {task.slug} ...", flush=True)
        _, result = work(index - 1, task)
        _save_result(home, task.slug, result)
        results.append(result)
        detail = f"{result.status} за {result.duration_s:.1f} с"
        if result.error:
            detail += f" ({result.error})"
        if result.executor:
            detail += f" [{result.executor}]"
        print(f"    {detail}", flush=True)
        if index < total:
            time.sleep(_POLITE_DELAY_S)
    return results


def run_agent_tier(
    home: str | Path | None = None,
    *,
    only_missing: bool = True,
    limit: int | None = None,
) -> list[TaskResult]:
    """Точка входа агентного яруса для внешнего sync-harness.

    `geodocs sync` живёт в другом репозитории: после синхронизации статики
    он должен вызвать run_agent_tier(home, only_missing=True) — либо сам,
    либо (run_after_sync: true в agents.yaml) через свою обвязку. only_missing
    ограничивает очередь недобранными статусами загрузки.
    """
    home_path = (
        Path(home).expanduser()
        if home is not None
        else Path(os.environ.get("GEODOCS_HOME", str(Path.home() / ".geodocs")))
    )
    store = DocumentStore(home_path / "geodocs.sqlite3", files_dir=home_path / "files")
    try:
        statuses = (
            ("not_found", "pending")
            if only_missing
            else ("not_found", "pending", "download_failed", "search_failed")
        )
        return run_pending(
            store, limit=limit, statuses=statuses, geodocs_home=home_path
        )
    finally:
        store.close()
