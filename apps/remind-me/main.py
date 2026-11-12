def run(ctx, text, day):
    return {
        "reply": f"Reminder for {day:%A %d %B}: {text}",
        "_actions": [{"type": "reminder.create", "title": text, "due": day}],
    }
