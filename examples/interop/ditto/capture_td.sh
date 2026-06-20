#!/usr/bin/env bash
# Reproduce the Ditto-generated TD fixture from scratch.
#
# Prereqs: Docker running. Brings up the Eclipse Ditto docker-compose stack,
# creates a twin from a public WoT Thing Model, and captures the TD that Ditto
# generates. Ditto is the TD *producer*; drive_ditto_td.py is the consumer.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DITTO_SRC="$HERE/.ditto-src"
THING_ID="org.thingctx:lamp-1"
TM="https://eclipse.dev/ditto/wot/example-models/dimmable-colored-lamp-1.0.0.tm.jsonld"
BASE="http://localhost:8080/api/2"

# 1. Get Ditto's official compose stack (shallow) and start it.
[ -d "$DITTO_SRC" ] || git clone --depth 1 https://github.com/eclipse-ditto/ditto.git "$DITTO_SRC"
( cd "$DITTO_SRC/deployment/docker" && docker compose up -d )

# 2. Wait for the gateway (behind nginx, basic auth ditto:ditto) to be ready.
echo "waiting for Ditto..."
until [ "$(curl -s -o /dev/null -w '%{http_code}' -u ditto:ditto "$BASE/things")" = "200" ]; do
  sleep 3
done

# 3. Create a twin whose definition points at the WoT Thing Model. Ditto
#    generates the thing skeleton (attributes) from the model.
curl -s -u ditto:ditto -X PUT "$BASE/things/$THING_ID" \
  -H 'Content-Type: application/json' \
  -d "{\"definition\":\"$TM\"}" >/dev/null

# 4. Ask Ditto for the TD describing that twin (content negotiation).
curl -s -u ditto:ditto -H 'Accept: application/td+json' \
  "$BASE/things/$THING_ID" | python3 -m json.tool > "$HERE/ditto-generated-td.json"

echo "captured -> ditto-generated-td.json"
