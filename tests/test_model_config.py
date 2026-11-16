"""Version, credential, concurrency and local configuration security contracts."""

import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from incident_desk.model_config import ModelConfigConflict, ModelConfigStore, ModelProfileInput
from incident_desk.settings import Settings


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    return ModelConfigStore(
        Settings(_env_file=None, api_key="synthetic-imported-secret", local_dir=tmp_path)
    )


def editable(public):
    profile = public["profile"]
    return {
        key: value
        for key, value in profile.items()
        if key not in {"version", "created_at", "has_credential", "experimental"}
    } | {"expected_version": public["active_version"]}


def test_bootstrap_public_is_secret_free_and_uses_real_profile_defaults(store):
    public = store.public()
    assert public["profile"]["has_credential"] is True
    assert "synthetic-imported-secret" not in json.dumps(public)
    assert "credential_ref" not in json.dumps(public)
    assert "api_key" not in public["profile"]
    assert public["profile"]["max_output_tokens"] == 4096
    assert public["profile"]["max_request_bytes"] == 262144
    assert public["capabilities"]["reasoning"] == ["off", "low", "high", "max"]
    assert store.freeze() == store.active_version() == 1
    assert store.settings_for_version(1).api_key.get_secret_value() == "synthetic-imported-secret"
    assert store.directory.stat().st_mode & 0o777 == 0o700
    for path in store.directory.iterdir():
        assert path.stat().st_mode & 0o777 == 0o600


def test_saved_revisions_and_credentials_are_immutable_across_store_restart(store):
    before = store.settings_for_version(store.freeze())
    payload = editable(store.public()) | {
        "reasoning": "high",
        "max_output_tokens": 8192,
        "api_key": "synthetic-new-secret",
    }
    public = store.save(payload)
    assert public["active_version"] == 2
    assert "synthetic-new-secret" not in json.dumps(public)
    restarted = ModelConfigStore(store.settings)
    assert restarted.settings_for_version(1) == before
    active = restarted.settings_for_version(2)
    assert active.reasoning == "high"
    assert active.max_output_tokens == 8192
    assert active.api_key.get_secret_value() == "synthetic-new-secret"
    assert active.model_profile_version == 2
    assert restarted.settings_for_version(1).reasoning == "off"
    assert not list(store.directory.glob(".pending-*"))


def test_host_change_cannot_implicitly_send_existing_key_to_new_endpoint(store):
    payload = editable(store.public()) | {"base_url": "https://gateway.example/v1"}
    with pytest.raises(ValueError, match="requires a new API key"):
        store.save(payload)
    assert store.active_version() == 1
    public = store.save(payload | {"clear_credential": True})
    assert public["profile"]["has_credential"] is False
    assert public["profile"]["experimental"] is True
    assert public["capabilities"]["reasoning"] == ["off"]
    assert store.settings_for_version(1).api_key is not None
    assert store.settings_for_version(2).api_key is None


def test_same_origin_path_normalization_preserves_key_and_conflicts_are_rejected(store):
    payload = editable(store.public()) | {"base_url": "https://API.DEEPSEEK.COM:443/v1/"}
    assert store.save(payload)["profile"]["base_url"] == "https://api.deepseek.com/v1"
    assert store.settings_for_version(2).api_key == store.settings_for_version(1).api_key
    with pytest.raises(ModelConfigConflict):
        store.save(payload)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://api.deepseek.com",
        "https://user:password@api.deepseek.com",
        "https://api.deepseek.com?token=secret",
        "https://api.deepseek.com/#secret",
        "https://api.deepseek.com\\@evil.example",
        "https://api.deepseek.com:invalid",
    ],
)
def test_unsafe_endpoint_forms_are_rejected(endpoint):
    with pytest.raises(ValueError):
        ModelProfileInput(base_url=endpoint)


def test_unknown_provider_or_model_never_gets_fake_reasoning_support():
    with pytest.raises(ValueError):
        ModelProfileInput(provider="generic-openai")
    with pytest.raises(ValueError, match="verified only"):
        ModelProfileInput(base_url="https://gateway.example", reasoning="max")
    with pytest.raises(ValueError, match="explicitly supplied"):
        ModelProfileInput(model="unverified-model")
    with pytest.raises(ValueError):
        ModelProfileInput(project_budget_cny=301)
    with pytest.raises(ValueError):
        ModelProfileInput(pricing={"input_per_million_cny": float("inf")})


def test_concurrent_editors_cannot_overwrite_one_another(store):
    payload = editable(store.public())

    def save_revision(index):
        try:
            return store.save(payload | {"max_output_tokens": 4096 + index})["active_version"]
        except ModelConfigConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(save_revision, range(4)))
    assert sorted(map(str, results)) == ["2", "conflict", "conflict", "conflict"]
    assert len(json.loads(store.manifest.read_text())["revisions"]) == 2


def test_manifest_symlink_is_rejected_without_reading_target(store, tmp_path):
    store.directory.mkdir()
    target = tmp_path / "unrelated"
    target.write_text("private")
    store.manifest.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        store.public()
    assert target.read_text() == "private"
    assert os.path.islink(store.manifest)
