from html import escape

import markdown
from django import template
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter
def conversation_markdown(value: object) -> str:
    """Render agent Markdown after escaping every possible HTML fragment."""
    safe_source = escape(str(value or ""))
    rendered = markdown.markdown(
        safe_source,
        extensions=["nl2br", "sane_lists"],
        output_format="html5",
    )
    return mark_safe(rendered)

