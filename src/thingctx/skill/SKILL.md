---
name: thingctx-driver
description: Call a real device or service (read a sensor, send a command, fetch data) registered as a W3C WoT Thing. Use thingctx list then thingctx invoke.
---

# Drive any Thing with the thingctx CLI

`thingctx` turns a Thing Description (a device or service interface) into shell
commands. You discover what is available, read the input schema, and invoke an
action. Nothing about a specific app is hardcoded here: the actions and their
schemas come from `thingctx list` at runtime, so a newly added Thing is drivable
with no change to this skill.

Two commands do everything:

- `thingctx list` , what can I call, and what input does each take.
- `thingctx invoke <action> ...` , call one action, get JSON back.

## 1. Discover

```bash
thingctx list
```

Prints a JSON array of entries, each with `name`, `kind`, `description`, and
`input_schema` (a JSON Schema for the arguments). The `name` is slug qualified,
like `thermostat__setTarget` or `camera__snapshot`. Read the `input_schema` to
learn the argument names, types, and which are required; the `description` often
states the output. Form every call from that schema; never guess argument names.

If nothing is configured yet, pass a source explicitly (a directory of
`*.td.json`, a file, an http(s) URL, or a `tdd:` directory):

```bash
thingctx list ./things/
```

To make `list`/`invoke` work with no source, add Things to the default registry
once: `thingctx registry add ./things/` (or a URL / `tdd:` directory). After
that, omit the source everywhere.

## 2. Invoke

Pass arguments as one JSON object, or as repeated scalar flags:

```bash
thingctx invoke thermostat__setTarget --json '{"celsius": 21}'
thingctx invoke thermostat__setTarget --arg celsius=21
```

- The result is JSON on **stdout**. Capture it once, then parse (e.g. with `jq`).
- On success the exit code is 0. On failure it is non-zero and a short error
  JSON goes to **stderr**, so stdout stays clean for capture and pipes.
- `--arg k=v`: a value that is valid JSON is parsed (`count=3` is an int,
  `on=true` is a bool); anything else stays a string. Use `--json` for nested
  objects or arrays.
- `--out FILE` writes the result to a file instead of stdout (use `-` for
  stdout). Bytes (e.g. an image) are written raw.

Chain actions by feeding one result into the next:

```bash
ID=$(thingctx invoke catalog__create --json "$ITEM" | jq -r .id)
thingctx invoke catalog__publish --arg id="$ID"
```

## 3. Trust gate

Some actions are marked risky (destructive or requiring approval). They are
gated:

- Add `--yes` to approve unattended (scripts, cron, CI).
- On an interactive terminal you are asked to confirm.
- With no terminal and no `--yes`, a gated action is denied (it returns an error
  and a non-zero exit), so add `--yes` when you mean it.

Override the policy with `--approve-when declared|destructive|all|never`.

## 4. Output quality

A capability can report a verdict on its own output under a reserved key,
`tc:quality` (`{verdict, score, reason, signals}`), when it can tell its result
is untrustworthy though the call "succeeded" (for example transcription that
hallucinated by repeating one line). If an action's output includes `tc:quality`
with verdict `suspect` or `bad`, do not continue the pipeline (do not publish or
chain the result): show the user the `reason` and ask how to proceed.

## Legible commands

Keep a run easy to follow. The narration of which steps and why for a goal
belongs to the app's recipe; this is just how to make each step read clearly:

- Before each action, write ONE plain sentence saying what this step does and
  why, then run the command.
- Keep the command minimal: prefer `--arg key=value`. For a JSON body, build it
  into a variable or a small file first and pass that; do not inline a big
  `jq -n '{...}'` on the invoke line.
- Capture each result into a variable or `--out FILE` and reference that next,
  instead of threading long temp paths through every line.
- Report results in plain words (an id, a link, a verdict), not a raw JSON dump.
- The command is `thingctx` (expected on PATH). Do not prefix it with a package
  runner or a project path; that adds noise and can fail outside the project dir.

## Gotchas (generic, worth knowing up front)

- **A file or binary body**: pass the file as a path string. For an action whose
  schema takes a body/media field, give the path (e.g. `--arg media=/tmp/clip.mp4`
  or inside `--json`); the path is read for you. A `file://` URL works too.
- **A multipart file part**: supply it as a `[filename, content, content-type]`
  array. `content` may be inline text or a path string (an existing file is read,
  otherwise the text is sent as is):
  ```bash
  thingctx invoke captions__add --json '{"file": ["c.srt", "/tmp/c.srt", "application/octet-stream"]}'
  ```
- **Read after write may lag**: a value read right after writing it may not
  reflect the change yet. If a follow up read looks stale, retry once or twice.
- **Discover, do not assume**: argument names, required fields, and which actions
  exist all come from `thingctx list`. Re run it rather than hardcoding.

## Scope

This skill is the driver: it knows how to call any Thing. A fixed multi step
sequence for a particular goal (do A, then B, then C) is domain specific and
belongs with that app as its own short recipe; this skill is what such a recipe
calls.
