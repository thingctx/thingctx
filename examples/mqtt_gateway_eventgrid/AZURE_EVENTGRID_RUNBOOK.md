# Live test: MqttGateway against Azure Event Grid's MQTT broker

This runbook takes you from an empty Azure subscription to a green run of
`run_eventgrid.py`. The script puts an in-process demo Thing on the Event Grid
MQTT broker with `MqttGateway`, then drives it back over the bus as a plain
consumer and asserts a write / read / invoke round-trip.

## Read this first (safety)

- **Personal subscription and tenant only.** Do every step below in your own
  personal Azure subscription and tenant. **Never** the corporate tenant. If you
  are signed into a work account, sign out or use a separate CLI profile first
  (`az login` with the personal account, confirm with `az account show`).
- **No secrets in the repo.** The client certificate and private key are
  generated locally and are gitignored (`*.pem *.key *.crt *.pfx`). The harness
  reads their **paths**; it never embeds key material and never prints a secret.
- **Nothing real is hardcoded.** The namespace hostname, client ids, and cert
  paths are passed at run time (flags or env vars). There is no default that
  points at a real endpoint; a missing required value fails closed with an error.
- **Clean up when done** (last section) so an idle namespace does not bill.

## What the harness needs from Event Grid

Event Grid's MQTT broker is MQTT v5 over TLS on **port 8883** (there is no plain
1883). It gates every publish/subscribe by a **topic space** + **permission
binding**, and it requires each connection's **MQTT client id to equal the
registered client's authentication name**. The harness opens **two** connections
at once (the gateway and the consumer), so you register **two** clients with
**two** client ids.

You will create, in order:

1. an Event Grid **namespace** with MQTT enabled,
2. a **CA / client certificate** (or thumbprint-based client auth),
3. **two clients** (`gateway-01`, `consumer-01`) authenticated by that cert,
4. a **topic space** covering `tc/#`,
5. a **permission binding** granting those clients publisher + subscriber on it.

---

## Step 1 — Sign in to your PERSONAL subscription

```bash
az login                       # use your PERSONAL account
az account show                # CONFIRM this is the personal subscription/tenant
az account set --subscription "<your-personal-subscription-id>"

# One-time: make sure the CLI has the Event Grid namespace commands.
az extension add --name eventgrid --upgrade

RG=thingctx-eg-lab
LOC=eastus
NS=thingctx-eg-$RANDOM        # namespace names are globally unique
az group create -n "$RG" -l "$LOC"
```

## Step 2 — Create the namespace with MQTT enabled

```bash
az eventgrid namespace create \
  --resource-group "$RG" \
  --name "$NS" \
  --location "$LOC" \
  --topic-spaces-configuration "{state:Enabled}"
```

Get the MQTT hostname the harness connects to:

```bash
az eventgrid namespace show -g "$RG" -n "$NS" \
  --query "topicSpacesConfiguration.hostname" -o tsv
# -> e.g.  thingctx-eg-1234.eastus-1.ts.eventgrid.azure.net
# This is your --host value.
```

## Step 3 — Generate a client certificate (self-signed CA is fine for a lab)

The MQTT broker authenticates clients by X.509. The simplest lab setup is a
self-signed CA whose thumbprint you register once; every client cert you issue
from it is then trusted. For a first run you can also register a single
self-signed client cert directly by thumbprint. Below is the direct route.

```bash
mkdir -p certs && cd certs

# A self-signed client cert + key. CN can be anything; Event Grid matches the
# registered client by thumbprint, not by CN.
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout client.key -out client.pem -days 30 \
  -subj "/CN=thingctx-lab-client"

# The SHA-1 thumbprint Event Grid wants, uppercase hex, no colons.
openssl x509 -in client.pem -noout -fingerprint -sha1 \
  | sed 's/.*=//; s/://g'
# -> e.g. A1B2C3...  (save this; it is THUMBPRINT below)
cd ..
```

Both `client.pem` and `client.key` are gitignored. The harness reads them via
`--cert` / `--key`; it never reads the key's contents into any committed file.

## Step 4 — Register TWO clients (gateway + consumer)

Each connection needs its own client id == its own registered client name. Both
can share the same certificate thumbprint (same cert, two sessions).

```bash
THUMBPRINT=<paste the uppercase, colon-free SHA-1 from step 3>

for NAME in gateway-01 consumer-01; do
  az eventgrid namespace client create \
    --resource-group "$RG" \
    --namespace-name "$NS" \
    --client-name "$NAME" \
    --authentication-name "$NAME" \
    --client-certificate-authentication "{validationScheme:ThumbprintMatch,allowedThumbprints:[$THUMBPRINT]}" \
    --state Enabled
done
```

`--authentication-name` is what the client id must equal. The harness sends
`--gateway-client-id gateway-01` and `--consumer-client-id consumer-01`, so keep
these names in sync.

## Step 5 — Create a topic space for the prefix

The harness uses the `tc` prefix by default, so the topic space must cover
`tc/#`. If you pass `--prefix foo`, cover `foo/#` instead.

```bash
az eventgrid namespace topic-space create \
  --resource-group "$RG" \
  --namespace-name "$NS" \
  --topic-space-name tc-space \
  --topic-templates "tc/#"
```

## Step 6 — Grant publisher + subscriber (permission bindings)

A permission binding ties a client group to a topic space with a role. The
gateway and consumer each need **both** publisher and subscriber (the gateway
subscribes to action/property topics and publishes replies + retained TDs; the
consumer publishes requests and subscribes to replies). The built-in
`$all` client group covers every registered client, which is fine for a lab.

```bash
for ROLE in Publisher Subscriber; do
  az eventgrid namespace permission-binding create \
    --resource-group "$RG" \
    --namespace-name "$NS" \
    --permission-binding-name "tc-$ROLE" \
    --client-group-name '$all' \
    --topic-space-name tc-space \
    --permission "$ROLE"
done
```

## Step 7 — Run the harness

From the repo root, with the venv that has `paho-mqtt` installed:

```bash
python examples/mqtt_gateway_eventgrid/run_eventgrid.py \
  --host        "$(az eventgrid namespace show -g "$RG" -n "$NS" --query topicSpacesConfiguration.hostname -o tsv)" \
  --gateway-client-id  gateway-01 \
  --consumer-client-id consumer-01 \
  --cert        ./certs/client.pem \
  --key         ./certs/client.key \
  --prefix      tc
```

Or set them once as env vars and run bare:

```bash
export EG_MQTT_HOST="<namespace-hostname>"
export EG_GATEWAY_CLIENT_ID=gateway-01
export EG_CONSUMER_CLIENT_ID=consumer-01
export EG_CLIENT_CERT=./certs/client.pem
export EG_CLIENT_KEY=./certs/client.key
python examples/mqtt_gateway_eventgrid/run_eventgrid.py
```

Expected output ends with:

```
[1/3] write  target_rpm = 1800 through the bus ...
[2/3] read   target_rpm through the bus ...
[3/3] invoke set_speed rpm=2400 through the bus ...

PASS: write/read/invoke round-tripped through Event Grid.
```

**Offline smoke test** (no Azure, no cert files, no network) — always works and
is how the script was verified before you had a broker:

```bash
python examples/mqtt_gateway_eventgrid/run_eventgrid.py --dry-run
```

## Troubleshooting

- **Connection refused / TLS handshake fails.** Confirm port 8883 and that the
  cert thumbprint registered in step 4 matches `openssl ... -fingerprint -sha1`
  of the cert you pass. A stale or mismatched thumbprint is the usual cause.
- **`Not authorized` on connect.** The client id you sent does not match a
  registered `--authentication-name`. They must be identical (`gateway-01`,
  `consumer-01`).
- **Connect works but no reply (timeout on `/reply`).** The topic space does not
  cover `tc/#`, or the permission binding is missing Publisher or Subscriber.
  Re-check steps 5 and 6; the prefix in the topic template must match `--prefix`.
- **Both connections fight / one drops.** You reused one client id for both
  connections. Event Grid allows one active session per client id; register and
  use two distinct ones.

## Clean up

```bash
az group delete -n "$RG" --yes --no-wait
rm -rf certs
```

Deleting the resource group removes the namespace, clients, topic space, and
bindings in one step so nothing keeps billing.
