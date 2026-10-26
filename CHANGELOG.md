# Changelog

## Unreleased

### Added

- MQTT over TLS: `MqttBinding` connects over TLS for an `mqtts://` href, on port 8883 unless one is named, and `MqttGatewayBinding` for an `mqtts://host[:port]` broker; either takes `tls=MqttTls(ca_certs, certfile, keyfile)` to use TLS with a given CA file or client certificate. The broker's certificate and hostname are always verified, against the system's CA store by default. A gateway on TLS writes `mqtts://` in the forms it projects. A plain `mqtt://` href or `host:port` broker stays plain. A client certificate credential's key password now reaches TLS; before, paho asked for it at the terminal.
- `thingctx.authz.limited(thing, affordance, op, schema)` builds a grant that bounds its arguments: an action's input (with the TD's `const` and `default` values filled) or a written value must satisfy the JSON Schema, or the client refuses the call before the device is touched, directly or through a gateway. `AuthZenPDP` sends the arguments as the action's `properties.arguments`. `acl_file` compiles a limited grant like an unlimited one. A limited grant denies when `jsonschema` is not installed, when its schema cannot be applied, and when an argument is NaN or an infinity; `limited` refuses a numeric bound without a numeric `type`, which a string such as `"99"` would pass, and a `$ref` or `$dynamicRef`, which could hide one.
- The MQTT client sends the caller's bearer token as an MQTT 5 User Property with every request when the Thing Description's bearer scheme names its parameter; a scheme that names none keeps the token as the connection password.
- `thingctx.gateways.mosquitto.acl_file` writes Mosquitto rules for a guarded gateway bus: only gateway users read requests, so no other client sees the tokens they carry, and each caller publishes requests only under its own User Name and reads only its own replies and streams. Given a caller's grants, the rules allow exactly those operations, so the broker refuses any other request before the gateway sees it. Given the Things a gateway serves, they limit its login to those Things; with more than one gateway, `serves` must name each. The rules name each user. A name that cannot be one topic level is refused, as is a Thing whose slug is `_instances`, a level thingctx reserves.
- The MQTT client fills a URI variable typed `tc:MqttUserName` with the User Name it connects with, and the tool projection leaves it out. `thingctx.thing.MQTT_USER_NAME` and `is_mqtt_user_name` name and recognize the term.
- A gateway driver declares its security schemes through `thingctx.gateways.DeclaresSecurity` and, once for every Thing, its URI variables through `DeclaresUriVariables`; the conformance kit reports and checks both.
- A guarded MQTT gateway serves `observeproperty`: an authorized request on `<prefix>/<thing>/props/<property>/observe/<user>` streams the property's changes to `<request>/stream`, as a subscribe request streams an event, and `acl_file` compiles an observe grant to that request and stream. A client observing a property uses the form that declares `observeproperty`.

### Changed

- A call whose arguments the Thing Description does not allow is now refused. An action's input, with its `const` and `default` values filled, and a written property value are checked against the TD's schema (`type`, `enum`, `const`, `pattern`, `minLength`, `maxLength`, `minimum`, `maximum`, `required` and `properties`, through nested objects and array items) before the call is authorized, approved or sent. A mismatch returns `{"error": "invalid input", "tool": ..., "reason": ...}` naming the first mismatch and is recorded as refused. Before, anything else reached the device as given.
- A guarded MQTT gateway ends every request topic in the sender's MQTT User Name (`<prefix>/<thing>/actions/<action>/<user>`, `.../props/<property>/read/<user>`, `.../props/<property>/write/<user>`, `.../events/<event>/subscribe/<user>`), declares that level in its projected Thing Descriptions as a Thing level URI variable typed `tc:MqttUserName`, and runs a request only when the token's `sub` is that User Name. A property's read and write have a topic each, so the topic names the whole operation. Replies and streams go under the same level, so each caller reads only its own; a guarded event's form names the caller's stream; guarded forms no longer carry `mqv:retain`. A guarded gateway is refused at construction without a basic scheme in `bus_security`, because a client takes the User Name from that scheme's credential. Clients written for the earlier topics no longer reach a guarded gateway; restart each gateway so it republishes its descriptions.

### Fixed

- The MQTT gateway writes `mqv:qos` as the string the WoT MQTT binding's schema allows (`"0"`, `"1"` or `"2"`), not an integer, so a projected Thing Description passes that schema.
- An MQTT gateway built with `default_retain=True` publishes each mirrored event retained, as its form declares, and its property forms declare `mqv:retain` false, because a property topic carries requests, never a value. Before, both forms declared `mqv:retain` true and nothing was retained, so a client reading a property by its retained value waited for a value that never came.
- Closing an MQTT connection no longer blocks the event loop: the binding and the gateway send DISCONNECT first, so paho's network thread ends, and join it off the loop. Before, a close stopped the thread first, on the loop, which could stall every other task until the broker gave up.
- Closing a client disconnects every MQTT subscription it opened, including one whose stream was never read and used to keep its connection.
- A secret that is not a token, such as a (user, password) pair keyed by a Thing's id for its basic scheme, is no longer sent as the bearer token too; a Thing with both schemes used to carry `Bearer ('user', 'password')` on every request.
- An MQTT gateway subscribes and retains its descriptions again on each new connection, so it keeps serving after a broker restart or a network drop; before, it reconnected and served nothing.
- An MQTT gateway returns from `start` only once the broker has accepted its connection and acknowledged every subscription, so a request sent right after start finds its topic served, and a retained description means its topics are served. A refused connection or subscription, or no answer within the new `connect_timeout`, raises `ConnectionError` instead of leaving a gateway that serves nothing.
- The vocabulary namespace (`thingctx.extensions.VOCAB_URI`, the `tc` prefix the prompt templates use) and the record schema's id move from thingctx.dev, a domain the project does not use, to thingctx.com. Terms matched by local name, such as `tc:PromptTemplate`, `tc:Destructive` and `tc:requiresApproval`, still match in a description that binds `tc` to the old address.
- Two in process Things bound with `register_thing` whose events share a name no longer share subscribers.
- A guarded MQTT gateway given a second subscribe request for the same event and stream topic replaces the running stream instead of adding one, so each event reaches the stream once.
- A guarded MQTT gateway streams an event only to `<request topic>/stream`; a subscribe request can no longer name the topic the gateway publishes to.
- An MQTT request the broker refuses (PUBACK reason 0x80 or more) fails at once instead of after the timeout, and a call that carries the caller's token awaits the answer even when the action declares no output, so a refusal is never reported as sent.
- A guarded MQTT gateway's projected Thing Descriptions declare its bearer scheme and any broker scheme given as `bus_security`, instead of nosec, so a client knows how to authenticate.
- The MQTT gateway's projected Thing Description carries each affordance as the source declares it (input, output, description, `synchronous`, a property's schema), not only the bus forms, so a consumer sees parameters and knows when to await a reply.
- The gateway waits for a long running action, up to 30 seconds, and replies with its state as JSON, instead of the text form of the status handle.
- The MQTT binding reads a property: the retained value when the form has `mqv:retain`, otherwise a request on the topic answered on `<topic>/reply`.
- MQTT requests carry an MQTT 5 Response Topic and Correlation Data, and the MQTT gateway copies the data into its reply, so two calls in flight on one topic no longer take each other's replies. A request now needs a broker that speaks MQTT 5.
- An MQTT request expires with the binding's timeout, so a broker never delivers it after the caller stopped waiting, and a request no subscriber matched (PUBACK reason 0x10) fails at once instead of after the timeout.

## 0.2.3 - 2026-08-30

### Fixed

- `pip install thingctx[mcp]` works again: the bridge is pinned below `mcp` 2.0, which redesigned the server API. Also fixes the Claude Desktop bundle.
- The CLI reports input errors without a traceback (#119, @lntutor).
- The default path no longer reaches the registry during packaging (#115).

### Added

- Redis transport binding: properties to keys, events to pub/sub, over `redis://` and `rediss://`, behind the `redis` extra (#130, @avishekrao).
- Per caller authorization on the MCP HTTP bridge: each call is authorized against the request's validated caller, not the bridge's own identity (#141, @KrzysiekSko).

### Changed

- Every GitHub Action is pinned by commit SHA (#127, @WAHIB-EL-KHADIRI).
- ruff moves to 0.16.0 (#125, @TalhaTahir24).
