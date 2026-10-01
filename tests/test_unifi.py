"""UniFi provider: update semantics against the static-dns API."""

import json

import httpx
import pytest
import respx

from traefik_dns_sync.config import UnifiConfig
from traefik_dns_sync.models import DnsRecord
from traefik_dns_sync.providers.unifi import UnifiProvider

BASE = "https://unifi.test/proxy/network/v2/api/site/default/static-dns"


def make_provider() -> UnifiProvider:
    return UnifiProvider(UnifiConfig(host="unifi.test", api_key="k"))


@pytest.mark.asyncio
@respx.mock
async def test_update_sends_id_so_unifi_does_not_create_a_new_record():
    """UniFi's PUT without _id in the body creates a new record (verified on UCG Fiber)."""
    respx.get(BASE).mock(return_value=httpx.Response(200, json=[
        {"_id": "a1", "key": "app.example.se", "record_type": "A", "value": "10.0.0.1"},
    ]))
    put = respx.put(f"{BASE}/a1").mock(return_value=httpx.Response(200, json={}))

    await make_provider().update_record(DnsRecord.from_fqdn("app.example.se", "10.0.0.2"))

    body = json.loads(put.calls.last.request.content)
    assert body["_id"] == "a1"
    assert body["value"] == "10.0.0.2"


@pytest.mark.asyncio
@respx.mock
async def test_update_keeps_record_that_already_has_the_ip():
    """UniFi rejects a PUT that duplicates another record's key+value (400), so when one
    record already has the desired IP it is kept and the others are deleted — no PUT."""
    respx.get(BASE).mock(return_value=httpx.Response(200, json=[
        {"_id": "a1", "key": "app.example.se", "record_type": "A", "value": "10.0.0.1"},
        {"_id": "t1", "key": "_tdns.a-app.example.se", "record_type": "TXT", "value": "x"},
        {"_id": "a2", "key": "app.example.se", "record_type": "A", "value": "10.0.0.2"},
    ]))
    put = respx.put(url__startswith=BASE).mock(return_value=httpx.Response(400, json={}))
    delete = respx.delete(f"{BASE}/a1").mock(return_value=httpx.Response(200, json={}))

    await make_provider().update_record(DnsRecord.from_fqdn("app.example.se", "10.0.0.2"))

    assert not put.called
    assert delete.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_update_puts_one_and_deletes_other_duplicates():
    respx.get(BASE).mock(return_value=httpx.Response(200, json=[
        {"_id": "a1", "key": "app.example.se", "record_type": "A", "value": "10.0.0.1"},
        {"_id": "a2", "key": "app.example.se", "record_type": "A", "value": "10.0.0.3"},
    ]))
    put = respx.put(f"{BASE}/a1").mock(return_value=httpx.Response(200, json={}))
    delete = respx.delete(f"{BASE}/a2").mock(return_value=httpx.Response(200, json={}))

    await make_provider().update_record(DnsRecord.from_fqdn("app.example.se", "10.0.0.2"))

    assert json.loads(put.calls.last.request.content)["_id"] == "a1"
    assert delete.called


@pytest.mark.asyncio
@respx.mock
async def test_update_prefers_known_provider_id():
    respx.get(BASE).mock(return_value=httpx.Response(200, json=[
        {"_id": "a1", "key": "app.example.se", "record_type": "A", "value": "10.0.0.1"},
        {"_id": "a2", "key": "app.example.se", "record_type": "A", "value": "10.0.0.2"},
    ]))
    put = respx.put(f"{BASE}/a2").mock(return_value=httpx.Response(200, json={}))
    delete = respx.delete(f"{BASE}/a1").mock(return_value=httpx.Response(200, json={}))

    await make_provider().update_record(DnsRecord.from_fqdn("app.example.se", "10.0.0.3"), "a2")

    assert put.called
    assert delete.called
