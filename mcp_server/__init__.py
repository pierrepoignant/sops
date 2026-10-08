"""MCP server for SOPs — lets an AI assistant (Claude, Claude Code, Antigravity,
any MCP client) read the procedures of one brand on behalf of a logged-in user.

One blueprint, three parts — the same shape as DataSab's and the PIM's:

- ``oauth.py``    — the OAuth 2.1 authorization server MCP clients expect from
                    a remote server: discovery documents, dynamic client
                    registration, authorization code + PKCE, refresh tokens.
                    The user authenticates with the existing login (Google or
                    e-mail code); the tokens issued here are SOPs' own and map
                    to a user *and a brand* — the brand of the host the
                    consent was given on, since a host shows one brand only.
- ``protocol.py`` — the MCP Streamable HTTP endpoint (``POST /mcp``,
                    JSON-RPC 2.0): initialize, tools/list, tools/call, with
                    every tool call logged.
- ``tools.py``    — the tools themselves. Read-only: departments, categories,
                    search, one procedure in full.

The blueprint name is not a module id on purpose: the permission hook lets
it through, and a procedure is readable by every user of the brand.
"""
from flask import Blueprint

mcp_bp = Blueprint('mcp', __name__, template_folder='templates')

from . import models, oauth, protocol, admin  # noqa: E402, F401
