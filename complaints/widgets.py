"""An iPhone-alarm style scroll wheel: spin to pick an option, or choose "Write my own"."""

from __future__ import annotations

import json
from typing import Any

from django import forms
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

OWN = "Write my own..."
START_ON_OWN = "__own__"  # pass as `selected` to open the wheel on "Write my own"


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

    def value_from_datadict(self, data, files, name):
        return data.get(name, "")

    def render(self, name, value, attrs=None, renderer=None):
        chosen = str(value if value not in (None, "") else (self.selected or ""))
        items = [(str(v), label, False) for v, label in self.choices]
        if self.allow_own:
            items.append(("", OWN, True))
        start = next((i for i, (v, _, own) in enumerate(items) if not own and v == chosen), 0)
        if chosen == START_ON_OWN and self.allow_own:
            start = len(items) - 1
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
                'maxlength="200" autocomplete="off" hidden>',
                name,
                self.own_placeholder,
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
