# Development Guidelines for MCP Fetch Server

## Commands

- **Python**: `uv run python ...`
- **Type check**: `uv run basedpyright`
- **Lint**: `uv run ruff check --fix .`
- **Format**: `uv run ruff format`
- **Run server**: `uv run mcp-server-fetch` or `uv run src/mcp_server_fetch`
- **Debug with MCP inspector**: `bunx @modelcontextprotocol/inspector uv run src/mcp_server_fetch`

## Commit Guidelines

- Write every commit message using the Conventional Commit format (e.g., `fix: guard oversized responses`).

## Git Workflow

- Do not run any git workflow commands (e.g., fetch, pull, push, merge) automatically; only execute them when explicitly requested by the user.
