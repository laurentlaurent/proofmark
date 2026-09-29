import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from proofmark.server import app


@pytest.fixture
def client():
    return TestClient(app)


def test_config_lists_providers_samples_and_doc_types(client):
    data = client.get("/api/config").json()
    assert data["default_provider"] == "demo"
    assert {p["id"] for p in data["providers"]} == {"demo", "gemini", "groq", "ollama", "custom"}
    assert {s["id"] for s in data["samples"]} == {"deposit-rescue", "streak-shield"}
    assert len(data["doc_types"]) == 5 and data["kb_articles"] == 6
    assert "api_key" not in json.dumps(data)


def test_samples_include_screenshots_as_data_urls(client):
    sample = client.get("/api/samples/deposit-rescue").json()
    assert sample["sources"]["screenshots"][0].startswith("data:image/png;base64,")
    assert client.get("/api/samples/nope").status_code == 404


def test_full_flow_in_demo_mode(client):
    sources = client.get("/api/samples/deposit-rescue").json()["sources"]
    extracted = client.post("/api/extract", json={"sources": sources, "llm": {"provider": "demo"}}).json()
    assert extracted["facts"]["feature_name"] == "Deposit Rescue"
    assert "Demo mode does not read screenshots" in extracted["warnings"][0]

    drafts = []
    for doc_type in ("help_article", "faq", "agent_brief"):
        response = client.post("/api/generate", json={"facts": extracted["facts"], "doc_type": doc_type,
                                                      "llm": {"provider": "demo"}, "sources": sources})
        assert response.status_code == 200
        drafts.append(response.json())

    checked = client.post("/api/check", json={"doc_type": "help_article", "facts": extracted["facts"],
                                              "markdown": "# Hi\n\n<script>alert(1)</script>Project Safety Net"}).json()
    assert "<script" not in checked["html"]
    assert next(c for c in checked["checks"] if c["id"] == "jargon")["status"] == "fail"

    exported = client.post("/api/export", json={"facts": extracted["facts"], "drafts": drafts})
    assert exported.headers["content-disposition"] == 'attachment; filename="deposit-rescue-docs.zip"'
    names = set(zipfile.ZipFile(io.BytesIO(exported.content)).namelist())
    assert {"deposit-rescue/facts.json", "deposit-rescue/help_article.md", "deposit-rescue/html/faq.html",
            "deposit-rescue/check-report.md", "deposit-rescue/zendesk_articles.json"} <= names
    zendesk = json.loads(zipfile.ZipFile(io.BytesIO(exported.content)).read("deposit-rescue/zendesk_articles.json"))
    assert len(zendesk) == 2 and zendesk[0]["article"]["draft"] is True  # agent brief stays internal


def test_errors_come_back_as_readable_messages(client):
    empty = client.post("/api/extract", json={"sources": {"spec": ""}})
    assert empty.status_code == 400 and "Add a feature spec" in empty.json()["error"]

    no_key = client.post("/api/extract", json={"sources": {"spec": "# X"}, "llm": {"provider": "gemini"}})
    assert no_key.status_code == 400 and "GEMINI_API_KEY" in no_key.json()["error"]

    unreachable = client.post("/api/extract", json={
        "sources": {"spec": "# X"},
        "llm": {"provider": "custom", "base_url": "http://127.0.0.1:9/v1", "model": "m"}})
    assert unreachable.status_code == 502 and "Could not reach" in unreachable.json()["error"]

    assert client.post("/api/export", json={"facts": {}, "drafts": []}).status_code == 400
