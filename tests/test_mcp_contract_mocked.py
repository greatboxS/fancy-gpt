from __future__ import annotations
import importlib, sys, types


def test_mcp_tool_surface_can_be_registered_without_real_transport(monkeypatch):
    registered=[]
    class FakeMCPServer:
        def __init__(self,name): assert name=="fancy-gpt"
        def tool(self):
            def deco(func): registered.append(func.__name__); return func
            return deco
        def run(self): return None
    pkg=types.ModuleType("mcp"); mod=types.ModuleType("mcp.server"); mod.MCPServer=FakeMCPServer
    monkeypatch.setitem(sys.modules,"mcp",pkg); monkeypatch.setitem(sys.modules,"mcp.server",mod); sys.modules.pop("fancy_gpt.mcp_server",None)
    module=importlib.import_module("fancy_gpt.mcp_server"); assert module.mcp is not None
    assert set(registered)=={
      "prepare_request","run_request_automatic","submit_planner_result","submit_final_result","get_request_status",
      "inspect_routing","inspect_context","list_tunnel_sites","list_tunnel_runtimes","list_tunnel_transports","inspect_tunnel_layers",
      "list_tunnels","probe_tunnels","inspect_tunnel","select_tunnel",
      "list_skills","list_workflows","list_domains"
    }
