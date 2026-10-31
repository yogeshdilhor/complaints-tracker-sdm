# Seeds the standard concerned departments, and the villages and MC wards read from the
# sub-division's own village map, for every office that already exists. Existing village rows
# (several already entered, some in Hindi, from earlier legacy imports) are left untouched;
# only names not already present (case-insensitively) are added, so nothing already tied to a
# real complaint is renamed or duplicated. Tehsil is only set where the map leaves no doubt
# (Dabwali town and Goriwala itself); every other new village is added with tehsil blank for
# the office to confirm.

from django.db import migrations

CONCERNED_DEPARTMENTS = [
    "MICADA (Micro Irrigation)",
    "Police Department",
    "CRID",
    "DHBVN",
    "BDPO",
    "EO MC",
    "Irrigation",
    "Agriculture",
    "SDM Office",
    "Revenue",
]

# Read off "Sub-Division Dabwali" village map (District Sirsa, Haryana). Tehsil is left blank
# except where the map itself names the tehsil headquarters.
VILLAGES = [
    ("Paniwala Morika", ""),
    ("Jogewala", ""),
    ("Desu Jodha", ""),
    ("Chatha", ""),
    ("Dabwali Village", "Dabwali"),
    ("Dabwali (Rural)", "Dabwali"),
    ("Jottanwali", ""),
    ("Phullo", ""),
    ("Tigri", ""),
    ("Naurang", ""),
    ("Lohgarh", ""),
    ("Sakta Khera", ""),
    ("Shergarh", ""),
    ("Alikan", ""),
    ("Mangiana", ""),
    ("Habuana", ""),
    ("Abubshahar", ""),
    ("New Rajpura", ""),
    ("Gobind Garh", ""),
    ("Masitan", ""),
    ("Siwani Khera", ""),
    ("Nilanwali", ""),
    ("Paniwala Ruldu", ""),
    ("Teja Khera", ""),
    ("Lakhuana", ""),
    ("Maujgarh", ""),
    ("Diwankhera", ""),
    ("Khuyian Malkana", ""),
    ("Mithri", ""),
    ("Asakhera", ""),
    ("Sukhera Khera", ""),
    ("Giddar Khera", ""),
    ("Matdadu", ""),
    ("Lambi", ""),
    ("Jhuti Khera", ""),
    ("Kingra", ""),
    ("Chautala", ""),
    ("Jandwala Bishnoiyan", ""),
    ("Ganga", ""),
    ("Modi", ""),
    ("Goriwala", "Goriwala"),
    ("Malikpura", ""),
    ("Rampura Bishnoiyan", ""),
    ("Bharukhera", ""),
    ("Munnawali", ""),
    ("Chakjalu", ""),
    ("Ratta Majra", ""),
    ("Godikan", ""),
    ("Ahamadpur", ""),
    ("Chak Faridpur", ""),
    ("Risalia Khera", ""),
    ("Rajpura", ""),
    ("Ramgarh", ""),
    ("Kaluana", ""),
    ("Bijuwali", ""),
    ("Banwala", ""),
]

MC_WARD_COUNT = 21


def seed(apps, schema_editor):
    Office = apps.get_model("core", "Office")
    ConcernedDepartment = apps.get_model("core", "ConcernedDepartment")
    Village = apps.get_model("core", "Village")

    for office in Office.objects.all():
        for name in CONCERNED_DEPARTMENTS:
            ConcernedDepartment.objects.get_or_create(office=office, name=name)

        existing = {
            v.lower() for v in Village.objects.filter(office=office).values_list("name", flat=True)
        }
        for name, tehsil in VILLAGES:
            if name.lower() not in existing:
                Village.objects.create(office=office, name=name, tehsil=tehsil)

        for number in range(1, MC_WARD_COUNT + 1):
            name = f"Ward {number}"
            if name.lower() not in existing:
                Village.objects.create(
                    office=office,
                    name=name,
                    block="Municipal Committee Dabwali",
                    is_mc_ward=True,
                )


def unseed(apps, schema_editor):
    """Removes only the MC wards this migration added (unambiguous: is_mc_ward is new and
    nothing else sets it). The added villages and concerned departments are left in place,
    since by the time anyone reverses this they may already be used by a real complaint."""
    Village = apps.get_model("core", "Village")
    Village.objects.filter(is_mc_ward=True, name__startswith="Ward ").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0004_concerned_department_and_wards"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
