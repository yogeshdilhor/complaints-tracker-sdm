"""The dashboard, the Excel table download, and the pieces around them."""

import io
import zipfile
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from complaints.models import Document, Platform
from complaints.permissions import visible_complaints
from complaints.services import xlsx
from complaints.services.analytics import build_dashboard
from complaints.services.export import build_workbook, export_queryset
from complaints.tests.helpers import fake_attachment
from complaints.workflow import Action, apply_transition, attach_document
from core.models import Department


@pytest.fixture
def busy(make_complaint, clerk, reviewer, tehsildar, bdpo):
    """Three complaints in different places, so there is something to count."""
    shivir = make_complaint(source_platform=Platform.SAMADHAN_SHIVIR, subject="Road encroachment")
    apply_transition(shivir, Action.ISSUE_NOTICE, clerk, department=bdpo)
    report = attach_document(
        shivir, clerk, fake_attachment("bdpo-report"), Document.Kind.INQUIRY_REPORT
    )
    Document.objects.filter(pk=report.pk).update(summary="BDPO says the road was cleared.")
    apply_transition(shivir, Action.RECORD_REPORT, clerk, document=report)

    cm = make_complaint(source_platform=Platform.CM_WINDOW, subject="Pension")
    apply_transition(cm, Action.ISSUE_NOTICE, clerk, department=tehsildar)

    new = make_complaint(source_platform=Platform.CM_WINDOW, subject="Streetlight")
    return shivir, cm, new


# ------------------------------------------------------------- the .xlsx writer


def test_the_workbook_is_a_valid_zip_with_one_part_per_sheet():
    data = xlsx.write_workbook(
        [
            xlsx.Sheet("Complaints", ["Ref", "Days"], [["C-1", 5], ["C-2", ""]], [10, 8]),
            xlsx.Sheet("Complaints", ["A"], []),  # a clashing name must not break the file
        ]
    )
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert "xl/worksheets/sheet1.xml" in names and "xl/worksheets/sheet2.xml" in names
    workbook = zipfile.ZipFile(io.BytesIO(data)).read("xl/workbook.xml").decode()
    assert 'name="Complaints"' in workbook and 'name="Complaints 2"' in workbook


def test_text_is_escaped_and_control_characters_removed():
    data = xlsx.write_workbook([xlsx.Sheet("S", ["h"], [['A & <B> "q"\x00\x0b']])])
    sheet = zipfile.ZipFile(io.BytesIO(data)).read("xl/worksheets/sheet1.xml").decode()
    assert "A &amp; &lt;B&gt;" in sheet and "\x00" not in sheet and "\x0b" not in sheet


@pytest.mark.parametrize(
    "index, letters", [(0, "A"), (25, "Z"), (26, "AA"), (701, "ZZ"), (702, "AAA")]
)
def test_column_letters(index, letters):
    assert xlsx._column(index) == letters


# ------------------------------------------------------------------ the export


def test_export_filters_by_department_and_portal(clerk, busy, bdpo, tehsildar):
    shivir, cm, new = busy
    visible = visible_complaints(clerk)
    assert set(export_queryset(visible)) == {shivir, cm, new}
    assert set(export_queryset(visible, department=bdpo)) == {shivir}
    assert set(export_queryset(visible, department=tehsildar)) == {cm}
    assert set(export_queryset(visible, platform=Platform.SAMADHAN_SHIVIR)) == {shivir}
    assert set(export_queryset(visible, department=bdpo, platform=Platform.CM_WINDOW)) == set()
    assert set(export_queryset(visible, status="closed")) == set()
    assert set(export_queryset(visible, status="open")) == {shivir, cm, new}


def test_the_workbook_has_the_complainant_the_report_summary_and_the_stages(clerk, busy):
    shivir, *_ = busy
    data = build_workbook(
        export_queryset(visible_complaints(clerk), platform=Platform.SAMADHAN_SHIVIR)
    )
    z = zipfile.ZipFile(io.BytesIO(data))
    sheets = {n: z.read(n).decode() for n in z.namelist() if n.startswith("xl/worksheets/")}
    everything = " ".join(sheets.values())
    assert shivir.ref_no in everything and shivir.complainant.name in everything
    assert "BDPO says the road was cleared." in everything  # report summary
    assert "BDPO report filed" in everything and "Marked to BDPO for report" in everything
    assert "Road encroachment" in everything


def test_downloading_gives_an_excel_file(client, clerk, busy, bdpo):
    client.force_login(clerk)
    response = client.get(reverse("complaints:export_download"), {"department": bdpo.pk})
    assert response.status_code == 200
    assert response["Content-Type"].endswith("spreadsheetml.sheet")
    assert ".xlsx" in response["Content-Disposition"]
    assert zipfile.is_zipfile(io.BytesIO(response.content))


def test_the_download_page_shows_how_many_match(client, clerk, busy, bdpo):
    client.force_login(clerk)
    html = client.get(reverse("complaints:export"), {"department": bdpo.pk}).content.decode()
    assert "Table download" in html and "<b>1</b> complaint matches" in html
    html = client.get(reverse("complaints:export")).content.decode()
    assert "<b>3</b> complaints match" in html


def test_officers_cannot_download_or_see_the_dashboard(client, officer, busy):
    client.force_login(officer)
    for name in ("complaints:export", "complaints:export_download", "complaints:dashboard"):
        assert client.get(reverse(name)).status_code == 403
    html = client.get(reverse("complaints:list")).content.decode()
    assert "Table download" not in html and "Dashboard" not in html


def test_the_nav_no_longer_has_a_complaints_tab(client, clerk):
    client.force_login(clerk)
    html = client.get(reverse("complaints:list")).content.decode()
    nav = html.split("<nav>")[1].split("</nav>")[0]
    assert ">Complaints<" not in nav
    assert "Dashboard" in nav and "Table download" in nav and "Overdue today" in nav


# ---------------------------------------------------------------- the dashboard


def test_dashboard_numbers(clerk, busy, bdpo):
    d = build_dashboard(visible_complaints(clerk), "all")
    assert (d["total"], d["open"], d["closed"]) == (3, 3, 0)
    assert dict(zip(d["by_platform"]["labels"], d["by_platform"]["values"], strict=True)) == {
        "CM Window": 2,
        "Samadhan Shivir": 1,
    }
    assert (
        dict(zip(d["by_department"]["labels"], d["by_department"]["values"], strict=True))["BDPO"]
        == 1
    )
    perf = {p["name"]: p for p in d["dept_perf"]}
    assert perf["BDPO"]["answered"] == 1 and perf["BDPO"]["on_time_pct"] == 100
    assert perf["Tehsildar"]["waiting"] == 1 and perf["Tehsildar"]["answered"] == 0
    assert sum(d["weekly"]["received"]) == 3
    assert len(d["oldest"]) == 3


def test_dashboard_counts_late_reports(clerk, make_complaint, tehsildar):
    c = make_complaint()
    apply_transition(c, Action.ISSUE_NOTICE, clerk, department=tehsildar)
    c.referrals.update(due_on=timezone.localdate() - timedelta(days=3))
    d = build_dashboard(visible_complaints(clerk), "all")
    assert d["overdue_reports"] == 1
    assert {p["name"]: p["late"] for p in d["dept_perf"]}["Tehsildar"] == 1


def test_dashboard_period_filter(clerk, make_complaint):
    old = make_complaint()
    old.received_on = timezone.localdate() - timedelta(days=200)
    old.save()
    make_complaint()
    assert build_dashboard(visible_complaints(clerk), "30")["total"] == 1
    assert build_dashboard(visible_complaints(clerk), "365")["total"] == 2
    assert build_dashboard(visible_complaints(clerk), "nonsense")["range_key"] == "90"


def test_empty_dashboard_does_not_break(clerk, office):
    d = build_dashboard(visible_complaints(clerk), "all")
    assert d["total"] == 0 and d["avg_close_days"] is None and d["on_time_pct"] is None
    assert d["oldest"] == [] and d["by_department"]["labels"] == []


def test_dashboard_page_renders_with_data_for_the_charts(client, clerk, busy):
    client.force_login(clerk)
    html = client.get(reverse("complaints:dashboard"), {"range": "all"}).content.decode()
    assert "Dashboard" in html and 'id="chart-data"' in html and "chart.umd.min.js" in html
    assert "Department report performance" in html
    assert Department.objects.filter(short_name="BDPO").exists() and "BDPO" in html


def test_the_dashboard_is_scoped_to_what_the_user_may_see(auditor, busy):
    assert build_dashboard(visible_complaints(auditor), "all")["total"] == 3


def test_the_splitter_is_on_the_list_page(client, clerk):
    client.force_login(clerk)
    html = client.get(reverse("complaints:list")).content.decode()
    assert 'id="splitter"' in html and "splitter.js" in html
