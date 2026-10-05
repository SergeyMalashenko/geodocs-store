"""Запуск внешнего агента (KIMI CLI) по задачам и приём результата в geodocs."""

from __future__ import annotations

import hashlib
import json
import shlex
import shutil
import subprocess
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel

from ..models import FileRecord
from ..store import DocumentStore
from .gate import FileVerdict, gate_pass, verify_files
from .prompt import build_prompt
from .tasks import AgentTask, list_pending_tasks

MANIFEST_MARKER = "=== MANIFEST ==="
_POLITE_DELAY_S = 5
_TAIL_LEN = 1000

TaskStatus = Literal["downloaded", "not_found", "agent_error", "gate_failed"]


class TaskResult(BaseModel):
    """Итог прогона одной задачи через внешнего агента."""

    task: AgentTask
    manifest: dict[str, Any] | None
    verdicts: list[FileVerdict]
    status: TaskStatus
    error: str | None
    duration_s: float
    stdout_tail: str | None = None
    stderr_tail: str | None = None


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
        if not line.startswith(MANIFEST_MARKER):
            continue
        candidates = (
            line[len(MANIFEST_MARKER):].strip(),
            "\n".join(lines[index + 1:]).strip(),
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
) -> bool:
    """Общий шаг приёма: гейт по вердиктам и регистрация в БД. True = принято."""
    if not gate_pass(verdicts):
        return False
    store.record_agent_fetch(
        task.version_id,
        files=[_file_record(path) for path in copied],
        source_url=source_url,
        fetched_at=_utcnow(),
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


def run_task(
    task: AgentTask,
    store: DocumentStore,
    *,
    kimi_bin: str = "kimi",
    timeout_s: int = 1800,
    geodocs_home: Path | None = None,
) -> TaskResult:
    """Запускает внешнего агента для одной задачи и фиксирует результат.

    При прохождении гейта версия переходит в downloaded (record_agent_fetch),
    иначе состояние в БД не меняется, а причина — в TaskResult.error. Если
    агент завершился без манифеста, но в inbox лежит валидный набор файлов,
    результат дожимается детерминированно по уликам из inbox.
    """
    home = Path(geodocs_home) if geodocs_home is not None else store.db_path.parent
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
        proc = subprocess.run(
            [kimi_bin, "-p", prompt],
            cwd=str(home),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,  # код возврата разбираем сами ниже
        )
    except subprocess.TimeoutExpired as exc:
        error = f"агент не уложился в {timeout_s} с"
        stdout_tail = _tail(exc.stdout)
        stderr_tail = _tail(exc.stderr)
    except OSError as exc:
        error = f"не удалось запустить {shlex.quote(kimi_bin)}: {exc}"
    else:
        stdout_tail = _tail(proc.stdout)
        stderr_tail = _tail(proc.stderr)
        if proc.returncode != 0:
            status = "agent_error"
            error = (
                f"kimi завершился с кодом {proc.returncode}:"
                f" {proc.stderr.strip()[:400]}"
            )
        else:
            manifest = parse_manifest(proc.stdout)
            if manifest is None:
                # манифест потерян (типично: квота убила агента после
                # скачивания) — принимаем по уликам из inbox
                verdicts, copied = _collect_files(
                    _inbox_names(inbox), inbox, store, task
                )
                if _gate_and_register(verdicts, copied, store, task, None):
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
                    verdicts, copied, store, task, _manifest_source_url(manifest)
                ):
                    status = "downloaded"
                else:
                    status = "gate_failed"
                    error = (
                        _failed_reasons(verdicts)
                        or "агент не заявил ни одного файла"
                    )

    result = TaskResult(
        task=task,
        manifest=manifest,
        verdicts=verdicts,
        status=status,
        error=error,
        duration_s=time.monotonic() - started,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
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
    source_url неизвестен — record_agent_fetch получает None.
    """
    home = Path(geodocs_home) if geodocs_home is not None else store.db_path.parent
    inbox = inbox_dir(home, task)
    started = time.monotonic()

    verdicts, copied = _collect_files(_inbox_names(inbox), inbox, store, task)
    if _gate_and_register(verdicts, copied, store, task, None):
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
    )
    _save_result(home, task.slug, result)
    return result


def run_pending(
    store: DocumentStore,
    *,
    limit: int | None = None,
    statuses: Sequence[str] = ("not_found", "pending"),
    **kwargs: Any,
) -> list[TaskResult]:
    """Последовательный прогон очереди задач с вежливой паузой между запусками."""
    tasks = list_pending_tasks(store, statuses=tuple(statuses))
    if limit is not None:
        tasks = tasks[:limit]
    results: list[TaskResult] = []
    total = len(tasks)
    for index, task in enumerate(tasks, start=1):
        print(f"[{index}/{total}] {task.slug} ...", flush=True)
        result = run_task(task, store, **kwargs)
        results.append(result)
        detail = f"{result.status} за {result.duration_s:.1f} с"
        if result.error:
            detail += f" ({result.error})"
        print(f"    {detail}", flush=True)
        if index < total:
            time.sleep(_POLITE_DELAY_S)
    return results
