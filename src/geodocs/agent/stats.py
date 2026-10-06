"""Сводка по агентному ярусу: провайдеры в БД + per-task статусы из result-JSON."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..store import DocumentStore


@dataclass(frozen=True)
class ProviderStat:
    """Скачанные версии и файлы одного провайдера."""

    provider: str
    versions: int
    files: int


@dataclass(frozen=True)
class TaskStat:
    """Последний result-JSON одной задачи."""

    slug: str
    status: str
    executor: str | None
    attempts: int
    quota_exhausted: tuple[str, ...] = ()
    error: str | None = None
    finished_at: str | None = None


@dataclass(frozen=True)
class AgentStats:
    """Вся сводка для команды `geodocs-agent stats`."""

    providers: list[ProviderStat]
    tasks: list[TaskStat]
    status_counts: dict[str, int] = field(default_factory=dict)
    quota_counts: dict[str, int] = field(default_factory=dict)
    manual_required: list[str] = field(default_factory=list)


def collect_stats(store: DocumentStore, home: Path) -> AgentStats:
    """Собирает статистику: БД (провайдеры) + каталог result-JSON (задачи)."""
    rows = store.connection.execute(
        "SELECT dv.source_provider AS provider,"
        " COUNT(DISTINCT dv.id) AS versions, COUNT(vf.id) AS files"
        " FROM document_versions dv"
        " LEFT JOIN version_files vf ON vf.version_id = dv.id"
        " WHERE dv.fetch_status = 'downloaded'"
        " GROUP BY dv.source_provider ORDER BY provider"
    ).fetchall()
    providers = [
        ProviderStat(
            provider=row["provider"] or "unknown",
            versions=row["versions"],
            files=row["files"],
        )
        for row in rows
    ]

    tasks: list[TaskStat] = []
    results_dir = Path(home) / "agent" / "results"
    if results_dir.is_dir():
        for path in sorted(results_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(raw, dict):
                continue
            mtime = datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc
            ).isoformat()
            quota = raw.get("quota_exhausted") or []
            tasks.append(
                TaskStat(
                    slug=path.stem,
                    status=str(raw.get("status") or "unknown"),
                    executor=raw.get("executor"),
                    attempts=int(raw.get("attempts") or 1),
                    quota_exhausted=tuple(str(name) for name in quota),
                    error=raw.get("error"),
                    finished_at=mtime,
                )
            )

    status_counts: dict[str, int] = {}
    quota_counts: dict[str, int] = {}
    manual: list[str] = []
    for task in tasks:
        status_counts[task.status] = status_counts.get(task.status, 0) + 1
        for name in task.quota_exhausted:
            quota_counts[name] = quota_counts.get(name, 0) + 1
        if task.status == "manual_required":
            manual.append(task.slug)
    return AgentStats(
        providers=providers,
        tasks=tasks,
        status_counts=status_counts,
        quota_counts=quota_counts,
        manual_required=sorted(manual),
    )


def stats_to_dict(stats: AgentStats) -> dict[str, Any]:
    """Сериализация сводки для флага --json."""
    return {
        "providers": [
            {
                "provider": p.provider,
                "versions": p.versions,
                "files": p.files,
            }
            for p in stats.providers
        ],
        "tasks": [
            {
                "slug": t.slug,
                "status": t.status,
                "executor": t.executor,
                "attempts": t.attempts,
                "quota_exhausted": list(t.quota_exhausted),
                "error": t.error,
                "finished_at": t.finished_at,
            }
            for t in stats.tasks
        ],
        "status_counts": stats.status_counts,
        "quota_counts": stats.quota_counts,
        "manual_required": stats.manual_required,
    }


def render_stats(stats: AgentStats) -> str:
    """Человекочитаемая таблица сводки."""
    lines: list[str] = []
    lines.append("провайдеры (скачанные версии):")
    if stats.providers:
        width = max(len(p.provider) for p in stats.providers)
        for p in stats.providers:
            lines.append(
                f"  {p.provider:<{width}}  версий {p.versions:>4}, файлов {p.files:>4}"
            )
    else:
        lines.append("  (нет скачанных версий)")

    lines.append("")
    lines.append("задачи (последние результаты):")
    if stats.tasks:
        width = max(len(t.slug) for t in stats.tasks)
        for t in stats.tasks:
            detail = f"  {t.slug:<{width}}  {t.status}"
            if t.executor:
                detail += f" [{t.executor}]"
            if t.quota_exhausted:
                detail += f" quota: {', '.join(t.quota_exhausted)}"
            lines.append(detail)
    else:
        lines.append("  (result-JSON нет)")

    lines.append("")
    lines.append(
        "статусы: "
        + (
            ", ".join(
                f"{name}={count}" for name, count in sorted(stats.status_counts.items())
            )
            or "(нет)"
        )
    )
    lines.append(
        "quota-исчерпания: "
        + (
            ", ".join(
                f"{name}={count}" for name, count in sorted(stats.quota_counts.items())
            )
            or "(нет)"
        )
    )
    lines.append("manual_required: " + (", ".join(stats.manual_required) or "(нет)"))
    return "\n".join(lines)
