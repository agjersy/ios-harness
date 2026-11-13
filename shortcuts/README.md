# Building the Shortcuts

Shortcuts can't be stored in a repo as text, so this file is the source of
truth: each Shortcut below is listed action by action. Build them once per
device. `python -m harness shortcuts` lists the ones your installed apps need.

These steps follow protocol **2** (see "Shortcuts protocol" in the main
README). They were written from the protocol and have not yet been run on a
device; if an action behaves differently on your iOS version, the protocol
section is what has to hold.

Two values are used throughout. Set them once at the top of Harness Run:

- `Repo`: the absolute path of the repo folder in a-Shell.
- `Protocol`: the number `2`.

## Harness Run

Input: a Dictionary with any of `app`, `utterance`, `inputs`, `id`.
Called by the wrapper Shortcuts, never run directly.

1. **Get Dictionary from** Shortcut Input → `Request`.
2. **If** `Request.id` has no value: **Set Dictionary Value** `id` to
   Current Date formatted as `yyyyMMdd-HHmmss` plus a **Random Number**
   (ids may contain letters, digits, `-`, `_`, `.` only).
3. **Repeat** 12 times (the loop; 12 is a safety limit):
   1. **Put File** (a-Shell): contents `Request` (the Dictionary itself, so
      Shortcuts writes valid JSON), path `~/Documents/.harness/inbox/<id>.json`,
      overwrite on.
   2. **Execute Command** (a-Shell):
      `cd <Repo>` then `python -m harness run --request <id> 2>&1`.
   3. **Get Dictionary from** the command output → `Response`. If this
      fails, **Show Alert** with the raw output and **Stop**.
   4. **If** `Response.protocol` is not `Protocol`: **Show Alert** "The
      harness speaks protocol `Response.protocol`; rebuild Harness Run from
      shortcuts/README.md" and **Stop**.
   5. Branch on `Response.status` with nested **If** actions:

      **`ok`**
      - If `show` has a value: **Show Result** `show`.
      - If `speak` has a value: **Speak Text** `speak`.
      - If `notification` has a value: **Show Notification** `notification`.
      - If `clipboard` has a value: **Copy to Clipboard** `clipboard`.
      - If `open_url` has a value: if `open_url_confirm` is true, **Ask**
        "Open `open_url`?" (Choose from Menu: Open / Skip); then **Open URLs**.
      - **Repeat with each** item in `actions`:
        - If item `confirm` is true: **Choose from Menu** "`summary`. Go
          ahead?" with Yes / No. On No: optionally Execute Command
          `python -m harness ack <item.id> skipped`, then continue with the
          next item.
        - **Run Shortcut** named item `shortcut`, input: the item.
        - Optional: Execute Command `python -m harness ack <item.id> done`.
      - **Stop and Output** `Response`.

      **`ask`**
      - **Set variable** `Inputs` to `Response.inputs` (always start from
        the inputs in the response: they carry what was already collected).
      - **Repeat with each** item in `Response.ask`:
        - If item `error` has a value: **Show Alert** `error`.
        - Collect by item `type`:
          - `text`, `url`, `json`: **Ask for Input** (Text), prompt `prompt`,
            default `default`.
          - `dictation`: **Dictate Text** (or Ask for Input when not hands-free).
          - `choice`: **Choose from List** item `options`, prompt `prompt`.
          - `date`: **Ask for Input** (Date); `datetime`: Ask for Input
            (Date and Time). No formatting needed: the harness reads the
            text Shortcuts produces.
          - `number`: **Ask for Input** (Number).
          - `bool`: **Choose from Menu** Yes / No → text `yes` / `no`.
          - `file`: **Select File**, then Put File into
            `~/Documents/.harness/inbox/<id>/<name>`; the answer is the file name.
        - **Set Dictionary Value** item `name` to the answer in `Inputs`. If
          the person cancels or gives nothing, set it to empty text.
      - **Set Dictionary Value** in `Request`: `inputs` = `Inputs`,
        `app` = `Response.app`. The loop then resends.

      **`choose`**
      - **Choose from List** the keys of `Response.menu`.
      - **Get Dictionary Value** for the picked key → app id.
      - **Set Dictionary Value** in `Request`: `app` = that id (keep
        `utterance` as it was). The loop then resends.

      **`continue`**
      - Optional: **Show Notification** `Response.show`.
      - **Set Dictionary Value** in `Request`: `inputs` = `Response.inputs`,
        `app` = `Response.app`. The loop then resends.

      **`error`**
      - If `Response.repairable` is true: **Choose from Menu**
        "`error`" with Try a repair / Close. On repair: set `Request` to
        `{"app": "repair", "inputs": {"target": Response.app}}` with a new
        id, and let the loop resend.
      - Otherwise **Show Alert** `error` and **Stop**.

      If `Response.warning` has a value, show it as a notification in any branch.

## Harness Attach

Accepts files from the share sheet.

1. **Set variable** `Id` as in Harness Run step 2.
2. **Repeat with each** Shortcut Input item: **Put File** (a-Shell) into
   `~/Documents/.harness/inbox/<Id>/<item name>`.
3. **Run Shortcut** Harness Run with Dictionary `{ "id": Id }` (add
   `utterance` from Dictate Text if you want routing by voice).

## Wrappers

One small Shortcut per thing you say to Siri. Each builds a Dictionary and
runs Harness Run:

- By voice, any app: `{ "utterance": <Dictate Text> }`.
- One fixed app: `{ "app": "remind-me", "inputs": { "text": <Dictate Text> } }`.
- The menu: `{}`.
- Build an app: `{ "app": "forge" }`.
- Revise an app: `{ "app": "forge", "inputs": { "target": "<app id>" } }`.
- Repair an app: `{ "app": "repair" }` (asks which app crashed).

## Harness Act &lt;type&gt;

One Shortcut per action type you use. Each receives one action as a
Dictionary. Read fields with **Get Dictionary Value**; optional fields may
be missing.

| Shortcut | Actions |
|---|---|
| `Harness Act reminder.create` | **Add New Reminder** `title`; list `list`; notes `notes`; alert at `due` if present |
| `Harness Act event.create` | **Add New Event** `title`, start `start`, end `end` (default: start + 1 hour), all-day `all_day`, location `location`, notes `notes`, calendar `calendar` |
| `Harness Act note.create` | **Create Note** with `title` and `body` in folder `folder` |
| `Harness Act message.draft` | **Send Message** `body` to `to` with "Show When Run" on, so it opens as a draft |
| `Harness Act mail.draft` | **Send Email** to `to`, subject `subject`, body `body`, "Show Compose Sheet" on |
| `Harness Act shortcut.run` | **Run Shortcut** `name` with input `input` |

`due`, `start` and `end` arrive as `yyyy-MM-dd` or ISO 8601 text; pass them
through **Get Dates from Input** before using them.

### If Run Shortcut can't take a name from a variable

Set `single_shortcut = true` under `[actions]` in `config.toml`. Every
action's `shortcut` is then `Harness Act`. Build one Shortcut with that
name: **Get Dictionary Value** `type` from the input, then one **If** per
type containing the actions from the table above. Harness Run does not change.
