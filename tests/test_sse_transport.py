from micro_agent.tool.mcp.sse_transport import resolve_sse_endpoint


def test_resolve_under_mcp_proxy_prefix():
    sse = "https://fdueblab.cn/mcp-proxy/18000/sse"
    assert resolve_sse_endpoint(sse, "/messages/?session_id=abc") == (
        "https://fdueblab.cn/mcp-proxy/18000/messages/?session_id=abc"
    )


def test_resolve_direct_port():
    sse = "http://127.0.0.1:18000/sse"
    assert resolve_sse_endpoint(sse, "/messages/?session_id=abc") == (
        "http://127.0.0.1:18000/messages/?session_id=abc"
    )


def test_resolve_relative_endpoint():
    sse = "https://fdueblab.cn/mcp-proxy/18004/sse"
    assert resolve_sse_endpoint(sse, "messages/?session_id=x") == (
        "https://fdueblab.cn/mcp-proxy/18004/messages/?session_id=x"
    )
