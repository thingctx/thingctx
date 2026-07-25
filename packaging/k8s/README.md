# Run the thingctx gateway on Kubernetes (example)

An EXAMPLE for running the thingctx gateway as a central, governed tool gateway for a
team. You apply it; you operate it. The gateway is stateless: it reads Thing
Descriptions, projects tools, and enforces per-caller authorization on every request,
so it scales horizontally with no sticky state.

The stateful parts (credentials, tokens) live in a Kubernetes Secret you mount, never
in the image. The gateway is a stateless enforcer in front of your secret store.

    kubectl apply -f secret.example.yaml   # edit first: your real credentials
    kubectl apply -f deployment.yaml
    kubectl apply -f service.yaml

The gateway serves the MCP protocol over streamable HTTP on port 8080 (path `/`), so a
cloud agent runtime or another service reaches it by URL
(`http://thingctx-gateway:8080/` in-cluster). Put an Ingress or a LoadBalancer in front
to expose it outside the cluster.

Try it locally with k3d (a cluster in Docker):

    k3d cluster create thingctx
    k3d image import ghcr.io/thingctx/thingctx:0.2.0 -c thingctx   # or build+import your own
    kubectl apply -f secret.example.yaml -f deployment.yaml -f service.yaml
    kubectl port-forward svc/thingctx-gateway 8080:8080
    # then POST an MCP initialize to http://127.0.0.1:8080/
