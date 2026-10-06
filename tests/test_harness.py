"""Тесты пилота «Kimi Harness со статическими инструментами».

Покрывает: реестр и адаптеры порталов (HTTP мокается respx), MCP-хендлеры
(in-process, без транспорта), skills-валидацию, wiring KimiExecutor
(mcp.json/workspace-trust/--skills-dir) и порядок инструментов в промпте.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import yaml

from geodocs import (
    DocRole,
    DocType,
    DocumentRef,
    DocumentStore,
    FetchStatus,
    SourceName,
)
from geodocs.agent import MANIFEST_MARKER, KimiExecutor, run_pending
from geodocs.agent.config import ExecutorConfig
from geodocs.agent.mcp import (
    McpContext,
    check_local_store_impl,
    download_document_impl,
    fetch_page_impl,
    search_document_impl,
)
from geodocs.agent.portals import (
    Candidate,
    DocQuery,
    PortalError,
    get_portal,
    list_portals,
    register_portal,
)
from geodocs.agent.prompt import build_prompt
from geodocs.agent.tasks import AgentTask

MUNICIPALITY = "Городской округ Солнечногорск"
AMENDMENT_DATE = "2026-04-09"

SHATURA_QUERY = DocQuery(
    municipality="Муниципальный округ Шатура",
    doc_type="pzz",
    number="1069",
    version_date="2026-06-08",
    title="Правила землепользования и застройки",
)
KOLOMNA_QUERY = DocQuery(
    municipality="Городской округ Коломна",
    doc_type="pzz",
    number="1198",
    version_date="2026-04-17",
)

_PDF_BYTES = b"%PDF-1.4\n" + b"A" * 1000

_MEGANORM_RESULTS = """
<html><body>
<div class="result"><a href="/Data2/1/4293841/4293841858.htm">СП 42.13330</a></div>
<div class="result"><a href="/Data2/2/5000001/5000001123.htm">МГСН 1.01-99</a></div>
</body></html>
"""

_CNTD_SEARCH_HTML = """
<html><body>
<a href="/document/1200123456">Постановление № 1069 от 08.06.2026</a>
<a href="/document/1200654321">Другое</a>
</body></html>
"""

_FGISTP_CARD = """
<html><body>
<a href="/ais/file?id=9EAC2E0EFA900244A8A19C07D5AD2F1B&name=pzz.zip">Скачать комплект</a>
</body></html>
"""

_PRAVO_RESULTS = """
<html><body>
<a href="/document/0001202606080001">О внесении изменений в ПЗЗ</a>
</body></html>
"""

_MOSREG_RESULTS = """
<html><body>
<a href="/dataset/pzz-shatura-1069">ПЗЗ Шатуры, изменения 1069</a>
</body></html>
"""

_MUNICIPAL_PAGE = """
<html><body>
<p>Правила землепользования и застройки, изменения № 1069 от 08.06.2026</p>
<a href="/docs/pzz_1069.pdf">Текст изменений (PDF)</a>
<a href="/docs/map_1069.png">Карта</a>
</body></html>
"""


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    """Временный GEODOCS_HOME (только каталог; БД создаётся по требованию)."""
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    return home_dir


@pytest.fixture()
def portal_registry() -> Iterator[None]:
    """Восстанавливает реестр порталов после тестов."""
    from geodocs.agent.portals import registry as registry_module

    snapshot = dict(registry_module._PORTALS)
    yield
    registry_module._PORTALS.clear()
    registry_module._PORTALS.update(snapshot)


# ---------------------------------------------------------------------------
# Реестр порталов
# ---------------------------------------------------------------------------


def test_portal_registry_roundtrip(portal_registry: None) -> None:
    class _StubPortal:
        name = "stub-portal"

    assert "meganorm" in list_portals()
    register_portal("stub-portal", _StubPortal)
    assert get_portal("stub-portal").name == "stub-portal"
    assert "stub-portal" in list_portals()
    with pytest.raises(ValueError, match="уже зарегистрирован"):
        register_portal("stub-portal", _StubPortal)


def test_portal_registry_unknown() -> None:
    with pytest.raises(KeyError, match="неизвестный портал"):
        get_portal("ghost")


def test_builtin_portals_registered() -> None:
    names = list_portals()
    for expected in ("meganorm", "cntd", "fgistp", "pravo", "mosreg", "municipal"):
        assert expected in names


# ---------------------------------------------------------------------------
# Адаптеры (HTTP замокан respx)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_meganorm_search_parses_results() -> None:
    adapter = get_portal("meganorm")
    async with adapter._client:
        with respx.mock:
            respx.get("https://meganorm.ru/Search2/search").mock(
                return_value=httpx.Response(200, text=_MEGANORM_RESULTS)
            )
            candidates = await adapter.search(SHATURA_QUERY)
    assert len(candidates) == 2
    assert candidates[0].url.startswith("https://meganorm.ru/Data2/")
    assert candidates[0].meta["html_fulltext"] is True


@pytest.mark.asyncio()
async def test_meganorm_fetch_refuses_html_card(tmp_path: Path) -> None:
    adapter = get_portal("meganorm")
    candidate = Candidate(
        url="https://meganorm.ru/Data2/1/4293841/4293841858.htm",
        title="СП 42.13330",
        portal="meganorm",
    )
    async with adapter._client:
        with respx.mock:
            route = respx.get(candidate.url).mock(
                return_value=httpx.Response(200, text="<html>полный текст</html>")
            )
            with pytest.raises(PortalError, match="HTML полного текста"):
                await adapter.fetch(candidate, tmp_path)
    assert route.called


@pytest.mark.asyncio()
async def test_meganorm_fetch_downloads_linked_file(tmp_path: Path) -> None:
    adapter = get_portal("meganorm")
    candidate = Candidate(
        url="https://meganorm.ru/Data2/1/4293841/4293841858.htm",
        title="СП 42.13330",
        portal="meganorm",
    )
    card = '<html><a href="/files/sp.pdf">Скачать PDF</a></html>'
    async with adapter._client:
        with respx.mock:
            respx.get(candidate.url).mock(return_value=httpx.Response(200, text=card))
            respx.get("https://meganorm.ru/files/sp.pdf").mock(
                return_value=httpx.Response(
                    200, content=_PDF_BYTES, headers={"content-type": "application/pdf"}
                )
            )
            path = await adapter.fetch(candidate, tmp_path)
    assert path.read_bytes() == _PDF_BYTES


@pytest.mark.asyncio()
async def test_cntd_search_and_fetch_blocks(tmp_path: Path) -> None:
    adapter = get_portal("cntd")
    async with adapter._client:
        with respx.mock:
            respx.get("https://docs.cntd.ru/search").mock(
                return_value=httpx.Response(200, text=_CNTD_SEARCH_HTML)
            )
            candidates = await adapter.search(SHATURA_QUERY)
            assert [c.meta["document_id"] for c in candidates] == [
                "1200123456",
                "1200654321",
            ]
            base = "https://docs.cntd.ru/api/document/1200123456"
            respx.get(f"{base}/content/text/blocks/size").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "data": {"content": [{"block_index": 1}, {"block_index": 2}]}
                    },
                )
            )
            respx.get(f"{base}/content/text/block/1").mock(
                return_value=httpx.Response(200, text="<div>блок 1</div>")
            )
            respx.get(f"{base}/content/text/block/2").mock(
                return_value=httpx.Response(200, text="<div>блок 2</div>")
            )
            path = await adapter.fetch(candidates[0], tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "docs.cntd.ru/document/1200123456" in text
    assert "<div>блок 1</div>" in text and "<div>блок 2</div>" in text


@pytest.mark.asyncio()
async def test_cntd_fetch_fails_when_no_blocks(tmp_path: Path) -> None:
    adapter = get_portal("cntd")
    candidate = Candidate(
        url="https://docs.cntd.ru/document/1200123456",
        title="CNTD",
        portal="cntd",
        meta={"document_id": "1200123456"},
    )
    async with adapter._client:
        with respx.mock:
            base = "https://docs.cntd.ru/api/document/1200123456"
            respx.get(f"{base}/content/text/blocks/size").mock(
                return_value=httpx.Response(200, json={"data": {"content": []}})
            )
            with pytest.raises(PortalError, match="ни один текстовый блок"):
                await adapter.fetch(candidate, tmp_path)


@pytest.mark.asyncio()
async def test_fgistp_search_is_byo_url_and_fetch_parses_card(tmp_path: Path) -> None:
    adapter = get_portal("fgistp")
    assert await adapter.search(KOLOMNA_QUERY) == []
    candidate = Candidate(
        url="https://fgistp.economy.gov.ru/ais/of1?id=9EAC2E0EFA900244A8A19C07D5AD2F1B",
        title="ПЗЗ",
        portal="fgistp",
    )
    async with adapter._client:
        with respx.mock:
            respx.get("https://fgistp.economy.gov.ru/ais/of1").mock(
                return_value=httpx.Response(200, text=_FGISTP_CARD)
            )
            respx.get("https://fgistp.economy.gov.ru/ais/file").mock(
                return_value=httpx.Response(
                    200,
                    content=b"PK\x03\x04" + b"B" * 100,
                    headers={"content-type": "application/zip"},
                )
            )
            path = await adapter.fetch(candidate, tmp_path)
    assert path.suffix == ".zip"


@pytest.mark.asyncio()
async def test_fgistp_restricted_access_raises(tmp_path: Path) -> None:
    adapter = get_portal("fgistp")
    candidate = Candidate(
        url="https://fgistp.economy.gov.ru/ais/of1?id=9EAC2E0EFA900244A8A19C07D5AD2F1B",
        title="ПЗЗ",
        portal="fgistp",
    )
    async with adapter._client:
        with respx.mock:
            respx.get("https://fgistp.economy.gov.ru/ais/of1").mock(
                return_value=httpx.Response(200, text="<html>Доступ ограничен</html>")
            )
            with pytest.raises(PortalError, match="ограничен"):
                await adapter.fetch(candidate, tmp_path)


@pytest.mark.asyncio()
async def test_pravo_search_and_fetch(tmp_path: Path) -> None:
    adapter = get_portal("pravo")
    async with adapter._client:
        with respx.mock:
            respx.get("https://publication.pravo.gov.ru/Search").mock(
                return_value=httpx.Response(200, text=_PRAVO_RESULTS)
            )
            candidates = await adapter.search(SHATURA_QUERY)
            assert len(candidates) == 1
            assert candidates[0].url.startswith(
                "https://publication.pravo.gov.ru/document/"
            )
            card = '<html><a href="/Download/0001.pdf">Скачать документ</a></html>'
            respx.get(candidates[0].url).mock(
                return_value=httpx.Response(200, text=card)
            )
            respx.get("https://publication.pravo.gov.ru/Download/0001.pdf").mock(
                return_value=httpx.Response(
                    200,
                    content=_PDF_BYTES,
                    headers={"content-type": "application/pdf"},
                )
            )
            path = await adapter.fetch(candidates[0], tmp_path)
    assert path.read_bytes() == _PDF_BYTES


@pytest.mark.asyncio()
async def test_mosreg_search_and_fetch(tmp_path: Path) -> None:
    adapter = get_portal("mosreg")
    async with adapter._client:
        with respx.mock:
            respx.get("https://data.mosreg.ru/search").mock(
                return_value=httpx.Response(200, text=_MOSREG_RESULTS)
            )
            candidates = await adapter.search(KOLOMNA_QUERY)
            assert len(candidates) == 1
            assert (
                candidates[0].url == "https://data.mosreg.ru/dataset/pzz-shatura-1069"
            )
            card = '<html><a href="/files/pzz_1069.zip">Скачать</a></html>'
            respx.get(candidates[0].url).mock(
                return_value=httpx.Response(200, text=card)
            )
            respx.get("https://data.mosreg.ru/files/pzz_1069.zip").mock(
                return_value=httpx.Response(
                    200,
                    content=b"PK\x03\x04" + b"C" * 100,
                    headers={"content-type": "application/zip"},
                )
            )
            path = await adapter.fetch(candidates[0], tmp_path)
    assert path.suffix == ".zip"


@pytest.mark.asyncio()
async def test_municipal_fetch_page_text_and_files(tmp_path: Path) -> None:
    adapter = get_portal("municipal")
    candidate = Candidate(
        url="https://shatura-adm.ru/pzz/1069", title="page", portal="municipal"
    )
    async with adapter._client:
        with respx.mock:
            respx.get(candidate.url).mock(
                return_value=httpx.Response(200, text=_MUNICIPAL_PAGE)
            )
            respx.get("https://shatura-adm.ru/docs/pzz_1069.pdf").mock(
                return_value=httpx.Response(
                    200,
                    content=_PDF_BYTES,
                    headers={"content-type": "application/pdf"},
                )
            )
            # png-ссылка тоже пройдёт через file_links, но content-type png —
            # муниципальный адаптер скачает любой не-HTML файл.
            respx.get("https://shatura-adm.ru/docs/map_1069.png").mock(
                return_value=httpx.Response(
                    200, content=b"\x89PNG", headers={"content-type": "image/png"}
                )
            )
            path = await adapter.fetch(candidate, tmp_path)
    assert path.name == "page_text.txt"
    text = path.read_text(encoding="utf-8")
    assert "изменения № 1069 от 08.06.2026" in text
    assert (tmp_path / "pzz_1069.pdf").exists()
    assert (tmp_path / "map_1069.png").exists()


# ---------------------------------------------------------------------------
# MCP-инструменты (in-process)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_mcp_search_merges_portals_and_isolates_errors(
    portal_registry: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import geodocs.agent.mcp as mcp_module

    class _BoomPortal:
        name = "boom"

        async def search(self, query: DocQuery) -> list[Candidate]:
            raise PortalError("портал упал")

    register_portal("boom", _BoomPortal)
    monkeypatch.setattr(mcp_module, "list_portals", lambda: ["boom", "meganorm"])
    monkeypatch.setattr(
        mcp_module,
        "get_portal",
        lambda name: _BoomPortal() if name == "boom" else get_portal(name),
    )

    with respx.mock:
        respx.get("https://meganorm.ru/Search2/search").mock(
            return_value=httpx.Response(200, text=_MEGANORM_RESULTS)
        )
        result = await search_document_impl(
            municipality=SHATURA_QUERY.municipality,
            doc_type=SHATURA_QUERY.doc_type,
            number=SHATURA_QUERY.number,
            version_date=SHATURA_QUERY.version_date,
            title=SHATURA_QUERY.title,
        )
    assert result["portals_queried"] == ["boom", "meganorm"]
    assert result["errors"] == {"boom": "портал упал"}
    assert {c["portal"] for c in result["candidates"]} == {"meganorm"}
    assert all("url" in c and "title" in c for c in result["candidates"])


@pytest.mark.asyncio()
async def test_mcp_download_direct_refuses_html(tmp_path: Path) -> None:
    ctx = McpContext(inbox=tmp_path / "inbox", home=tmp_path)
    with respx.mock:
        respx.get("https://shatura-adm.ru/pzz/1069.pdf").mock(
            return_value=httpx.Response(
                200,
                content=_PDF_BYTES,
                headers={"content-type": "application/pdf"},
            )
        )
        ok = await download_document_impl(ctx, "https://shatura-adm.ru/pzz/1069.pdf")
    assert ok["size"] == len(_PDF_BYTES)
    assert Path(ok["path"]).exists()

    ctx2 = McpContext(inbox=tmp_path / "inbox2", home=tmp_path)
    with respx.mock:
        respx.get("https://shatura-adm.ru/page").mock(
            return_value=httpx.Response(
                200, text="<html>страница</html>", headers={"content-type": "text/html"}
            )
        )
        with pytest.raises(PortalError, match="HTML-страницу"):
            await download_document_impl(ctx2, "https://shatura-adm.ru/page")


@pytest.mark.asyncio()
async def test_mcp_fetch_page(tmp_path: Path) -> None:
    ctx = McpContext(inbox=tmp_path / "inbox", home=tmp_path)
    with respx.mock:
        respx.get("https://shatura-adm.ru/pzz").mock(
            return_value=httpx.Response(200, text=_MUNICIPAL_PAGE)
        )
        respx.get("https://shatura-adm.ru/docs/pzz_1069.pdf").mock(
            return_value=httpx.Response(
                200,
                content=_PDF_BYTES,
                headers={"content-type": "application/pdf"},
            )
        )
        respx.get("https://shatura-adm.ru/docs/map_1069.png").mock(
            return_value=httpx.Response(
                200, content=b"\x89PNG", headers={"content-type": "image/png"}
            )
        )
        result = await fetch_page_impl(ctx, "https://shatura-adm.ru/pzz")
    assert "изменения № 1069" in result["text_preview"]
    assert "pzz_1069.pdf" in result["inbox_files"]


def test_mcp_check_local_store(tmp_path: Path) -> None:
    home = tmp_path / "home"
    store = DocumentStore(home / "geodocs.sqlite3", files_dir=home / "files")
    ctx = McpContext(inbox=home / "inbox", home=home)

    unknown = check_local_store_impl(ctx, "pzz", "1069", "2026-06-08")
    assert unknown == {"known": False}

    ref = DocumentRef(
        municipality="Муниципальный округ Шатура",
        doc_type=DocType.PZZ,
        number="1069",
        version_date="2026-06-08",
        role=DocRole.AMENDMENT,
        source=SourceName.RGIS,
        source_object_id="rgis-1069",
        amendment_number="1069",
    )
    version_id = store.register_ref(ref)
    store.set_fetch_status(version_id, FetchStatus.NOT_FOUND)
    store.close()

    known = check_local_store_impl(ctx, "pzz", "1069", "2026-06-08")
    assert known["known"] is True
    (version,) = known["versions"]
    assert version["municipality"] == "Муниципальный округ Шатура"
    assert version["fetch_status"] == "not_found"


# ---------------------------------------------------------------------------
# Skills: frontmatter и уникальность имён
# ---------------------------------------------------------------------------


def test_skills_frontmatter_valid() -> None:
    skills_dir = Path(__file__).parent.parent / "src" / "geodocs" / "agent" / "skills"
    skills = sorted(path for path in skills_dir.iterdir() if path.is_dir())
    assert len(skills) == 4
    names: list[str] = []
    for skill_dir in skills:
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        assert text.startswith("---\n"), f"{skill_dir.name}: нет frontmatter"
        frontmatter = yaml.safe_load(text.split("---\n", 2)[1])
        for field in ("name", "description", "whenToUse"):
            assert frontmatter.get(field), f"{skill_dir.name}: пустое поле {field}"
        assert frontmatter["name"] == skill_dir.name
        names.append(frontmatter["name"])
    assert len(set(names)) == len(names), "имена скилов должны быть уникальны"


# ---------------------------------------------------------------------------
# Wiring: KimiExecutor материализует mcp.json/trust и argv скилов
# ---------------------------------------------------------------------------


def _kimi_cfg(**overrides: Any) -> ExecutorConfig:
    values: dict[str, Any] = {
        "name": "kimi",
        "type": "kimi",
        "command": "kimi",
        "args": ["-p"],
        "timeout_seconds": 900,
        "quota_patterns": [],
    }
    values.update(overrides)
    return ExecutorConfig(**values)


def _prompt(inbox: Path) -> str:
    return f"Тест. СКАЧИВАНИЕ: каталог {inbox} (mkdir -p)."


def test_kimi_executor_materializes_mcp_and_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kimi_home = tmp_path / "kimi-home"
    monkeypatch.setenv("KIMI_HOME", str(kimi_home))
    workdir = tmp_path / "workdir"  # рабочий каталог задачи — НЕ home базы
    home = tmp_path / "geodocs-home"
    inbox = workdir / "inbox" / "pzz_1069"
    workdir.mkdir(parents=True)

    argv_seen: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        argv_seen.append(argv)
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout="ok", stderr=""
        )

    monkeypatch.setattr("geodocs.agent.executors.subprocess.run", fake_run)
    executor = KimiExecutor(dataclasses.replace(_kimi_cfg(), home=home))
    result = executor.run(_prompt(inbox), workdir)

    assert result.returncode == 0
    # argv: kimi --skills-dir <пакетные скилы> -p <промт>
    argv = argv_seen[0]
    assert argv[0] == "kimi"
    skills_index = argv.index("--skills-dir")
    assert Path(argv[skills_index + 1]).name == "skills"
    assert argv.index("-p") > skills_index  # флаги до -p
    assert argv[-1].startswith("Тест.")

    mcp_config = json.loads(
        (workdir / ".kimi-code" / "mcp.json").read_text(encoding="utf-8")
    )
    server = mcp_config["mcpServers"]["geodocs"]
    assert server["command"] == sys.executable
    assert server["args"] == ["-m", "geodocs.agent.mcp"]
    # GEODOCS_HOME — общая база из конфига исполнителя, не рабочий каталог
    assert server["env"]["GEODOCS_HOME"] == str(home)
    assert server["env"]["GEODOCS_AGENT_INBOX"] == str(inbox)

    # workspace-trust для workdir создан в изолированном KIMI_HOME
    digest = hashlib.sha256(os.path.realpath(workdir).encode()).hexdigest()[:12]
    trust_file = kimi_home / "workspace-trust" / f"wd_workdir_{digest}"
    assert trust_file.exists()
    assert json.loads(trust_file.read_text(encoding="utf-8"))[
        "root"
    ] == os.path.realpath(workdir)


def test_runner_passes_home_and_mcp_check_local_store_sees_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сквозная проводка: раннер подставляет home → mcp.json → check_local_store."""
    monkeypatch.setenv("KIMI_HOME", str(tmp_path / "kimi-home"))
    home = tmp_path / "home"
    store = DocumentStore(home / "geodocs.sqlite3", files_dir=home / "files")
    version_id = store.register_ref(
        DocumentRef(
            municipality="Муниципальный округ Шатура",
            doc_type=DocType.PZZ,
            number="1069",
            version_date="2026-06-08",
            role=DocRole.AMENDMENT,
            title="Правила землепользования и застройки",
            source=SourceName.RGIS,
            source_object_id="rgis-1069",
            amendment_number="1069",
        )
    )
    store.set_fetch_status(version_id, FetchStatus.NOT_FOUND)

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        stdout = MANIFEST_MARKER + ' {"status": "not_found", "files": [], "notes": "x"}'
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=stdout, stderr=""
        )

    monkeypatch.setattr("geodocs.agent.executors.subprocess.run", fake_run)
    from geodocs.agent import AgentTierConfig

    config = AgentTierConfig(
        chain=["kimi"],
        executors={
            "kimi": ExecutorConfig(
                name="kimi",
                type="kimi",
                command="kimi",
                args=["-p"],
                timeout_seconds=60,
                quota_patterns=[],
            )
        },
        retry_attempts=1,
        retry_pause_seconds=0,
    )
    results = run_pending(store, config=config, geodocs_home=home)
    store.close()

    assert [r.status for r in results] == ["not_found"]
    mcp_config = json.loads(
        (home / ".kimi-code" / "mcp.json").read_text(encoding="utf-8")
    )
    env = mcp_config["mcpServers"]["geodocs"]["env"]
    assert env["GEODOCS_HOME"] == str(home)  # не рабочий каталог, а home базы

    # хендлер с окружением из mcp.json видит версию реального хранилища
    ctx = McpContext(
        inbox=Path(env["GEODOCS_AGENT_INBOX"]), home=Path(env["GEODOCS_HOME"])
    )
    found = check_local_store_impl(ctx, "pzz", "1069", "2026-06-08")
    assert found["known"] is True
    assert found["versions"][0]["fetch_status"] == "not_found"


def test_kimi_executor_mcp_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIMI_HOME", str(tmp_path / "kimi-home"))
    workdir = tmp_path / "home"
    inbox = workdir / "inbox" / "pzz_1"
    workdir.mkdir(parents=True)

    argv_seen: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        argv_seen.append(argv)
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout="ok", stderr=""
        )

    monkeypatch.setattr("geodocs.agent.executors.subprocess.run", fake_run)
    executor = KimiExecutor(_kimi_cfg(mcp=False))
    executor.run(_prompt(inbox), workdir)

    assert not (workdir / ".kimi-code" / "mcp.json").exists()
    assert "--skills-dir" in argv_seen[0]  # скилы независимы от mcp


def test_kimi_executor_extra_skills_dirs_from_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KIMI_HOME", str(tmp_path / "kimi-home"))
    extra = tmp_path / "extra-skills"
    extra.mkdir()
    workdir = tmp_path / "home"
    workdir.mkdir(parents=True)

    argv_seen: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        argv_seen.append(argv)
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout="ok", stderr=""
        )

    monkeypatch.setattr("geodocs.agent.executors.subprocess.run", fake_run)
    executor = KimiExecutor(_kimi_cfg(skills_dirs=[str(extra)], mcp=False))
    executor.run(_prompt(workdir / "inbox" / "pzz_1"), workdir)

    argv = argv_seen[0]
    assert argv.count("--skills-dir") == 2
    assert str(extra) in argv


def test_config_parses_skills_dirs_and_mcp(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from geodocs.agent import load_config

    (home / "agents.yaml").write_text(
        yaml.dump(
            {
                "chain": ["kimi"],
                "executors": {
                    "kimi": {
                        "type": "kimi",
                        "skills_dirs": ["/opt/skills"],
                        "mcp": {"enabled": False},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("GEODOCS_AGENTS_CONFIG", raising=False)
    cfg = load_config(home)
    assert cfg.executors["kimi"].skills_dirs == ["/opt/skills"]
    assert cfg.executors["kimi"].mcp is False


def test_config_rejects_bad_skills_dirs(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from geodocs.agent import AgentConfigError, load_config

    (home / "agents.yaml").write_text(
        "chain: [kimi]\nexecutors:\n  kimi:\n    type: kimi\n    skills_dirs: not-a-list\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("GEODOCS_AGENTS_CONFIG", raising=False)
    with pytest.raises(AgentConfigError, match="skills_dirs"):
        load_config(home)


# ---------------------------------------------------------------------------
# Промпт: порядок инструментов и финальный MANIFEST
# ---------------------------------------------------------------------------


def _task() -> AgentTask:
    return AgentTask(
        version_id=1,
        doc_type="pzz",
        number="1069",
        municipality="Муниципальный округ Шатура",
        title="Правила землепользования и застройки",
        issuer="Администрация",
        version_date="2026-06-08",
    )


def test_prompt_orders_tools_before_web_search() -> None:
    prompt = build_prompt(_task(), "/tmp/inbox/pzz_1069")
    order = [
        prompt.index("check_local_store"),
        prompt.index("search_document"),
        prompt.index("download_document"),
        prompt.index("fetch_page"),
        prompt.index("ВЕБ-ПОИСК"),
    ]
    assert order == sorted(order), "инструменты должны идти раньше веб-поиска"
    assert "=== MANIFEST ===" in prompt
    assert prompt.rstrip().endswith("}")
