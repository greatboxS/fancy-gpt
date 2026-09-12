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
      "prepare_request","run_request_automatic","submit_planner_result","submit_final_result","get_request_status","inspect_request","list_requests","get_request_raw_response",
      "inspect_routing","inspect_context","list_sites","list_tunnel_runtimes","list_tunnel_transports","inspect_tunnel_layers",
      "list_tunnels","probe_tunnels","inspect_tunnel","select_tunnel",
      "list_skills","list_workflows","list_domains","server_stats",
      "create_session","get_session","list_sessions","close_session","create_chat","list_chats",
      "select_chat","archive_chat","list_session_requests","session_capabilities","inspect_session",
      "create_project","bootstrap_project_cycle","add_project_work_item","set_project_work_item_context","get_project_status","continue_project","retry_project_work_item",
      "get_relevant_project_context","get_project_history","start_project_session","submit_agent_outcome","finish_project_session",
      "record_project_evidence","update_project_criterion","record_project_finding","resolve_project_finding","record_project_artifact",
      "run_project_next","run_project_until_pause","ask_focused",
      "get_execution_status","list_recent_executions","start_agent_assignment"
    }
