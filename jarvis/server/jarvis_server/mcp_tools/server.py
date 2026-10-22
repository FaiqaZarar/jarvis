"""Shared MCPServer instance for the MCP tool layer.

Every MCP tool registers onto this one `server` object: `search` today
(J-07), and `get_hierarchy` / `browse` / `related` when J-10 lands. Keeping
the instance here - not inside any single tool module - means a later
ticket never has to untangle and consolidate several independently-created
MCPServer instances into one; the tools already share this.
"""

from mcp.server.mcpserver import MCPServer

server = MCPServer(name="jarvis")
