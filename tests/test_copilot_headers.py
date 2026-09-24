"""Copilot's IDE headers must accompany every agent API request."""

from types import SimpleNamespace

from gyrfalcon.run_agent import AIAgent


def test_copilot_headers_are_inferred_from_gateway_base_url():
    class Completions:
        kwargs = None

        def create(self, **kwargs):
            self.kwargs = kwargs
            return object()

    completions = Completions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    agent = object.__new__(AIAgent)
    agent.provider = None
    agent.base_url = "https://api.githubcopilot.com"
    agent.model = "gpt-4o"
    agent._system_message = "system"
    agent._conversation_history = [{"role": "user", "content": "hello"}]
    agent.max_tokens = None
    agent.temperature = None
    agent.service_tier = None
    agent.reasoning_config = None
    agent.stream_delta_callback = None

    agent._openai_api_call(client, tools=None)

    headers = completions.kwargs["extra_headers"]
    assert headers["Editor-Version"] == "vscode/1.96.0"
    assert headers["Editor-Plugin-Version"] == "copilot-chat/0.24.0"
