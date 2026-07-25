# The thingctx skill (Agent Skills / agentskills.io format)

`thingctx-driver/` is a portable agent skill: it teaches any skill-supporting agent to
drive a registered W3C WoT Thing from the shell (`thingctx list`, `thingctx invoke`).
It follows the open [Agent Skills](https://agentskills.io/specification) standard, so
the same folder works across 40+ apps (Claude, Cursor, Copilot, Codex, Gemini CLI,
OpenCLAW/ClawHub, and more). The description is kept under 160 chars so it fits every
host, including OpenCLAW's stricter cap.

Install (any Agent Skills host): copy `thingctx-driver/` into the host's skills dir
(commonly `~/.claude/skills/` or the app's own), or run `thingctx skill install`.

Prerequisite: `thingctx` on PATH (`pip install thingctx` or `uvx thingctx ...`) and at
least one registered Thing (`thingctx registry add <source>`).

Publish: submit `thingctx-driver/` to ClawHub and the community skill lists. The folder
is self-contained; no build step.
