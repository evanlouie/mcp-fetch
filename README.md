# Fetch MCP Server (Browser Impersonation Fork)

A Model Context Protocol server that provides web content fetching capabilities with browser impersonation. This server enables LLMs to retrieve and process content from web pages, converting HTML to markdown for easier consumption.

This is a fork of the [original MCP fetch server](https://github.com/modelcontextprotocol/servers/tree/main/src/fetch) that utilizes [curl_cffi](https://github.com/lexiforest/curl_cffi) for browser impersonation, allowing it to bypass basic bot detection and access websites that might block standard HTTP requests.

## Key Features

- **Browser Impersonation**: Uses curl_cffi to mimic real browser requests, helping bypass basic bot detection
- **Content Extraction**: Converts HTML to markdown for easier LLM consumption
- **Chunked Reading**: Support for reading large webpages in chunks using `start_index`
- **Browser Headers**: Uses realistic Chrome browser headers for better compatibility

> [!CAUTION]
> This server can access local/internal IP addresses and may represent a security risk. Exercise caution when using this MCP server to ensure this does not expose any sensitive data.

The fetch tool will truncate the response, but by using the `start_index` argument, you can specify where to start the content extraction. This lets models read a webpage in chunks, until they find the information they need.

### Available Tools

- `fetch` - Fetches a URL from the internet and extracts its contents as markdown.
  - `url` (string, required): URL to fetch
  - `max_length` (integer, optional): Maximum number of characters to return (default: 5000)
  - `start_index` (integer, optional): Start content from this character index (default: 0)
  - `raw` (boolean, optional): Get raw content without markdown conversion (default: false)

### Prompts

- **fetch**
  - Fetch a URL and extract its contents as markdown
  - Arguments:
    - `url` (string, required): URL to fetch

## Installation

### Using uv

When using [`uv`](https://docs.astral.sh/uv/) no specific installation is needed. We will
use [`uvx`](https://docs.astral.sh/uv/guides/tools/) to directly run from the git repository:

```
uvx --from git+https://github.com/evanlouie/mcp-fetch.git mcp-server-fetch
```

## Configuration

### Configure for Claude.app

Add to your Claude settings:

```json
{
  "mcpServers": {
    "fetch": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/evanlouie/mcp-fetch.git",
        "mcp-server-fetch"
      ],
      "env": {
        "PYTHONWARNINGS": "ignore",
        "npm_config_audit": "false",
        "npm_config_fund": "false"
      }
    }
  }
}
```

### Configure for VS Code

For manual installation, add the following JSON block to your User Settings (JSON) file in VS Code. You can do this by pressing `Ctrl + Shift + P` and typing `Preferences: Open User Settings (JSON)`.

Optionally, you can add it to a file called `.vscode/mcp.json` in your workspace. This will allow you to share the configuration with others.

> Note that the `mcp` key is needed when using the `mcp.json` file.

```json
{
  "mcp": {
    "servers": {
      "fetch": {
        "command": "uvx",
        "args": [
          "--from",
          "git+https://github.com/evanlouie/mcp-fetch.git",
          "mcp-server-fetch"
        ],
        "env": {
          "PYTHONWARNINGS": "ignore",
          "npm_config_audit": "false",
          "npm_config_fund": "false"
        }
      }
    }
  }
}
```

### Customization - Proxy

The server can be configured to use a proxy by using the `--proxy-url` argument.

## Troubleshooting

### JSON Parsing Errors

If you encounter JSON parsing errors like:

```
Unexpected token 'o', "found 0 vul"... is not valid JSON
```

This is caused by security scanners or audit tools outputting non-JSON content to stdout during package installation. The MCP protocol requires pure JSON-RPC messages on stdout, but vulnerability scanners (like npm audit) can contaminate this output.

**Solution**: The environment variables in the configuration examples above suppress these outputs:

- `PYTHONWARNINGS=ignore` - Suppresses Python warning messages
- `npm_config_audit=false` - Disables npm security audits
- `npm_config_fund=false` - Disables npm funding messages

**Alternative Solution**: If you continue to experience issues, you can create a wrapper script:

```bash
#!/bin/bash
uvx --from git+https://github.com/evanlouie/mcp-fetch.git mcp-server-fetch 2>/dev/null
```

Then use the wrapper script path as your command instead of `uvx`.

## Debugging

You can use the MCP inspector to debug the server. For uvx installations:

```
npx @modelcontextprotocol/inspector uvx --from git+https://github.com/evanlouie/mcp-fetch.git mcp-server-fetch
```

Or if you've installed the package in a specific directory or are developing on it:

```
cd path/to/mcp-fetch
npx @modelcontextprotocol/inspector uv run src/mcp_fetch
```

## Contributing

We encourage contributions to help expand and improve mcp-fetch. Whether you want to add new tools, enhance existing functionality, or improve documentation, your input is valuable.

This project is a fork of the original MCP fetch server. For the original implementation and other MCP servers, see:
https://github.com/modelcontextprotocol/servers

Pull requests are welcome! Feel free to contribute new ideas, bug fixes, or enhancements to make mcp-fetch even more powerful and useful.

## Acknowledgments

This project is based on the original [MCP fetch server](https://github.com/modelcontextprotocol/servers/tree/main/src/fetch) from the Model Context Protocol team. The browser impersonation capabilities are powered by [curl_cffi](https://github.com/lexiforest/curl_cffi).

## License

mcp-fetch is licensed under the MIT License. This means you are free to use, modify, and distribute the software, subject to the terms and conditions of the MIT License. For more details, please see the LICENSE file in the project repository.
