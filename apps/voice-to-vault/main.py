"""Transcribe a voice memo, extract tasks and decisions, append to the daily note."""
from datetime import datetime

PROMPT = """Here is a voice memo transcript. Write a short markdown section with:
- a one-line summary as the first line
- "Decisions:" as a bullet list (omit if none)
- "Tasks:" as a "- [ ]" checklist (omit if none)

Transcript:
{text}"""


def run(ctx, audio, day):
    text = ctx.ai("transcribe", file=audio)
    notes = ctx.ai("reason", PROMPT.format(text=text)).strip()
    section = f"## Voice memo {datetime.now():%H:%M}\n{notes}\n"
    summary = notes.splitlines()[0] if notes else "Saved."
    return {"note": section, "summary": summary}
