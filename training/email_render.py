from django.template import Template, Context

def render_email_text(text: str, ctx: dict) -> str:
    """
    Renders Django-style {{ placeholders }} with a limited context.
    Intended for staff-managed templates.
    """
    if not text:
        return ""
    return Template(text).render(Context(ctx)).strip()
