"""Smoke test — verifies the agent module imports and exposes create_agent."""

import importlib


def test_factory_imports():
    factory = importlib.import_module("src.agent.factory")
    assert hasattr(factory, "create_agent"), "factory must expose create_agent()"


def test_mcp_module_imports():
    mcp = importlib.import_module("src.tools.mcp")
    assert hasattr(mcp, "build_mcp_tools"), "mcp module must expose build_mcp_tools()"


def test_prompts_and_specialists_load():
    prompts = importlib.import_module("src.agent.prompts")
    # Orchestrator prompt loads and carries the date suffix.
    orch = prompts.get_system_prompt()
    assert "orchestrator" in orch.lower()
    assert "Today's date is" in orch
    # Every specialist in the roster has a loadable, non-empty prompt.
    assert len(prompts.SPECIALISTS) == 5
    for name, description, prompt_file in prompts.SPECIALISTS:
        assert name and description and prompt_file
        body = prompts.load_specialist_prompt(prompt_file)
        assert body.strip(), f"specialist prompt {prompt_file} is empty"


def test_factory_builds_subagents():
    factory = importlib.import_module("src.agent.factory")
    # No sandbox -> the 5 static specialists, no verifier.
    subs = factory._build_subagents(pr_tool=None)
    names = {s["name"] for s in subs}
    assert "security-reviewer" in names and "correctness-reviewer" in names
    assert "verification-reviewer" not in names
    assert len(subs) == len(factory.SPECIALISTS)
    for s in subs:
        assert s["system_prompt"].strip()
        assert "tools" in s  # explicit (empty here since pr_tool is None)


def test_verifier_added_only_with_sandbox():
    factory = importlib.import_module("src.agent.factory")
    prompts = importlib.import_module("src.agent.prompts")
    # A truthy stand-in for the sandbox tool toggles the verifier on.
    sentinel = object()
    subs = factory._build_subagents(pr_tool=None, ci_tool=sentinel)
    verifier = next((s for s in subs if s["name"] == "verification-reviewer"), None)
    assert verifier is not None, "verifier must appear when a sandbox is provisioned"
    assert sentinel in verifier["tools"], "verifier must get the execute_code tool"
    assert len(subs) == len(factory.SPECIALISTS) + 1
    # The verifier prompt loads and is about running the code.
    body = prompts.load_specialist_prompt(prompts.VERIFIER[2])
    assert "execute_code" in body and "git" in body


def test_capability_modules_import():
    ci = importlib.import_module("src.tools.code_interpreter")
    br = importlib.import_module("src.tools.browser")
    mem = importlib.import_module("src.tools.memory")
    sf = importlib.import_module("src.tools.share_file")
    rc = importlib.import_module("src.tools.request_connection")
    gh = importlib.import_module("src.tools.github_pr")
    assert hasattr(ci, "build_code_interpreter_tool")
    assert hasattr(br, "build_browser_tool")
    assert hasattr(mem, "build_memory_tool")
    assert hasattr(sf, "build_share_file_tool")
    assert hasattr(rc, "build_request_connection_tool")
    assert hasattr(gh, "build_github_pr_tool")
