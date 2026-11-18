"""Proxy regression tests use local SOCKS servers, never a paid cloud account."""

import asyncio
import base64
from contextlib import asynccontextmanager, suppress
import json
import os
import socket
from urllib.request import getproxies_environment

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from jdx_backend import cloud, network


@pytest.fixture(autouse=True)
def clean_proxy_environment(monkeypatch):
    for name in list(os.environ):
        if name.lower().endswith("_proxy") or name == "REQUEST_METHOD":
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(network, "getproxies", getproxies_environment)
    monkeypatch.setattr(network, "proxy_bypass", lambda host: False)


@pytest.mark.parametrize("variable,url", [
    ("ALL_PROXY", "https://cloud.example/test"),
    ("HTTPS_PROXY", "https://cloud.example/test"),
    ("all_proxy", "wss://cloud.example/test"),
    ("wss_proxy", "wss://cloud.example/test"),
])
def test_legacy_socks_scheme_normalized_without_mutating_environment(monkeypatch, variable, url):
    original = "socks://127.0.0.1:7897"
    monkeypatch.setenv(variable, original)
    before = dict(os.environ)
    assert network.proxy_for_url(url) == "socks5://127.0.0.1:7897"
    assert dict(os.environ) == before


@pytest.mark.parametrize("value", ["http://proxy.example:7897", "https://proxy.example:7897", "socks5://proxy.example:7897", "socks5h://proxy.example:7897"])
def test_supported_proxy_schemes_and_authenticated_values(value):
    assert network.normalize_proxy(value) == value
    assert network.normalize_proxy("socks://user:p%40ss@127.0.0.1:7897/") == "socks5://user:p%40ss@127.0.0.1:7897"


def test_protocol_precedence_lowercase_and_no_proxy(monkeypatch):
    monkeypatch.setenv("ALL_PROXY", "socks://127.0.0.1:7897")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7898")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:7899")
    monkeypatch.setenv("WSS_PROXY", "socks5h://127.0.0.1:7900")
    monkeypatch.setenv("NO_PROXY", ".aliyuncs.com,example.net:443,192.168.0.0/16,https://specific.example")
    assert network.proxy_for_url("https://cloud.example") == "http://127.0.0.1:7899"
    assert network.proxy_for_url("wss://cloud.example") == "socks5h://127.0.0.1:7900"
    assert network.proxy_for_url("http://cloud.example") == "socks5://127.0.0.1:7897"
    for value in ["https://a.aliyuncs.com/path", "https://aliyuncs.com", "https://example.net", "http://192.168.1.20", "https://specific.example"]:
        assert network.proxy_for_url(value) is None
    assert network.proxy_for_url("https://notaliyuncs.com") is not None
    assert network.proxy_for_url("http://example.net") is not None
    assert network.proxy_for_url("http://specific.example") is not None


@pytest.mark.parametrize("target", ["http://127.0.0.1:7897", "http://127.2.3.4", "http://[::1]:1234", "http://localhost:5555", "http://app.localhost"])
def test_loopback_bypasses_even_malformed_proxy(monkeypatch, target):
    monkeypatch.setenv("ALL_PROXY", "broken://username:proxy-secret@127.0.0.1:7897")
    assert network.proxy_for_url(target) is None


def test_wildcard_bypass_and_unrelated_invalid_proxy(monkeypatch):
    monkeypatch.setenv("ALL_PROXY", "broken://username:proxy-secret@127.0.0.1:7897")
    monkeypatch.setenv("NO_PROXY", "*")
    assert network.proxy_for_url("https://cloud.example") is None
    monkeypatch.delenv("NO_PROXY")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    assert network.proxy_for_url("https://cloud.example") == "http://127.0.0.1:7897"


@pytest.mark.parametrize("proxy", ["broken://username:proxy-secret@127.0.0.1:7897", "socks://username:proxy-secret@127.0.0.1:bad", "socks://username:proxy-secret@127.0.0.1:7897/path", "socks://username:proxy-secret@127.0.0.1:99999"])
def test_invalid_proxy_error_never_includes_secret(monkeypatch, proxy):
    monkeypatch.setenv("ALL_PROXY", proxy)
    with pytest.raises(network.ProxyConfigurationError) as caught:
        network.proxy_for_url("https://cloud.example")
    assert "proxy-secret" not in str(caught.value)
    assert "username" not in str(caught.value)
    assert "代理" in str(caught.value)


@pytest.mark.asyncio
async def test_exact_reported_proxy_constructs_supported_httpx_client(monkeypatch):
    monkeypatch.setenv("ALL_PROXY", "socks://127.0.0.1:7897")
    async with network.http_client("https://cloud.example") as client:
        assert client is not None


@pytest.mark.asyncio
async def test_reported_mixed_http_and_socks_environment_does_not_poison_https(monkeypatch):
    monkeypatch.setenv("ALL_PROXY", "socks://127.0.0.1:7897")
    monkeypatch.setenv("all_proxy", "socks://127.0.0.1:7897")
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:7897")
    assert network.proxy_for_url("https://cloud.example") == "http://127.0.0.1:7897"
    async with network.http_client("https://cloud.example") as client:
        assert client is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "websocket"])
async def test_cloud_configuration_error_is_chinese_and_not_billed(monkeypatch, transport):
    monkeypatch.setenv("ALL_PROXY", "invalid://username:proxy-secret@127.0.0.1:7897")
    with pytest.raises(cloud.CloudError) as caught:
        await cloud.synthesize(cloud.CloudConfig(workspace_id="example", api_key="api-secret"), "测试。", {}, transport=transport)
    assert caught.value.code == "INVALID_PROXY_CONFIGURATION"
    assert not caught.value.uncertain
    assert "代理" in caught.value.message
    assert not any(secret in str(caught.value) for secret in ["proxy-secret", "api-secret", "username"])


@pytest.mark.asyncio
async def test_injected_http_client_is_not_reconfigured(monkeypatch):
    monkeypatch.setenv("ALL_PROXY", "unsupported://secret@host")
    async with httpx.AsyncClient(trust_env=False, transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"output": {"voices": []}}))) as client:
        result = await cloud.check_connection(cloud.CloudConfig(workspace_id="example", api_key="api-secret", http_client=client))
        assert result["ok"]
        assert not client.is_closed


@asynccontextmanager
async def socks_server(handler, *, authenticated=False):
    """Minimal real SOCKS5 peer to validate dependency integration and framing."""
    clients = set()
    destinations = []
    failures = []

    async def handle(reader, writer):
        clients.add(asyncio.current_task())
        try:
            version, count = await reader.readexactly(2)
            assert version == 5
            methods = await reader.readexactly(count)
            method = 2 if authenticated else 0
            assert method in methods
            writer.write(bytes([5, method]))
            await writer.drain()
            if authenticated:
                version, length = await reader.readexactly(2)
                username = await reader.readexactly(length)
                length = (await reader.readexactly(1))[0]
                password = await reader.readexactly(length)
                assert username == b"user" and password == b"proxy-pass"
                writer.write(b"\x01\x00")
                await writer.drain()
            version, command, _, kind = await reader.readexactly(4)
            assert version == 5 and command == 1
            if kind == 3:
                length = (await reader.readexactly(1))[0]
                hostname = (await reader.readexactly(length)).decode()
            elif kind == 1:
                hostname = socket.inet_ntop(socket.AF_INET, await reader.readexactly(4))
            else:
                hostname = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
            port = int.from_bytes(await reader.readexactly(2), "big")
            destinations.append((hostname, port))
            writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
            await writer.drain()
            await handler(reader, writer)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures.append(exc)
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            clients.discard(asyncio.current_task())

    async with await asyncio.start_server(handle, "127.0.0.1", 0) as server:
        try:
            yield server.sockets[0].getsockname()[1], destinations
        finally:
            for task in list(clients):
                task.cancel()
            if clients:
                await asyncio.gather(*list(clients), return_exceptions=True)
    assert not failures, failures


@pytest.mark.asyncio
async def test_real_httpx_socks_alias_and_proxy_auth_handshake(monkeypatch):
    async def responder(reader, writer):
        headers = await reader.readuntil(b"\r\n\r\n")
        assert b"GET /probe HTTP/1.1" in headers
        assert b"proxy-pass" not in headers
        body = b'{"ok":true}'
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
        await writer.drain()

    async with socks_server(responder, authenticated=True) as (port, destinations):
        monkeypatch.setenv("ALL_PROXY", f"socks://user:proxy-pass@127.0.0.1:{port}")
        async with network.http_client("http://proxy-test.invalid/probe") as client:
            response = await client.get("http://proxy-test.invalid/probe")
            assert response.json() == {"ok": True}
        assert destinations == [("proxy-test.invalid", 80)]


@pytest.mark.asyncio
async def test_real_websocket_socks5h_tunnel(monkeypatch):
    async def echo(websocket):
        await websocket.send(await websocket.recv())

    async with serve(echo, "127.0.0.1", 0) as target:
        target_port = target.sockets[0].getsockname()[1]

        async def tunnel(reader, writer):
            remote_reader, remote_writer = await asyncio.open_connection("127.0.0.1", target_port)

            async def pump(source, destination):
                while value := await source.read(65536):
                    destination.write(value)
                    await destination.drain()

            jobs = [asyncio.create_task(pump(reader, remote_writer)), asyncio.create_task(pump(remote_reader, writer))]
            try:
                await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for job in jobs:
                    job.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)
                remote_writer.close()
                await remote_writer.wait_closed()

        async with socks_server(tunnel) as (port, destinations):
            monkeypatch.setenv("ALL_PROXY", f"socks5h://127.0.0.1:{port}")
            uri = f"ws://proxy-test.invalid:{target_port}"
            async with connect(uri, proxy=network.proxy_for_url(uri)) as websocket:
                await websocket.send("代理流式测试")
                assert await websocket.recv() == "代理流式测试"
            assert destinations == [("proxy-test.invalid", target_port)]


@pytest.mark.asyncio
async def test_cloud_websocket_receives_normalized_proxy(monkeypatch):
    monkeypatch.setenv("ALL_PROXY", "socks://127.0.0.1:7897")
    captured = {}

    def fail_connect(uri, **kwargs):
        captured.update(kwargs)
        raise OSError("fake proxy refused username:proxy-secret")

    monkeypatch.setattr(cloud, "websocket_connect", fail_connect)
    with pytest.raises(cloud.CloudError) as caught:
        await cloud.synthesize(cloud.CloudConfig(workspace_id="example", api_key="api-secret"), "测试。", {}, transport="websocket")
    assert captured["proxy"] == "socks5://127.0.0.1:7897"
    assert caught.value.code == "CONNECT_FAILED"
    assert "proxy-secret" not in str(caught.value)
