# Install the thingctx bundle in Claude Desktop

The bundle gives Claude Desktop a set of tools built from Thing Descriptions, and
puts a policy gate in front of every call. It is a launcher, not a copy of
thingctx: three small files, no vendored code. `uv` installs thingctx itself on
first launch.

## Before you start

Claude Desktop, and [uv](https://docs.astral.sh/uv/).

    uv --version

The bundle looks for `uv` in the three places it normally lands: `/usr/local/bin`,
`/opt/homebrew/bin`, and `~/.local/bin`, which is where the standalone installer
puts it. Desktop starts the server with a minimal PATH that does not include your
shell profile, so somewhere else means it will not be found, and the only symptom
is "Unable to connect to extension server". If `uv` lives somewhere unusual, link
it into one of those three.

## Install

1. Download `thingctx.mcpb`.
2. Open Claude Desktop, go to Settings, then Extensions.
3. Drag the `.mcpb` onto the window, or use Install from file.
4. Answer the four questions below.
5. Quit Claude Desktop completely and reopen it. Not just the window: the server
   starts with the app, so a reload will not pick up a config change.

First launch takes a minute or two while `uv` resolves the dependencies. The
media extra pulls PyAV and numpy, which are large.

## What it asks you

**Things** is the only required answer. It takes a TD Directory URL, a folder of
Thing Descriptions, or a single TD file. The default is the hosted registry,
which is the fastest way to see it work. Point it at your own folder when you
have one.

**Policy** decides what the tools may do at all. `read-only`, the default, allows
reads, subscriptions and actions the description marks safe. `full` allows
everything the Things declare.

**Approve when** decides what needs your say so. `destructive`, the default, asks
before any write or non idempotent action. `declared` asks only for actions the
description marks risky. `all` asks every time. `never` disables the prompt, which
also means nothing stops a mistake.

**Filesystem root** is empty by default, and while it is empty every filesystem
call is refused. Set it only when you want the filesystem Thing, and set it to
the narrowest directory that works.

## Check it worked

Ask Claude what tools it has. You should see names shaped `<thing>__<action>`,
like `time__getCurrentTime`.

Then ask it for something harmless, a current time or a status read, and confirm
you get a real answer.

Then ask for something that changes state. With the default policy you should be
asked to approve first. If it goes through without asking, the gate is not doing
its job and that is worth reporting.

## When it does not work

**The extension is there but has no tools.** Almost always the Things setting:
a URL that does not serve JSON, or a folder with no `.td.json` in it. Check the
value resolves in a browser.

**Nothing appears at all.** Usually `uv` is not on the PATH Desktop sees, which
is not always the PATH your shell has. The Desktop MCP log names the failure.

**It worked and then stopped.** If you changed a setting, quit and reopen the app
rather than reloading.

## Removing it

Settings, Extensions, remove. Nothing is left behind except the packages `uv`
cached.
