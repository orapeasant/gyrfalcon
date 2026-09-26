"""The dashboard designer's API publishes the same graph the webhook runs."""

import os

import pytest

pytest.importorskip("fastapi.testclient")
TEST_PG_DSN = os.environ.get("GYRFALCON_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not TEST_PG_DSN, reason="PostgreSQL test DSN required")


def test_designer_sample_runs_through_webhook(monkeypatch):
    from fastapi.testclient import TestClient

    from gyrfalcon import identity
    from gyrfalcon.flow.graphs import GraphStore
    from gyrfalcon_cli import web_server

    def authenticated(_request):
        identity._PRINCIPAL.set(identity.LOCAL)
        return identity.LOCAL

    monkeypatch.setattr(web_server, "_verify_token", authenticated)
    monkeypatch.setattr(web_server, "_graph_store", lambda: GraphStore(dsn=TEST_PG_DSN))
    client = TestClient(web_server.app)
    try:
        sample = client.post("/api/flow/graphs/sample")
        assert sample.status_code == 200, sample.text
        definition_id = sample.json()["id"]
        graph = client.get(f"/api/flow/graphs/{definition_id}").json()
        assert len(graph["draft"]["nodes"]) == 5
        assert graph["published_version"] == 1

        result = client.post(
            f"/api/webhooks/flows/{definition_id}", json={"inputs": {"name": "Ada"}}
        )
        assert result.status_code == 200, result.text
        assert result.json()["result"]["message"].endswith("hello5")

        # The designer saves a changed draft, publishes it, and the invoke
        # route executes that new version while v1 remains available.
        edited = graph["draft"]
        edited["nodes"] = edited["nodes"][:2]
        edited["edges"] = edited["edges"][:1]
        assert client.put(
            f"/api/flow/graphs/{definition_id}/draft", json={"draft": edited}
        ).status_code == 200
        published = client.post(f"/api/flow/graphs/{definition_id}/publish")
        assert published.json()["version"] == 2
        revised = client.post(
            f"/api/flow/graphs/{definition_id}/invoke", json={"inputs": {"name": "Ada"}}
        )
        assert revised.json()["result"]["message"].endswith("hello2")
        assert client.delete(f"/api/flow/graphs/{definition_id}").status_code == 200
        assert client.get(f"/api/flow/graphs/{definition_id}").status_code == 404
    finally:
        client.close()


def test_designer_requires_admin_or_developer_but_webhook_can_run(monkeypatch):
    from fastapi.testclient import TestClient

    from gyrfalcon import identity
    from gyrfalcon.flow.graphs import GraphStore
    from gyrfalcon_cli import web_server

    active_role = ["admin"]

    def authenticated(_request):
        principal = identity.Principal(
            user_id="caller", tenant_id="local", roles=frozenset({active_role[0]})
        )
        identity._PRINCIPAL.set(principal)
        return principal

    monkeypatch.setattr(web_server, "_verify_token", authenticated)
    monkeypatch.setattr(web_server, "_graph_store", lambda: GraphStore(dsn=TEST_PG_DSN))
    client = TestClient(web_server.app)
    try:
        definition_id = client.post("/api/flow/graphs/sample").json()["id"]
        for role in ("admin", "system_admin", "app_developer", "operator"):
            active_role[0] = role
            assert client.get("/api/flow/graphs").status_code == 200

        active_role[0] = "viewer"
        assert client.get("/api/flow/graphs").status_code == 403
        assert client.get("/api/flow/graphs/activities").status_code == 403
        assert client.post("/api/flow/graphs", json={"name": "blocked", "draft": {}}).status_code == 403
        assert client.post(f"/api/flow/graphs/{definition_id}/publish").status_code == 403
        assert client.delete(f"/api/flow/graphs/{definition_id}").status_code == 403
        result = client.post(f"/api/webhooks/flows/{definition_id}", json={"inputs": {"name": "Ada"}})
        assert result.status_code == 200
    finally:
        client.close()
