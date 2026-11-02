# Connect ChatGPT

Connect ChatGPT to a running Workgate Control through its **MCP endpoint**.

## 1. Find your MCP URL

ChatGPT needs a reachable **public HTTPS** Control endpoint. Use its public address followed by /mcp, for example:

~~~text
https://control.example.com/mcp
~~~

Follow [VPS deployment](vps.md) to publish Control. Your computer's 127.0.0.1 address is only usable by programs on that same computer.

## 2. Add Workgate

In ChatGPT, open your MCP connector settings and add the MCP URL. Complete the OAuth sign-in and owner approval when prompted. Use a ChatGPT mode with access to MCP tools for workspace operations.

## 3. Test the connection

Try:

> Use Workgate to start a session in my project directory and show its Git status.

You can then [explore common workflows](../guides/common-workflows.md) or open the [WebUI](../guides/webui.md).

If the connection or authorization fails, see [Troubleshooting](../troubleshooting.md).
