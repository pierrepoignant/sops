# SOP MCP

SOP exposes a [Model Context Protocol](https://modelcontextprotocol.io) server so an AI
assistant (Claude on claude.ai, Claude Desktop, Claude Code, Antigravity, or any MCP client)
can read the procedures of **one brand** on behalf of a logged-in user. Same design, same
gestures as the DataSab and PIM servers.

**Endpoints:** `https://sops.essenciagua.fr/mcp` · `https://sops.gourmiz.fr/mcp` ·
`https://sops.sablesienne.com/mcp` (Streamable HTTP, stateless). A host shows one brand,
so a connection opens that brand and nothing else; two brands means two connections.

## Connecting

Each user connects once from their own client; the browser opens the SOP login (Google, or
the e-mail code), then a consent page naming the client and the brand. Nothing is typed by
hand (no key, no secret). Access tokens last 1 hour and are refreshed silently for up to
90 days; after that, or after a revocation, the client asks to reconnect.

- **claude.ai / Claude Desktop** — Settings → Connectors → *Add custom connector* →
  URL `https://sops.sablesienne.com/mcp` → *Connect*, then approve in the browser window.
- **Claude Code** — `claude mcp add --transport http sops-sablesienne https://sops.sablesienne.com/mcp`,
  then `/mcp` inside Claude Code to authenticate.
- **Antigravity** — Agent panel → `…` → *MCP Servers* → *Manage MCP Servers* →
  *View raw config* (`~/.gemini/antigravity/mcp_config.json`):

  ```json
  { "mcpServers": { "sops-sablesienne": { "serverUrl": "https://sops.sablesienne.com/mcp" } } }
  ```

  A client that cannot do OAuth by itself goes through the `mcp-remote` bridge:
  `"command": "npx", "args": ["-y", "mcp-remote", "https://sops.sablesienne.com/mcp", "--transport", "http-only"]`.
- Any other MCP client: same URL; the client discovers the OAuth endpoints by itself.

Who may connect: anyone who can log in on the host — the login already enforces the brand's
e-mail domains (`brands.py`).

## What the assistant can do

Read-only tools, all on the brand of the connection:

| Tool | What it returns |
|---|---|
| `whoami` | the connected user, the brand, the list of tools |
| `list_departments` | the departments, with their count of published procedures |
| `list_categories` | the category tree of a department, with the procedures of each |
| `search_sops` | the procedures matching a few words, best first, with a snippet |
| `get_sop` | one procedure in full: text, department, category, attachments, version, last review |
| `recent_changes` | the procedures changed in the last days |

Only published procedures are visible. There is no write tool by this channel.

## Administration

**Administration → Connexions IA** lists every connection (user, client, brand, last call),
lets an admin revoke one, and shows the last 50 tool calls with their arguments and
duration (table `mcp_calls`).

## How it works (for developers)

`mcp_server/` — one Flask blueprint:

- `oauth.py` — OAuth 2.1 authorization server as the MCP spec requires from a remote server:
  discovery (`/.well-known/oauth-protected-resource`, `/.well-known/oauth-authorization-server`),
  dynamic client registration (`POST /oauth/register`), authorization code + PKCE S256
  (`/oauth/authorize`, consent page with its nonce), `POST /oauth/token` (code and refresh
  grants, refresh rotation in place), `POST /oauth/revoke`. The user identity comes from the
  normal SOP login; `auth.login` accepts `?next=` to return to the consent page. Tokens are
  opaque, stored hashed (`oauth_clients`, `oauth_codes`, `oauth_tokens`), and carry the brand
  of the host they were issued on.
- `protocol.py` — `POST /mcp`: JSON-RPC 2.0 (`initialize` with protocol version
  negotiation, `ping`, `tools/list`, `tools/call`; `resources/*` and `prompts/*` answer
  empty; batches accepted). No session id, no SSE (`GET /mcp` is 405). A token for another
  brand than the host's gets 403. Every tool call is logged.
- `tools.py` — the registry (`@tool(name, …)`) and the tools, `fn(token, **args)`.
- `admin.py` — the « Connexions IA » screen, on the administration blueprint.

The tables are created by `db.create_all()` at start-up, like the rest of the schema.

Local check:

```
curl -s https://sops.sablesienne.com/.well-known/oauth-protected-resource
curl -s -X POST https://sops.sablesienne.com/mcp -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'      # 401 + WWW-Authenticate
```
