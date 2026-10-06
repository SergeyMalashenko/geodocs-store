"""CLI агентного яруса: список задач, запуск по цепочке агентов, recover, stats."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..store import DocumentStore
from .config import AgentConfigError, AgentTierConfig, load_config
from .runner import TaskResult, inbox_dir, recover_inbox, run_pending
from .stats import collect_stats, render_stats, stats_to_dict
from .tasks import list_pending_tasks

_DEFAULT_STATUSES = "not_found,pending"


def _parse_statuses(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _parse_only(raw: str | None) -> tuple[str, ...] | None:
    if not raw:
        return None
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _parse_chain(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [part.strip() for part in raw.split(",") if part.strip()]


def recover_pending(
    store: DocumentStore,
    *,
    only: Sequence[str] | None = None,
    statuses: Sequence[str] = ("not_found", "pending"),
    **kwargs: Any,
) -> list[TaskResult]:
    """Дожимает pending-задачи по уликам из inbox — без вызова агента.

    Задачи с пустым inbox/<slug>/ пропускаются с сообщением; остальные
    проходят verify_files → gate → record_agent_fetch (provider=manual).
    Задачи с result-JSON manual_required остаются в очереди БД и сюда тоже
    попадают — recover показывает всё, что ещё не скачано.
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


def _print_run_plan(
    tasks: Sequence[Any],
    *,
    chain: Sequence[str],
    config: AgentTierConfig,
    limit: int | None,
) -> None:
    print("план прогона (dry-run, ничего не запускается):")
    print(f"  цепочка: {' → '.join(chain)}")
    print(
        f"  retry: до {config.retry_attempts} проходов,"
        f" пауза {config.retry_pause_seconds} с; workers={config.workers}"
    )
    shown = list(tasks)[:limit] if limit is not None else list(tasks)
    print(f"  задачи ({len(shown)}):")
    for task in shown:
        print(f"    {task.slug}\t№ {task.number}\t{task.version_date}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="geodocs-agent",
        description=(
            "Агентный ярус документного поиска: внешние агенты"
            " добирают документы, которые статический поиск не нашёл."
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
        "run", help="запустить цепочку агентов по pending-задачам"
    )
    run_parser.add_argument("--limit", type=int, default=None)
    run_parser.add_argument(
        "--status",
        default=_DEFAULT_STATUSES,
        help="статусы загрузки через запятую (по умолчанию: not_found,pending)",
    )
    run_parser.add_argument(
        "--chain",
        default=None,
        help="порядок failover поверх конфига: имена исполнителей из agents.yaml",
    )
    run_parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="потоков одновременно (по умолчанию из agents.yaml: workers)",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="показать план (задачи × исполнители × retry), ничего не запуская",
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

    stats_parser = subparsers.add_parser(
        "stats", help="сводка: провайдеры из БД, статусы задач, quota, manual_required"
    )
    stats_parser.add_argument(
        "--json", action="store_true", help="вывести сводку как JSON"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    store = DocumentStore.from_env()
    try:
        home = store.db_path.parent

        if args.command == "list":
            tasks = list_pending_tasks(store, statuses=_parse_statuses(args.status))
            for task in tasks:
                print(
                    f"{task.slug}\t№ {task.number}\t{task.version_date}\t"
                    f"{task.municipality}"
                )
            print(f"всего задач: {len(tasks)}")
            return 0

        if args.command == "stats":
            stats = collect_stats(store, home)
            if args.json:
                print(json.dumps(stats_to_dict(stats), ensure_ascii=False, indent=2))
            else:
                print(render_stats(stats))
            return 0

        if args.command == "recover":
            results = recover_pending(
                store,
                only=_parse_only(args.only),
                statuses=_parse_statuses(args.status),
                geodocs_home=home,
            )
            errors = sum(1 for r in results if r.status != "downloaded")
            return 0 if errors == 0 else 1

        # run
        try:
            config = load_config(home)
        except AgentConfigError as exc:
            print(f"agents.yaml: {exc}", file=sys.stderr)
            return 2
        chain_override = _parse_chain(args.chain)
        if chain_override is not None:
            unknown = [name for name in chain_override if name not in config.executors]
            if unknown:
                print(
                    f"--chain: неизвестные исполнители: {', '.join(unknown)}"
                    f" (известные: {', '.join(config.executors)})",
                    file=sys.stderr,
                )
                return 2
            config = dataclasses.replace(config, chain=chain_override)
        if args.workers is not None:
            if args.workers < 1:
                print("--workers должно быть ≥ 1", file=sys.stderr)
                return 2
            config = dataclasses.replace(config, workers=args.workers)

        if args.dry_run:
            tasks = list_pending_tasks(store, statuses=_parse_statuses(args.status))
            _print_run_plan(
                tasks, chain=config.chain, config=config, limit=args.limit
            )
            return 0

        results = run_pending(
            store,
            limit=args.limit,
            statuses=_parse_statuses(args.status),
            geodocs_home=home,
            config=config,
        )
        downloaded = sum(1 for r in results if r.status == "downloaded")
        not_found = sum(1 for r in results if r.status == "not_found")
        manual = sum(1 for r in results if r.status == "manual_required")
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
            f" нужен ручной добор {manual},"
            f" ошибок {errors}, суммарный размер {total_bytes / 1024 / 1024:.1f} МБ"
        )
        return 0 if errors == 0 and manual == 0 else 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
