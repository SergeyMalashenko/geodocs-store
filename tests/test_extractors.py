"""Тесты детерминированных экстракторов таблиц ВРИ."""

from __future__ import annotations

import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from geodocs import (
    DocRole,
    DocType,
    DocumentRef,
    DocumentStore,
    ExtractionKind,
    SourceName,
    VriExtractor,
    demap,
    extract_html,
    extract_vri_from_file,
    looks_like_mojibake,
)
from geodocs.extractors import pdf_layout

# Реальные фрагменты из Можайского регламента (RGIS, объект 12805587090,
# редакция до переиздания 2026-09-30): часть страниц набрана шрифтом
# с уехавшей картой символов — кириллица в U+0230-U+02AF (демап +0x1D6),
# \x03 - пробел, \x05 - кавычка, \x11 - точка. Литералы только в \uXXXX,
# чтобы исключить путаницу внешне похожих IPA-глифов.
MOJIBAKE_FRAGMENT_A = "\u024d\u026b\u0265\u0268\u025c\u0267\u0268\u0003\u026a\u025a\u0261\u026a\u025f\u0272\u025f\u0267\u0267\u0275\u025f\u0003\u025c\u0262\u025e\u0275\u0003\u0262\u026b\u0269\u0268\u0265\u0276\u0261\u0268\u025c\u025a\u0267\u0262\u0279"
MOJIBAKE_DECODED_A = "Условно разрешенные виды использования"
MOJIBAKE_FRAGMENT_B = "\u0262\u0003\u026b\u026c\u026a\u0268\u0262\u026c\u025f\u0265\u0276\u026b\u026c\u025c\u026d\u0003\u0267\u025a\u0003\u026c\u025f\u026a\u026a\u0262\u026c\u0268\u026a\u0262\u0262\u0003\u025e\u0268\u026b\u026c\u0268\u0269\u026a\u0262\u0266\u025f\u0271\u025a\u026c\u025f\u0265\u0276\u0267\u0268\u025d\u0268\u0003\u0266\u025f\u026b\u026c\u025a\u0003\u0005\u023b\u0268\u026a\u0268\u025e\u0262\u0267\u026b\u0264\u0268\u025f\u0003\u0269\u0268\u0265\u025f\u0003\u0262\u0003\u0269\u025a\u0266\u0279\u026c\u0267\u0262\u0264\u0262\u0003\u0267\u025a\u0003\u0267\u025f\u0266\u0005\u0011"
MOJIBAKE_DECODED_B = (
    'и строительству на территории достопримечательного места '
    '"Бородинское поле и памятники на нем".'
)

SYNTHETIC_DOC_XML = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:body>
<w:p><w:r><w:t>Ж-2 - ЗОНА ЗАСТРОЙКИ ИНДИВИДУАЛЬНЫМИ ЖИЛЫМИ ДОМАМИ</w:t></w:r></w:p>
<w:p><w:r><w:t>Зона застройки индивидуальными жилыми домами Ж-2 установлена для теста.</w:t></w:r></w:p>
<w:p><w:r><w:t>Основные виды разрешенного использования</w:t></w:r></w:p>
<w:tbl>
<w:tr><w:tc><w:p><w:r><w:t>№ п/п</w:t></w:r></w:p></w:tc>
<w:tc><w:p><w:r><w:t>Наименование ВРИ</w:t></w:r></w:p></w:tc>
<w:tc><w:p><w:r><w:t>Код</w:t></w:r></w:p></w:tc>
<w:tc><w:p><w:r><w:t>min</w:t></w:r></w:p></w:tc>
<w:tc><w:p><w:r><w:t>max</w:t></w:r></w:p></w:tc>
<w:tc><w:p><w:r><w:t>%</w:t></w:r></w:p></w:tc>
<w:tc><w:p><w:r><w:t>отступ</w:t></w:r></w:p></w:tc></w:tr>
<w:tr><w:tc><w:p><w:r><w:t>1</w:t></w:r></w:p></w:tc>
<w:tc><w:p><w:r><w:t>Для индивидуального жилищного строительства</w:t></w:r></w:p></w:tc>
<w:tc><w:p><w:r><w:t>2.1*</w:t></w:r></w:p></w:tc>
<w:tc><w:p><w:r><w:t>500</w:t></w:r></w:p></w:tc>
<w:tc><w:p><w:r><w:t>5 000 000</w:t></w:r></w:p></w:tc>
<w:tc><w:p><w:r><w:t>40%</w:t></w:r></w:p></w:tc>
<w:tc><w:p><w:r><w:t>3</w:t></w:r></w:p></w:tc></w:tr>
<w:tr><w:tc><w:p><w:r><w:t>2</w:t></w:r></w:p></w:tc>
<w:tc><w:p><w:r><w:t>Связь</w:t></w:r></w:p></w:tc>
<w:tc><w:p><w:r><w:t>6.8</w:t></w:r></w:p></w:tc>
<w:tc><w:p><w:r><w:t>Не подлежат установлению</w:t></w:r></w:p></w:tc></w:tr>
</w:tbl>
</w:body>
</w:document>
"""

LAYOUT_FIXTURE = """\
ЧАСТЬ III. ГРАДОСТРОИТЕЛЬНЫЕ РЕГЛАМЕНТЫ
                    Ж-2 - ЗОНА ЗАСТРОЙКИ ИНДИВИДУАЛЬНЫМИ ЖИЛЫМИ ДОМАМИ
     Зона застройки индивидуальными жилыми домами Ж-2 установлена для обеспечения формирования жилых районов.
                                                  Основные виды разрешенного использования
                                                     Предельные размеры         Максимальный процент
                                       Код
          Наименование ВРИ                                 (кв. м)                  застройки
 п/п                               обозначение
                                                                                 min            max
         Для индивидуального                                                                                    Не подлежат
  1                                    2.1*          400             3 000                40%                      3
        жилищного строительства                                                                                 установлению
           Для ведения личного
          подсобного хозяйства
  2                                    2.2*          300             3 000                40%                      3
       (приусадебный земельный
              участок)
  3    Коммунальное обслуживание       3.1            30            100 000               75%                      3
  4    Связь                           6.8                            Не подлежат установлению
                    Ж-2Б - СПЕЦИАЛИЗИРОВАННАЯ ЗОНА ЗАСТРОЙКИ ЖИЛЫМИ ДОМАМИ
  5    Не попадет в выдачу             9.9            1                1                      1%                       1
"""


def _write_docx(path: Path, xml: str = SYNTHETIC_DOC_XML) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return path


SYNTHETIC_HTML = """\
<html>
<body>
<p>Вводная часть документа, не относящаяся к зонам.</p>
<h6>Ж-2 - зона застройки индивидуальными жилыми домами</h6>
<p>Зона застройки индивидуальными жилыми домами Ж-2 установлена для теста.</p>
<table class="wideTable">
<tr height="1"><td></td><td></td><td></td><td></td><td></td><td></td><td></td></tr>
<tr>
<td><p>N п/п</p></td>
<td><p>Наименование ВРИ</p></td>
<td><p>Код (числовое обозначение ВРИ)</p></td>
<td colspan="2"><p>Предельные размеры земельных участков (кв. м)</p></td>
<td><p>Максимальный процент застройки</p></td>
<td><p>Минимальные отступы от границ земельного участка (м)</p></td>
</tr>
<tr>
<td></td><td></td><td></td>
<td><p>min</p></td>
<td><p>max</p></td>
<td></td><td></td>
</tr>
<tr>
<td><p>1</p></td>
<td><p>Для индивидуального жилищного<br/>строительства</p></td>
<td><p>2.1*</p></td>
<td><p>500</p></td>
<td><p>5 000 000</p></td>
<td><p>40%</p></td>
<td><p>3</p></td>
</tr>
<tr>
<td><p>2</p></td>
<td><p>Связь</p></td>
<td><p>6.8 &lt;2&gt;</p></td>
<td></td>
<td></td>
<td></td>
<td><p>3</p></td>
</tr>
</table>
<h6>Ж-2Б - специализированная зона застройки жилыми домами</h6>
<table class="wideTable">
<tr>
<td><p>N п/п</p></td>
<td><p>Наименование ВРИ</p></td>
<td><p>Код</p></td>
<td><p>min</p></td>
<td><p>max</p></td>
</tr>
<tr>
<td><p>1</p></td>
<td><p>Не попадет в выдачу</p></td>
<td><p>9.9</p></td>
<td><p>1</p></td>
<td><p>1</p></td>
</tr>
</table>
</body>
</html>
"""


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[DocumentStore]:
    store = DocumentStore(tmp_path / "geodocs.sqlite3")
    yield store
    store.close()


def test_mojibake_demap_real_fragments() -> None:
    assert demap(MOJIBAKE_FRAGMENT_A) == MOJIBAKE_DECODED_A
    assert demap(MOJIBAKE_FRAGMENT_B) == MOJIBAKE_DECODED_B


def test_mojibake_detector() -> None:
    assert looks_like_mojibake(MOJIBAKE_FRAGMENT_A * 3) is True
    assert (
        looks_like_mojibake(
            "Обычный русский текст Правил землепользования и застройки, без сбоев."
            * 4
        )
        is False
    )


def test_extract_docx_synthetic(tmp_path: Path) -> None:
    path = _write_docx(tmp_path / "reglament.docx")

    outcome = extract_vri_from_file(path, filename="reglament.docx", zone_code="Ж-2")

    assert outcome.status == "extracted"
    assert outcome.extractor == "vri-docx@1"
    table = outcome.table
    assert table is not None
    assert table.zone_code == "Ж-2"
    assert table.zone_name == "ЗОНА ЗАСТРОЙКИ ИНДИВИДУАЛЬНЫМИ ЖИЛЫМИ ДОМАМИ"
    assert table.zone_description is not None and "установлена для теста" in table.zone_description
    assert table.counts == {"rows_total": 2, "rows_parsed": 2}
    assert table.confidence == 1.0
    first, second = table.items
    assert first.row == "1"
    assert first.code == "2.1*"
    assert first.name == "Для индивидуального жилищного строительства"
    assert first.area_min == 500
    assert first.area_max == 5000000
    assert first.building_percentage == "40%"
    assert first.margin == 3
    assert second.code == "6.8"
    assert second.area_min is None
    assert "6.8" in second.raw


def test_extract_docx_zone_not_found(tmp_path: Path) -> None:
    path = _write_docx(tmp_path / "reglament.docx")

    outcome = extract_vri_from_file(path, filename="reglament.docx", zone_code="СХ-3")

    assert outcome.status == "no_section"


def test_extract_docx_not_a_zip(tmp_path: Path) -> None:
    path = tmp_path / "broken.docx"
    path.write_bytes(b"not a zip")

    outcome = extract_vri_from_file(path, filename="broken.docx", zone_code="Ж-2")

    assert outcome.status == "error"


def test_pdf_layout_synthetic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pdf_layout, "run_pdftotext", lambda path: LAYOUT_FIXTURE)
    path = tmp_path / "reglament.pdf"

    outcome = extract_vri_from_file(path, filename="reglament.pdf", zone_code="Ж-2")

    assert outcome.status == "extracted"
    assert outcome.extractor == "vri-pdf-layout@1"
    table = outcome.table
    assert table is not None
    assert table.zone_code == "Ж-2"
    assert table.zone_name is not None and table.zone_name.startswith("ЗОНА ЗАСТРОЙКИ")
    assert table.counts == {"rows_total": 4, "rows_parsed": 4}
    assert table.confidence == 0.8
    first = table.items[0]
    assert first.code == "2.1*"
    # наименование собрано из переносов над строкой-ядром
    assert first.name == "Для индивидуального жилищного строительства"
    assert first.area_min == 400
    assert first.area_max == 3000
    assert first.building_percentage == "40%"
    assert first.margin == 3
    second = table.items[1]
    assert second.name == (
        "Для ведения личного подсобного хозяйства (приусадебный земельный участок)"
    )
    fourth = table.items[3]
    assert fourth.code == "6.8"
    assert fourth.area_min is None
    # секция обрезана заголовком следующей зоны Ж-2Б
    assert all(item.code != "9.9" for item in table.items)


def test_pdf_layout_section_locator() -> None:
    span = pdf_layout.locate_section(LAYOUT_FIXTURE, "Ж-2")
    assert span is not None
    section = LAYOUT_FIXTURE[span[0] : span[1]].strip()
    assert section.startswith("Ж-2 - ЗОНА")
    assert "Ж-2Б" not in section
    assert pdf_layout.locate_section(LAYOUT_FIXTURE, "Ж-5") is None
    assert pdf_layout.locate_section(LAYOUT_FIXTURE, "СХ-2") is None


def test_pdf_layout_scan_detector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pdf_layout, "run_pdftotext", lambda path: "12345\n678\n" * 50)
    path = tmp_path / "scan.pdf"

    outcome = extract_vri_from_file(path, filename="scan.pdf", zone_code="Ж-2")

    assert outcome.status == "scan_pdf"


def test_pdf_layout_no_pdftotext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pdf_layout, "_PDFTOTEXT", None)
    path = tmp_path / "reglament.pdf"

    outcome = extract_vri_from_file(path, filename="reglament.pdf", zone_code="Ж-2")

    assert outcome.status == "no_pdftotext"


def test_extract_unsupported_extension(tmp_path: Path) -> None:
    path = tmp_path / "reglament.txt"
    path.write_text("текст")

    outcome = extract_vri_from_file(path, filename="reglament.txt", zone_code="Ж-2")

    assert outcome.status == "error"


def test_extract_html_synthetic(tmp_path: Path) -> None:
    path = tmp_path / "cntd_574725075.html"
    path.write_text(SYNTHETIC_HTML, encoding="utf-8")

    outcome = extract_html(path, "Ж-2")

    assert outcome.status == "extracted"
    assert outcome.extractor == "vri-html@1"
    table = outcome.table
    assert table is not None
    assert table.zone_code == "Ж-2"
    assert table.zone_name == "зона застройки индивидуальными жилыми домами"
    assert table.zone_description is not None and "установлена для теста" in table.zone_description
    assert table.counts == {"rows_total": 2, "rows_parsed": 2, "tables": 1}
    assert table.confidence == 1.0
    first, second = table.items
    assert first.row == "1"
    assert first.code == "2.1*"
    # <br/> внутри ячейки склеен пробелом
    assert first.name == "Для индивидуального жилищного строительства"
    assert first.area_min == 500
    assert first.area_max == 5000000
    assert first.building_percentage == "40%"
    assert first.margin == "3"
    assert second.code == "6.8"
    assert second.area_min is None
    assert "6.8" in second.raw
    # сноска <2> отброшена из кода, но сохранена в raw
    assert "<2>" in second.raw
    # секция обрезана заголовком следующей зоны Ж-2Б
    assert all(item.code != "9.9" for item in table.items)


def test_clean_code_strips_footnotes() -> None:
    from geodocs.extractors.html_tables import _clean_code

    assert _clean_code("2.1*") == "2.1*"
    assert _clean_code("2.1 <1>") == "2.1"
    assert _clean_code("  12.0.1  <3>") == "12.0.1"
    assert _clean_code("Код (числовое обозначение)") is None
    assert _clean_code("текст без кода") is None


def test_extract_html_zone_not_found(tmp_path: Path) -> None:
    path = tmp_path / "cntd_574725075.html"
    path.write_text(SYNTHETIC_HTML, encoding="utf-8")

    outcome = extract_html(path, "СХ-3")

    assert outcome.status == "no_section"


def test_extract_html_via_dispatcher(tmp_path: Path) -> None:
    path = tmp_path / "cntd_574725075.html"
    path.write_text(SYNTHETIC_HTML, encoding="utf-8")

    outcome = extract_vri_from_file(
        path, filename="cntd_574725075.html", zone_code="Ж-2"
    )

    assert outcome.status == "extracted"
    assert outcome.extractor == "vri-html@1"
    assert outcome.table is not None
    assert outcome.table.items[0].code == "2.1*"


def _register_docx_version(store: DocumentStore, tmp_path: Path) -> int:
    ref = DocumentRef(
        municipality="Городской округ Клин",
        doc_type=DocType.PZZ,
        number="592",
        version_date="2026-04-09",
        role=DocRole.AMENDMENT,
        region_code="50",
        source=SourceName.RGIS,
        source_object_id="13881025700",
    )
    version_id = store.register_ref(ref)
    path = _write_docx(tmp_path / "files" / "reglament.docx")
    store.save_file(
        version_id,
        path.read_bytes(),
        filename="reglament.docx",
        title="Регламент Ж-2.docx",
        section="Зональные регламенты",
        source_provider=SourceName.RGIS,
    )
    return version_id


def test_vri_extractor_end_to_end(store: DocumentStore, tmp_path: Path) -> None:
    version_id = _register_docx_version(store, tmp_path)
    extractor = VriExtractor(store)

    outcomes = extractor.extract_version(version_id, ["Ж-2"])

    assert [outcome.status for outcome in outcomes] == ["extracted"]
    record = store.get_extraction(
        version_id, zone_code="Ж-2", kind=ExtractionKind.VRI_TABLE
    )
    assert record is not None
    assert record.extractor == "vri-docx@1"
    assert record.confidence == 1.0
    assert record.payload["counts"] == {"rows_total": 2, "rows_parsed": 2}
    assert record.payload["items"][0]["code"] == "2.1*"

    # force=False — кэш, перезаписи нет
    snapshot = record.model_dump()
    cached = extractor.extract_version(version_id, ["Ж-2"])
    assert cached[0].detail == "already extracted"
    record_after = store.get_extraction(
        version_id, zone_code="Ж-2", kind=ExtractionKind.VRI_TABLE
    )
    assert record_after is not None
    assert record_after.created_at == snapshot["created_at"]

    # force=True — перезапись
    forced = extractor.extract_version(version_id, ["Ж-2"], force=True)
    assert forced[0].detail is None
    record_forced = store.get_extraction(
        version_id, zone_code="Ж-2", kind=ExtractionKind.VRI_TABLE
    )
    assert record_forced is not None
    assert record_forced.payload["items"][0]["code"] == "2.1*"


def test_vri_extractor_no_files(store: DocumentStore, tmp_path: Path) -> None:
    ref = DocumentRef(
        municipality="Городской округ Клин",
        doc_type=DocType.PZZ,
        number="600",
        version_date="2026-04-09",
        role=DocRole.SINGLE,
        region_code="50",
        source=SourceName.RGIS,
        source_object_id="1",
    )
    version_id = store.register_ref(ref)
    extractor = VriExtractor(store)

    outcomes = extractor.extract_version(version_id, ["Ж-2"])

    assert outcomes[0].status == "error"
    assert store.get_extraction(
        version_id, zone_code="Ж-2", kind=ExtractionKind.VRI_TABLE
    ) is None


def _register_html_version(store: DocumentStore, tmp_path: Path) -> int:
    ref = DocumentRef(
        municipality="Городской округ Солнечногорск",
        doc_type=DocType.PZZ,
        number="592",
        version_date="2026-04-09",
        role=DocRole.AMENDMENT,
        region_code="50",
        source=SourceName.CNTD,
        source_object_id="574725075",
    )
    version_id = store.register_ref(ref)
    path = tmp_path / "files" / "cntd_574725075.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SYNTHETIC_HTML, encoding="utf-8")
    store.save_file(
        version_id,
        path.read_bytes(),
        filename="cntd_574725075.html",
        title="CNTD 574725075 полный текст",
        section="cntd",
        source_provider=SourceName.CNTD,
    )
    return version_id


def test_vri_extractor_end_to_end_html(store: DocumentStore, tmp_path: Path) -> None:
    version_id = _register_html_version(store, tmp_path)
    extractor = VriExtractor(store)

    outcomes = extractor.extract_version(version_id, ["Ж-2"])

    assert [outcome.status for outcome in outcomes] == ["extracted"]
    record = store.get_extraction(
        version_id, zone_code="Ж-2", kind=ExtractionKind.VRI_TABLE
    )
    assert record is not None
    assert record.extractor == "vri-html@1"
    assert record.confidence == 1.0
    assert record.payload["counts"] == {"rows_total": 2, "rows_parsed": 2, "tables": 1}
    assert record.payload["items"][0]["code"] == "2.1*"
    assert record.payload["items"][0]["margin"] == "3"
