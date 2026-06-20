// Minimal node-wot producer: exposes a `counter` Thing and serves its TD at
// http://localhost:8080/counter. The W3C reference implementation generates the
// Thing Description; thingctx consumes it (see drive_nodewot_td.py).
//
//   npm install @node-wot/core @node-wot/binding-http
//   node producer.js

const { Servient } = require("@node-wot/core");
const { HttpServer } = require("@node-wot/binding-http");

const servient = new Servient();
servient.addServer(new HttpServer({ port: 8080 }));

servient.start().then(async (WoT) => {
  let count = 0;

  const thing = await WoT.produce({
    id: "urn:dev:counter",
    title: "counter",
    description: "A simple counter Thing.",
    properties: {
      count: { type: "integer", readOnly: true, observable: true },
    },
    actions: {
      increment: { description: "Add one to the count." },
      decrement: { description: "Subtract one from the count." },
      reset: { description: "Reset the count to zero." },
    },
  });

  thing.setPropertyReadHandler("count", async () => count);
  thing.setActionHandler("increment", async () => { count += 1; return undefined; });
  thing.setActionHandler("decrement", async () => { count -= 1; return undefined; });
  thing.setActionHandler("reset", async () => { count = 0; return undefined; });

  await thing.expose();
  console.log("counter exposed at http://localhost:8080/counter");
});
