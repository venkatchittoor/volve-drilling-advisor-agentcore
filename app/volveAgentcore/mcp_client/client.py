import os
import logging
from mcp.client.streamable_http import streamablehttp_client
from strands.tools.mcp.mcp_client import MCPClient

logger = logging.getLogger(__name__)

# AgentCore Gateway MCP endpoint — set after first deploy
MCP_ENDPOINT = os.getenv("SILVER_TABLE_MCP_ENDPOINT", "")

def get_streamable_http_mcp_client():
    """Returns MCP Client for Silver table reader via AgentCore Gateway.
    Returns None if endpoint not yet configured — agent runs without MCP tools.
    """
    if not MCP_ENDPOINT:
        logger.warning("SILVER_TABLE_MCP_ENDPOINT not set — MCP tools disabled")
        return None
    return MCPClient(lambda: streamablehttp_client(MCP_ENDPOINT))
