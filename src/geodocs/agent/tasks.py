"""Задачи агентного яруса: версии, которые статический поиск не добрал."""

from __future__ import annotations

from pydantic import BaseModel, computed_field

from ..store import DocumentStore

_DEFAULT_STATUSES = ("not_found", "pending")

_PENDING_SQL = """
SELECT v.id AS version_id,
       d.doc_type AS doc_type,
       d.number AS number,
       d.municipality AS municipality,
       v.title AS title,
       v.issuer AS issuer,
       v.version_date AS version_date
FROM document_versions v
JOIN documents d ON d.id = v.document_id
WHERE v.fetch_status IN ({placeholders})
ORDER BY v.id
"""


class AgentTask(BaseModel):
    """Задача для внешнего агента: конкретная редакция без валидного файла."""

    version_id: int
    doc_type: str
    number: str
    municipality: str
    title: str | None
    issuer: str | None
    version_date: str

    @computed_field  # type: ignore[misc]
    @property
    def slug(self) -> str:
        """Каталог задачи: «{doc_type}-{number}» с «/», «-», «_» → «_», lower."""
        slug = f"{self.doc_type}-{self.number}"
        for char in ("/", "-", "_"):
            slug = slug.replace(char, "_")
        return slug.lower()


def list_pending_tasks(
    store: DocumentStore,
    statuses: tuple[str, ...] = _DEFAULT_STATUSES,
) -> list[AgentTask]:
    """Версии со статусом загрузки из `statuses` — очередь задач для агента."""
    if not statuses:
        return []
    placeholders = ", ".join("?" for _ in statuses)
    rows = store.connection.execute(
        _PENDING_SQL.format(placeholders=placeholders),
        statuses,
    ).fetchall()
    return [
        AgentTask(**dict(zip(row.keys(), row, strict=True))) for row in rows
    ]
