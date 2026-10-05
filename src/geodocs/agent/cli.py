"""CLI агентного яруса: список задач, запуск внешнего агента, recover без LLM."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..store import DocumentStore
from .runner import TaskResult, inbox_dir, recover_inbox, run_pending
from .tasks import list_pending_tasks

_DEFAULT_STATUSES = "not_found,pending"


def _parse_statuses(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _parse_only(raw: str | None) -> tuple[str, ...] | None:
    if not raw:
        return None
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def recover_pending(
    store: DocumentStore,
    *,
    only: Sequence[str] | None = None,
    statuses: Sequence[str] = ("not_found", "pending"),
    **kwargs: Any,
) -> list[TaskResult]:
    """Дожимает pending-задачи по уликам из inbox — без вызова агента.

    Задачи с пустым inbox/<slug>/ пропускаются с сообщением; остальные
    проходят verify_files → gate → record_agent_fetch. Возвращает результаты
    обработанных задач (пропущенных в списке нет).
    """
    tasks = list_pending_tasks(store, statuses=tuple(statuses))
    wanted = set(only) if only else None
    raw_home = kwargs.get("geodocs_home")
    home = Path(raw_home) if raw_home else store.db_path.parent

    results: list[TaskResult] = []
    recovered = skipped = rejected = 0
    for task in tasks:
        if wanted is not None and task.slug not in wanted:
            continue
        inbox = inbox_dir(home, task)
        has_files = inbox.is_dir() and any(p.is_file() for p in inbox.iterdir())
        if not has_files:
            skipped += 1
            print(f"пропуск {task.slug}: inbox пуст", flush=True)
            continue
        result = recover_inbox(task, store, geodocs_home=home)
        results.append(result)
        if result.status == "downloaded":
            recovered += 1
        else:
            rejected += 1
        detail = f"{task.slug}: {result.status}"
        if result.error:
            detail += f" ({result.error})"
        print(f"    {detail}", flush=True)
    print(
        f"итог recover: восстановлено {recovered}, пропущено {skipped},"
        f" отклонено гейтом {rejected}"
    )
    return results


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="geodocs-agent",
        description=(
            "Агентный ярус документного поиска: внешний агент (KIMI CLI)"
            " добирает документы, которые статический поиск не нашёл."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser(
        "list", help="показать pending-задачи агента"
    )
    list_parser.add_argument(
        "--status",
        default=_DEFAULT_STATUSES,
        help="статусы загрузки через запятую (по умолчанию: not_found,pending)",
    )

    run_parser = subparsers.add_parser(
        "run", help="запустить агента по pending-задачам"
    )
    run_parser.add_argument("--limit", type=int, default=None)
    run_parser.add_argument("--kimi-bin", default="kimi")
    run_parser.add_argument("--timeout-s", type=int, default=1800)
    run_parser.add_argument(
        "--status",
        default=_DEFAULT_STATUSES,
        help="статусы загрузки через запятую (по умолчанию: not_found,pending)",
    )

    recover_parser = subparsers.add_parser(
        "recover",
        help="принять готовые файлы из inbox без вызова агента (дожать обрывы)",
    )
    recover_parser.add_argument(
        "--only",
        default=None,
        help="только эти slug через запятую (по умолчанию: все pending)",
    )
    recover_parser.add_argument(
        "--status",
        default=_DEFAULT_STATUSES,
        help="статусы загрузки через запятую (по умолчанию: not_found,pending)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    store = DocumentStore.from_env()
    try:
        statuses = _parse_statuses(args.status)
        if args.command == "list":
            tasks = list_pending_tasks(store, statuses=statuses)
            for task in tasks:
                print(
                    f"{task.slug}\t№ {task.number}\t{task.version_date}\t"
                    f"{task.municipality}"
                )
            print(f"всего задач: {len(tasks)}")
            return 0

        if args.command == "recover":
            results = recover_pending(
                store,
                only=_parse_only(args.only),
                statuses=statuses,
                geodocs_home=store.db_path.parent,
            )
            errors = sum(1 for r in results if r.status != "downloaded")
            return 0 if errors == 0 else 1

        results = run_pending(
            store,
            limit=args.limit,
            statuses=statuses,
            kimi_bin=args.kimi_bin,
            timeout_s=args.timeout_s,
            geodocs_home=store.db_path.parent,
        )
        downloaded = sum(1 for r in results if r.status == "downloaded")
        not_found = sum(1 for r in results if r.status == "not_found")
        errors = sum(
            1 for r in results if r.status in {"agent_error", "gate_failed"}
        )
        total_bytes = sum(
            verdict.size
            for r in results
            if r.status == "downloaded"
            for verdict in r.verdicts
            if verdict.ok
        )
        print(
            f"итог: скачано {downloaded}, не найдено {not_found},"
            f" ошибок {errors}, суммарный размер {total_bytes / 1024 / 1024:.1f} МБ"
        )
        return 0 if errors == 0 else 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
