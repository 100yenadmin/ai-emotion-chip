"""Assemble local templates using capabilities from the dialect tables."""
import re

OPTIONAL = re.compile(r"<!-- optional -->(.*?)<!-- (?:/optional|endoptional) -->", re.S)


def _assemble(template, dialect, model, data, overlay):
    table = data.dialects[dialect]
    licence = data.guide["licence"]
    model_note = data.models.get(model, {}).get("note", "")
    if not isinstance(model_note, str):
        raise ValueError(f"model note {model}: must be a mapping with a string note")
    # The agent writes score forms only; vendor syntax stays inside the renderer.
    forms = ["- `[word]` or `[word, word]` before a sentence sets its delivery; add `slightly` or `very` for intensity.",
             "- `[[register: word; word]]` at the very start sets the whole reply's register. It is never spoken."]
    spans = [f"`[{op}: words]`" for op, value in table["spans"].items() if value]
    if spans:
        forms.append("- Inside one sentence: " + " ".join(spans) + ".")
    if table.get("speaker"):
        forms.append("- `[speaker: N]` switches voice, only when speakers are enabled.")
    pauses = [f"[{e}]" for e, v in table["events"].items() if v and data.sounds["events"][e]["kind"] == "structural"]
    affect = [f"[{e}]" for e, v in table["events"].items() if v and data.sounds["events"][e]["kind"] == "affect"]
    sounds = " ".join(affect) + ("\nPauses and breaths: " + " ".join(pauses) if pauses else "")
    replacements = {"LICENCE": licence, "GRAMMAR": "\n".join(forms),
                    "DIALECT_NOTES": table.get("notes", ""), "MODEL_NOTE": model_note,
                    "SOUNDS": sounds}
    if overlay:
        replacements["DIALECT_NOTES"] += "\n" + overlay.get("note", "")
    for key, value in replacements.items():
        template = template.replace("{{" + key + "}}", value)
    return template


def build_prompt_section(dialect, model, data, overlay=None):
    template = data.guide["section"]
    result = _assemble(template, dialect, model, data, overlay)
    while True:
        visible = OPTIONAL.sub(lambda m: m[1], result).strip()
        if len(visible) <= 3800:
            return visible
        blocks = list(OPTIONAL.finditer(result))
        if not blocks:
            raise ValueError("prompt section exceeds 3800 characters after optional blocks removed")
        last = blocks[-1]
        result = result[:last.start()] + result[last.end():]


def build_skill(dialect, model, data, overlay=None):
    template = data.guide["skill"]
    return OPTIONAL.sub(lambda m: m[1], _assemble(template, dialect, model, data, overlay)).strip()
