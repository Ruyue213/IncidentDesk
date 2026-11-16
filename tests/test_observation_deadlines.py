"""A peer sending one byte forever cannot hold the sampler or business checks forever."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

import incident_desk.observations as observations
import incident_desk.tools.business_probe as probes


@pytest.mark.asyncio
async def test_slow_drip_observations_and_business_calls_have_whole_response_deadlines(
    tmp_path, monkeypatch
):
    class Drip(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.005)
                yield b"x"

    transport = httpx.MockTransport(lambda _request: httpx.Response(200, stream=Drip()))
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs)
    )
    monkeypatch.setattr(observations, "SCRAPE_TIMEOUT_SECONDS", 0.025)
    monkeypatch.setattr(probes, "REQUEST_SECONDS", 0.025)
    store = observations.ObservationStore(tmp_path)
    sample = await asyncio.wait_for(
        store._scrape("metrics", "http://127.0.0.1:1234/metrics"), timeout=0.5
    )
    assert sample["status"] == "unavailable" and sample["error"] == "TimeoutError"
    project = SimpleNamespace(
        id="actual",
        version=1,
        health_url="http://127.0.0.1:1234/healthz",
        business_probe={
            "method": "POST",
            "path": "/query",
            "body": {"type": "dist", "a": 1, "b": 1},
            "expected_json": {"distance": 0},
        },
    )
    business = await asyncio.wait_for(probes.BusinessProbe().run(project), timeout=0.5)
    assert business["status"] == "unavailable" and not business["business_ok"]
