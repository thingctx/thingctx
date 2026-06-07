# Mapping a Thing Description to agent tools

A Thing Description and an agent's tools are two different shapes, and something
must convert between them. That conversion is the one layer thingctx defines that
is not already a W3C standard. The description format, the transport bindings, and
discovery are W3C Recommendations; this mapping sits one layer above them.

Writing it down makes the conversion deterministic: the same Thing Description
yields the same tools in any implementation. thingctx is one; an MCP server or
another library built on these rules would produce the same tools.

The mapping has two directions. A Thing Description becomes the tools an agent
sees. A tool call becomes a real request to the system.

## Terminology

**Affordance.** A named interaction a Thing offers. There are three kinds: an
*action* (something to do), a *property* (state to read or write), an *event*
(something to subscribe to).

**Form.** The transport binding for an affordance: a target (`href`) and, for
HTTP, a method. An affordance can have several forms, one per transport.

**Form invocation.** Performing the interaction the form names against the real
system. For HTTP, the actual request to the `href`. The system's own endpoints
answer it; there is no intermediate server.

**Tool spec.** The description of a callable function an agent is given so it can
choose it and supply arguments: a name, a description, and a JSON Schema for its
parameters. This document uses the OpenAI function format.

## Actions become tools

Each action becomes one tool.

**Name:** `<thing>.<action>`, where `<thing>` is the last meaningful segment of the
Thing's `id` with a trailing version token (`v1`, `2`) removed. So `setSpeed` on
`urn:demo:pump:v1` is `pump.setSpeed`, and `createIssue` on `urn:svc:github` is
`github.createIssue`. Namespacing by Thing avoids collisions across a fleet.

**Parameters:** the action's `input` JSON Schema, unchanged. No `input` means no
arguments.

**Description:** the action's `description`. The OpenAI format has no output
field, so an `output` schema is appended as `Returns: <schema>`.

For example, this action on `urn:svc:github`:

```json
"createIssue": {
  "input": { "type": "object", "properties": {
    "owner": {"type": "string"}, "repo": {"type": "string"},
    "title": {"type": "string"}, "body": {"type": "string"} } },
  "forms": [{ "href": "https://api.github.com/repos/{owner}/{repo}/issues",
              "htv:methodName": "POST" }]
}
```

becomes:

```json
{ "type": "function", "function": {
  "name": "github.createIssue",
  "description": "createIssue",
  "parameters": { "type": "object", "properties": {
    "owner": {"type": "string"}, "repo": {"type": "string"},
    "title": {"type": "string"}, "body": {"type": "string"} } } } }
```

## A tool call becomes a form invocation

The call is routed to the action's form, and the arguments are placed by the
form's binding.

**Path variables first.** Any `{name}` in the `href` is replaced by the argument
of that name, which is then consumed. So `createIssue(owner="my-org", repo="api",
...)` gives the URL `https://api.github.com/repos/my-org/api/issues`.

**Remaining arguments by binding.** For HTTP, per the
[WoT HTTP binding](https://www.w3.org/TR/wot-binding-templates/):

- The method is the form's `htv:methodName` if declared.
- Otherwise it defaults by safety: an `idempotent` action uses `GET` with the
  arguments as query parameters; any other action uses `POST` with them as a JSON
  body.

So the GitHub call is `POST .../repos/my-org/api/issues` with body `{"title": ...,
"body": ...}`: the GitHub REST API, called directly.

## Properties and events

A property becomes read and write calls. Reading is a `GET` on its form; writing
sends the value, by default an HTTP `PUT`.

An event, or an observable property, is a subscription over the form's streaming
binding: Server-Sent Events for HTTP, the topic for MQTT.

## Security

The Thing Description declares which scheme an interaction needs, in
`securityDefinitions`. The secret is never in the description; it is supplied to
the client at run time, keyed by the scheme name, so a description is safe to
commit. Each scheme modifies the request:

- `bearer`: header `Authorization: Bearer <secret>`.
- `basic`: header `Authorization: Basic <base64(secret)>`.
- `apikey`, `in: header`: header `<name>: <secret>`.
- `apikey`, `in: query`: query parameter `<name>=<secret>`.
- `nosec`: nothing.

## Transports

The `href` scheme selects the binding: `https://` over HTTP, `mqtt://` over MQTT,
no scheme handled locally. A Thing may mix them, reading a property over HTTP and
subscribing to an event over MQTT in one description. The mapping is per form.

## Current gaps

As implemented today, not an intended state:

- Tool specs use the OpenAI function format; other runtimes use others.
- Events are subscriptions, not callable tools, so a request-and-reply agent does
  not see them.
- Path and query variables are filled by name; their individual schemas are not
  enforced at this layer.
- Only HTTP, MQTT, and local bindings are mapped. CoAP, WebSocket, and others are
  not yet implemented.

## Status

A convention, not a wire protocol. Its natural long-term home is a W3C Web of
Things note on consuming Thing Descriptions as agent tools, for which this is a
starting point.
