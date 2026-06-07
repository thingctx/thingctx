# Mapping a Thing Description to agent tools

This document specifies how a [W3C Thing Description](https://www.w3.org/TR/wot-thing-description11/)
becomes the tools an LLM agent can call, and how a call from the agent becomes a
real request to the system. It is the one layer thingctx defines that is not
already a W3C standard. The description format, the transport bindings, and
discovery are W3C Recommendations; this mapping sits on top of them.

The mapping is deterministic. Anyone who implements it produces the same tools
from the same Thing Description, so a description authored once behaves the same
across implementations. thingctx is one such implementation.

## Terminology

**Thing Description (TD).** A JSON-LD document that describes a system: a device,
an API, a database, a service. It is plain JSON an agent can read.

**Affordance.** A single interaction a Thing offers. A TD has three kinds. An
*action* is something to do (create an issue, set a speed). A *property* is state
to read or write (the current rpm). An *event* is something to subscribe to (an
overheat alert). Affordances are named.

**Form.** The transport binding for an affordance. A form says how to perform the
interaction: a target (`href`), and for HTTP, the method. One affordance can have
several forms, one per transport. A form's `href` scheme (`https`, `mqtt`, and so
on) selects the binding.

**Form invocation.** Performing the interaction a form names against the real
system. For an HTTP form, this is the actual HTTP request to the `href`. thingctx
performs the invocation; the system's own endpoints answer it. There is no
intermediate server.

**Model tool, tool spec.** A tool is a function an agent can call. A tool spec is
the description of that function the agent is given so it can choose it and supply
arguments: a name, a description, and a JSON Schema for its parameters. This
document uses the OpenAI function format for tool specs.

The mapping has two directions. An affordance becomes a tool spec, which is what
the model sees. A tool call becomes a form invocation, which is the real request.

## Actions become tools

Each action in the Thing Description becomes one tool.

The tool's name is `<thing>.<action>`. The `<thing>` part is the last meaningful
segment of the Thing's `id`, with a trailing version token (`v1`, `2`, and the
like) removed. So action `setSpeed` on `urn:demo:pump:v1` becomes `pump.setSpeed`,
and action `createIssue` on `urn:svc:github` becomes `github.createIssue`.
Namespacing by Thing keeps actions from colliding across a fleet.

The tool's parameters are the action's `input` JSON Schema, used unchanged. An
action with no `input` takes an empty object.

The tool's description is the action's `description`. A WoT action may also declare
an `output` schema. The OpenAI function format has no output field, so the output
schema is appended to the description as `Returns: <schema>`.

For example, this action on a Thing whose `id` is `urn:svc:github`:

```json
"createIssue": {
  "input": { "type": "object", "properties": {
    "owner": {"type": "string"}, "repo": {"type": "string"},
    "title": {"type": "string"}, "body": {"type": "string"} } },
  "forms": [{ "href": "https://api.github.com/repos/{owner}/{repo}/issues",
              "htv:methodName": "POST" }]
}
```

produces this tool spec:

```json
{ "type": "function", "function": {
  "name": "github.createIssue",
  "description": "createIssue",
  "parameters": { "type": "object", "properties": {
    "owner": {"type": "string"}, "repo": {"type": "string"},
    "title": {"type": "string"}, "body": {"type": "string"} } } } }
```

## A tool call becomes a form invocation

When the agent calls a tool, the call is routed to the matching action's form, and
the arguments are placed according to the form's transport binding.

Path variables are filled first. Any `{name}` placeholder in the form's `href` is
replaced by the argument of that name, and that argument is then removed from the
set passed onward. A call to `github.createIssue` with `owner` set to `my-org` and
`repo` set to `api`, against the `href` above, yields the URL
`https://api.github.com/repos/my-org/api/issues`, with `owner` and `repo`
consumed.

The remaining arguments are placed by the transport binding. For HTTP, following
the [WoT HTTP binding](https://www.w3.org/TR/wot-binding-templates/):

- The method is the form's `htv:methodName` when the form declares one.
- When no method is declared, it is chosen by safety. An action marked
  `idempotent` uses `GET`, and the remaining arguments become query parameters.
  Any other action uses `POST`, and the remaining arguments become a JSON body.

In the GitHub example, the action declares `POST`, so the invocation is
`POST https://api.github.com/repos/my-org/api/issues` with the body
`{"title": ..., "body": ...}`. That is the GitHub REST API, called directly.

## Properties and events

A property becomes read and write calls rather than a single action tool. Reading
a property performs a `GET` on its form. Writing a property sends the new value;
over HTTP the default is `PUT`.

An event, and an observable property, is a subscription. It uses the streaming
binding the form names: Server-Sent Events for HTTP, the topic for MQTT. A
subscription yields each message as it arrives.

## Security

The Thing Description declares which security scheme an interaction requires, in
its `securityDefinitions`. The secret itself is never in the description. It is
supplied to the client at run time, keyed by the scheme name, so a description is
safe to commit and to share.

Each scheme maps to a modification of the request:

- `bearer` adds the header `Authorization: Bearer <secret>`.
- `basic` adds the header `Authorization: Basic <base64 of the secret>`.
- `apikey` with `in` set to `header` adds the header `<name>: <secret>`.
- `apikey` with `in` set to `query` adds the query parameter `<name>=<secret>`.
- `nosec` adds nothing.

## Transports

A form's `href` scheme selects its binding. An `https://` href is driven over
HTTP, an `mqtt://` href over MQTT, and an href with no scheme is handled locally.
A single Thing may mix transports: it can read a property over HTTP and subscribe
to an event over MQTT in the same description. The mapping is per form, so each
interaction uses the transport its own form names.

## Current gaps

This describes the mapping as implemented today, not an intended future state.

- Tool specs use the OpenAI function format. Other agent runtimes use other
  formats. An implementation may translate; this document specifies the OpenAI
  form.
- Events are exposed as subscriptions, not as callable tools. An agent that only
  makes request and reply tool calls does not see events.
- Path and query variables are substituted by name from the flat argument object.
  Their individually declared schemas are not separately enforced at this layer.
- Only the HTTP, MQTT, and local bindings are mapped. Other WoT bindings, such as
  CoAP and WebSocket, are not yet implemented.

## Why this is specified

The mapping is the interoperability surface. Two implementations that follow it
produce the same tools from the same Thing Description, so a description authored
once works the same whether it is driven by thingctx, by an MCP server built on
the same rules, or by another library. It is a convention, not a wire protocol.
Its natural long-term home is a W3C Web of Things note on consuming Thing
Descriptions as agent tools, for which this document is a starting point.
