"""Style registry — persistent per-book CSS style definitions."""

from __future__ import annotations

from pydantic import BaseModel

ALLOWED_ROLES = {
    "body",
    "epigraph",
    "blockquote",
    "poem",
    "caption",
    "attribution",
    "preface_note",
    "dedication",
    "list_item",
    "code",
    "misc_display",
}

_DEFAULT_STYLES: list[dict] = [
    {
        "id": "epigraph",
        "parent_role": "epigraph",
        "description": "Chapter opening epigraph",
        "css_class": "epigraph",
        "css_rules": {"font-style": "italic", "margin": "1em 3em", "text-indent": "0"},
    },
    {
        "id": "blockquote",
        "parent_role": "blockquote",
        "description": "Indented quotation block",
        "css_class": "blockquote",
        "css_rules": {"margin": "1em 2em", "text-indent": "0"},
    },
    {
        "id": "poem",
        "parent_role": "poem",
        "description": "Verse / poetry with preserved line breaks",
        "css_class": "poem",
        "css_rules": {
            "white-space": "pre-wrap",
            "text-indent": "0",
            "text-align": "center",
            "margin": "1em 0",
        },
    },
    {
        "id": "caption",
        "parent_role": "caption",
        "description": "Figure or table caption text",
        "css_class": "caption",
        "css_rules": {"font-size": "0.88em", "color": "#555", "text-indent": "0"},
    },
    {
        "id": "attribution",
        "parent_role": "attribution",
        "description": "Author attribution after epigraph or blockquote",
        "css_class": "attribution",
        "css_rules": {"text-align": "right", "font-style": "italic", "text-indent": "0"},
    },
    {
        "id": "dedication",
        "parent_role": "dedication",
        "description": "Book dedication text",
        "css_class": "dedication",
        "css_rules": {"text-align": "center", "font-style": "italic", "margin": "2em 0"},
    },
    {
        "id": "preface_note",
        "parent_role": "preface_note",
        "description": "Short note or caveat in preface",
        "css_class": "preface-note",
        "css_rules": {"font-size": "0.9em", "margin": "0.5em 1em", "text-indent": "0"},
    },
]


class StyleDefinition(BaseModel):
    id: str
    parent_role: str
    description: str
    css_class: str
    css_rules: dict[str, str] = {}
    exemplar_block_ids: list[str] = []
    confidence: float = 1.0


class StyleRegistry(BaseModel):
    styles: list[StyleDefinition] = []
    book: str = ""


def seed_defaults(registry: StyleRegistry) -> None:
    """Add default styles if registry is empty."""
    if registry.styles:
        return
    for d in _DEFAULT_STYLES:
        registry.styles.append(StyleDefinition(**d))
