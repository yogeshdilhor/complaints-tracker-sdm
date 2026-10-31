"""An iPhone-alarm style scroll wheel: spin to pick an option, or choose "Write my own"."""

from __future__ import annotations

import json
from typing import Any

from django import forms
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

OWN = "Write my own..."
START_ON_OWN = "__own__"  # pass as `selected` to open the wheel on "Write my own"


class VillageWidget(forms.Widget):
    """A plain <select> of villages, grouped Village / MC area (ward) by a radio pair above it.

    Each <option> carries data-group="village" or "mc"; static/js/cascade.js shows only the
    group the radios ask for. Without JavaScript every option stays visible.
    """

    def __init__(self, villages=(), *, empty_label="Choose...", attrs=None):
        super().__init__(attrs)
        # villages: iterable of objects with pk, name, tehsil, is_mc_ward (a Village queryset).
        self.villages = list(villages)
        self.empty_label = empty_label

    def value_from_datadict(self, data, files, name):
        return data.get(name, "")

    def render(self, name, value, attrs=None, renderer=None):
        chosen = str(value) if value not in (None, "") else ""
        kind_of_chosen = "village"
        options = [format_html('<option value="">{}</option>', self.empty_label)]
        for v in self.villages:
            group = "mc" if v.is_mc_ward else "village"
            label = f"{v.name} ({v.tehsil} tehsil)" if v.tehsil else v.name
            if str(v.pk) == chosen:
                kind_of_chosen = group
            options.append(
                format_html(
                    '<option value="{}" data-group="{}"{}>{}</option>',
                    v.pk,
                    group,
                    mark_safe(" selected") if str(v.pk) == chosen else "",
                    label,
                )
            )
        village_checked = mark_safe(" checked") if kind_of_chosen == "village" else ""
        mc_checked = mark_safe(" checked") if kind_of_chosen == "mc" else ""
        return format_html(
            '<div class="loc-field">'
            '<div class="loc-kind">'
            '<label><input type="radio" name="{0}_kind" value="village"{1}> Village</label>'
            '<label><input type="radio" name="{0}_kind" value="mc"{2}> MC area (ward)</label>'
            "</div>"
            '<select name="{0}" id="id_{0}" data-cascade>{3}</select>'
            "</div>",
            name,
            village_checked,
            mc_checked,
            mark_safe("".join(options)),
        )


class GroupedAddSelect(forms.Widget):
    """A plain <select>, each option optionally tagged data-group="<other field's option
    value>" so static/js/cascade.js can filter it against another select's current choice
    (pass `controlled_by` as that select's id). A final "+ Add new..." choice reveals a text
    input posted as "<name>_own"; complaints.forms turns that text into a new row.
    """

    def __init__(
        self,
        choices=(),
        *,
        groups=None,
        controlled_by: str | None = None,
        empty_label="Choose...",
        own_label="+ Add new...",
        own_placeholder="Type the name",
        attrs=None,
    ):
        super().__init__(attrs)
        self.choices = list(choices)  # [(pk, label), ...]
        self.groups = groups or {}  # {pk: group value}
        self.controlled_by = controlled_by
        self.empty_label = empty_label
        self.own_label = own_label
        self.own_placeholder = own_placeholder

    def value_from_datadict(self, data, files, name):
        return data.get(name, "")

    def render(self, name, value, attrs=None, renderer=None):
        chosen = str(value) if value not in (None, "") else ""
        options = [format_html('<option value="">{}</option>', self.empty_label)]
        for pk, label in self.choices:
            group = self.groups.get(pk, "")
            options.append(
                format_html(
                    '<option value="{}" data-group="{}"{}>{}</option>',
                    pk,
                    group,
                    mark_safe(" selected") if str(pk) == chosen else "",
                    label,
                )
            )
        options.append(format_html('<option value="" data-own="1">{}</option>', self.own_label))
        cascade_of = (
            format_html(' data-cascade-of="id_{}"', self.controlled_by)
            if self.controlled_by
            else ""
        )
        return format_html(
            '<span class="cascade-field">'
            '<select name="{0}" id="id_{0}" data-cascade{1}>{2}</select>'
            '<input type="text" class="cascade-own" name="{0}_own" placeholder="{3}" '
            'maxlength="200" autocomplete="off" hidden>'
            "</span>",
            name,
            cascade_of,
            mark_safe("".join(options)),
            self.own_placeholder,
        )


class WheelWidget(forms.Widget):
    """Posts the chosen value in a hidden input named `name`.

    With `allow_own`, the last item lets the user type their own text, posted as `<name>_own`.
    static/js/wheel.js makes it spin; without JavaScript the first item stays selected.
    """

    def __init__(
        self,
        choices: list[tuple[Any, str]] | None = None,
        *,
        allow_own: bool = True,
        own_placeholder: str = "Type here",
        selected: str | None = None,
        data: dict[str, Any] | None = None,
        attrs: dict | None = None,
    ):
        super().__init__(attrs)
        self.choices = choices or []
        self.allow_own = allow_own
        self.own_placeholder = own_placeholder
        self.selected = selected
        self.data = data or {}

    @staticmethod
    def _known(items, chosen: str) -> bool:
        return any(not own and chosen in (v, label) for v, label, own in items)

    def value_from_datadict(self, data, files, name):
        return data.get(name, "")

    def render(self, name, value, attrs=None, renderer=None):
        chosen = str(value if value not in (None, "") else (self.selected or ""))
        items = [(str(v), label, False) for v, label in self.choices]
        if self.allow_own:
            items.append(("", OWN, True))
        start = next((i for i, (v, _, own) in enumerate(items) if not own and v == chosen), 0)
        own_text = ""
        if self.allow_own and (
            chosen == START_ON_OWN or (chosen and not self._known(items, chosen))
        ):
            start = len(items) - 1
            own_text = "" if chosen == START_ON_OWN else chosen
        lis = format_html_join(
            "",
            '<li role="option" data-value="{}" data-label="{}"{}{}>{}</li>',
            (
                (
                    v,
                    label,
                    mark_safe(" data-own") if own else "",
                    mark_safe(' class="selected"') if i == start else "",
                    label,
                )
                for i, (v, label, own) in enumerate(items)
            ),
        )
        own_input = ""
        if self.allow_own:
            own_input = format_html(
                '<input type="text" class="wheel-own" name="{}_own" placeholder="{}" '
                'maxlength="200" autocomplete="off" value="{}" hidden>',
                name,
                self.own_placeholder,
                own_text,
            )
        return format_html(
            '<div class="wheel-field" data-name="{}" data-start="{}" data-config="{}">'
            '<div class="wheel-box"><div class="wheel-band"></div>'
            '<ul class="wheel" tabindex="0" role="listbox">{}</ul></div>'
            '<input type="hidden" name="{}" value="{}">{}</div>',
            name,
            start,
            json.dumps(self.data),
            lis,
            name,
            items[start][0],
            own_input,
        )
