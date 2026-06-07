# Mapping a Thing Description to agent tools

## Scope

This document specifies the mapping between a
[W3C Thing Description](https://www.w3.org/TR/wot-thing-description11/) and the
tools consumed by a large language model agent. The Thing Description format, the
transport bindings, and discovery are defined by W3C Recommendations; this mapping
is defined one layer above them and is not otherwise standardized.

The mapping is deterministic. A given Thing Description yields an identical set of
tools under any conforming implementation, so a description authored once behaves
uniformly across consumers. thingctx is one such implementation.

The mapping is defined in two directions. A Thing Description is projected to a set
of tool specifications presented to the agent. A tool invocation by the agent is
resolved to a request against the described system.

## Terminology

**Affordance.** A named interaction exposed by a Thing. Three kinds are defined: an
*action* (an operation to perform), a *property* (state to read or write), and an
*event* (a source to subscribe to).

**Form.** The transport binding of an affordance, comprising a target (`href`) and,
for HTTP, a method. An affordance may declare multiple forms, one per transport.

**Form invocation.** Execution of the interaction a form names against the
described system. For an HTTP form, the corresponding HTTP request to its `href`.
The described system's own endpoints serve the request; no intermediate server is
introduced.

**Tool specification.** The declaration of a callable function presented to the
agent, comprising a name, a description, and a JSON Schema for its parameters. This
document adopts the OpenAI function format for tool specifications.

## Projection of actions to tools

Each action is projected to exactly one tool.

**Name.** The tool name is `<thing>.<action>`, where `<thing>` is the final
significant segment of the Thing's `id` with a trailing version token (for example
`v1` or `2`) removed. Thus action `setSpeed` on `urn:demo:pump:v1` projects to
`pump.setSpeed`, and action `createIssue` on `urn:svc:github` projects to
`github.createIssue`. Namespacing by Thing prevents collisions across a fleet.

**Parameters.** The tool's parameters are the action's `input` JSON Schema,
unmodified. An action without an `input` schema accepts no arguments.

**Description.** The tool's description is the action's `description`. The OpenAI
function format defines no output field; an action's `output` schema is therefore
appended to the description in the form `Returns: <schema>`.

The following action on `urn:svc:github`:

```json
"createIssue": {
  "input": { "type": "object", "properties": {
    "owner": {"type": "string"}, "repo": {"type": "string"},
    "title": {"type": "string"}, "body": {"type": "string"} } },
  "forms": [{ "href": "https://api.github.com/repos/{owner}/{repo}/issues",
              "htv:methodName": "POST" }]
}
```

projects to the tool specification:

```json
{ "type": "function", "function": {
  "name": "github.createIssue",
  "description": "createIssue",
  "parameters": { "type": "object", "properties": {
    "owner": {"type": "string"}, "repo": {"type": "string"},
    "title": {"type": "string"}, "body": {"type": "string"} } } } }
```

## Resolution of a tool invocation to a form invocation

An invocation is resolved to the form of the corresponding action, and its
arguments are bound according to that form's transport binding.

**Path variables are bound first.** Each `{name}` template variable in the form's
`href` is replaced by the argument of the same name, which is then removed from the
remaining arguments. An invocation of `github.createIssue` with `owner` equal to
`my-org` and `repo` equal to `api` therefore yields the URL
`https://api.github.com/repos/my-org/api/issues`.

**Remaining arguments are bound by the transport binding.** For HTTP, in accordance
with the [WoT HTTP binding](https://www.w3.org/TR/wot-binding-templates/):

- The method is the form's `htv:methodName`, where declared.
- Where no method is declared, the method is selected by safety: an action declared
  `idempotent` is issued as `GET`, with the remaining arguments bound as query
  parameters; any other action is issued as `POST`, with the remaining arguments
  bound as a JSON request body.

The example invocation is consequently issued as
`POST https://api.github.com/repos/my-org/api/issues` with the body `{"title": ...,
"body": ...}`, corresponding to the GitHub REST API.

## Properties and events

A property is projected to read and write operations rather than to a single tool.
A read is issued as `GET` against the property's form. A write transmits the value,
by default as an HTTP `PUT`.

An event, and an observable property, is projected to a subscription over the
form's streaming binding: Server-Sent Events for HTTP, the named topic for MQTT.

## Security

A Thing Description declares the security scheme each interaction requires in its
`securityDefinitions`. The credential is never contained in the description; it is
supplied to the client at run time, keyed by the scheme name, so that a description
may be committed and shared without exposing secrets. Each scheme determines a
modification of the request:

- `bearer`: the header `Authorization: Bearer <secret>`.
- `basic`: the header `Authorization: Basic <base64(secret)>`.
- `apikey` with `in` equal to `header`: the header `<name>: <secret>`.
- `apikey` with `in` equal to `query`: the query parameter `<name>=<secret>`.
- `nosec`: no modification.

## Transports

The scheme of a form's `href` selects its binding: `https://` is bound over HTTP,
`mqtt://` over MQTT, and an `href` with no scheme is handled locally. A single Thing
may combine transports, for example reading a property over HTTP while subscribing
to an event over MQTT within one description. Binding is determined per form.

## Limitations

The following describe the mapping as currently implemented, not its intended final
state.

- Tool specifications use the OpenAI function format; other agent runtimes use other
  formats.
- Events are projected to subscriptions rather than to callable tools; an agent
  restricted to request and reply invocations does not observe them.
- Template variables are bound by name from the argument object; their individual
  schemas are not separately enforced at this layer.
- Only the HTTP, MQTT, and local bindings are mapped. Other bindings, including CoAP
  and WebSocket, are not yet implemented.

## Status

This mapping is a convention rather than a wire protocol. Its intended long-term
home is a W3C Web of Things note on the consumption of Thing Descriptions as agent
tools, to which this document is a contribution.
