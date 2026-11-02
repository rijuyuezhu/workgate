# Run Control and Executor on one computer

**Colocate** runs Control and Executor together. For a new installation, choose the **Colocate** role in [Install Workgate](install-options.md). The installer guides you through approval and sets up the user service.

## Run from a development checkout

If you are working directly with the source, initialize and pair once:

1. Initialize a private local Control config with `uv run workgate control init-config`, then start Control using `uv run workgate control`.
2. In another terminal, run `uv run workgate executor connect http://127.0.0.1:8765 --name local`.
3. Approve the request at `http://127.0.0.1:8765/ui` using the admin PIN saved in the Control config, then stop Control.

Start both roles:

~~~sh
uv run workgate colocate
~~~

WebUI: http://127.0.0.1:8765/ui · MCP: http://127.0.0.1:8765/mcp

For a persistent service, use **workgate colocate install-service**. Check options with **workgate colocate --help**.

To run the roles on different hosts, see [Choose a deployment](deployment.md).
