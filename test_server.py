"""Offline regression check. Run with the dependencies declared in server.py."""
import asyncio
import base64
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from starlette.testclient import TestClient


def rejects(error=ValueError):
    return unittest.TestCase().assertRaises(error)


async def check(s, root):
    requests = []

    def upstream(request):
        requests.append(request)
        if request.url.path.endswith('/redirect'):
            return httpx.Response(302, headers={'location': 'https://elsewhere.example/download'})
        if request.url.path.endswith('/error'):
            return httpx.Response(403, json={'error': 'denied'})
        if request.url.path.endswith('/download'):
            return httpx.Response(200, content=b'file data', headers={'content-type': 'application/octet-stream'})
        return httpx.Response(200, json={'ok': True})

    assert s.client.follow_redirects is False
    await s.client.aclose()
    s.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    op = s.ALL_OPS['get_item']
    assert len(await s.list_tools()) == 4
    await s.call_tool_impl('silo_call_operation', {'operation_id': op.id, 'arguments': {'id': 'a/b', 'limit': 2, 'headers': {'Range': 'bytes=0-10'}}})
    assert requests[-1].url.raw_path.startswith(b'/items/a%2Fb?')
    assert requests[-1].headers['authorization'] == 'Bearer fake-api-key'
    assert requests[-1].url.params['limit'] == '2'
    for bad in ({}, {'id': '..'}, {'id': 'x', 'limit': '2'}, {'id': 'x', 'extra': True}, {'id': 'x', 'headers': {'Host': 'evil.example'}}, {'id': 'x', 'headers': {'authorization': 'override'}}, {'id': 'x', 'save_to': 'file'}):
        before = len(requests)
        with rejects():
            await s.execute(op, bad)
        assert len(requests) == before
    for bad in ({'limit': -1}, {'offset': -1}, {'limit': 101}, {'limit': True}):
        with rejects():
            await s.call_tool_impl('silo_search_operations', bad)
    with rejects():
        await s.call_tool_impl('silo_call_operation', {'operation_id': 'create_item', 'arguments': {'body': {'name': 'x'}}})
    with rejects():
        await s.execute(s.ALL_OPS['create_item'], {'body': {'name': 'x'}})
    with patch.object(s, 'MODE', 'full'):
        assert all(t.annotations.readOnlyHint for t in await s.list_tools())
        await s.call_tool_impl('get_item', {'id': 'x'})
        with rejects():
            await s.call_tool_impl('get_item', {'id': 'x', 'limit': 'bad'})
    with rejects(RuntimeError):
        await s.execute(s.ALL_OPS['redirect'], {})
    assert requests[-1].url.host == 'silo.example.com'

    with patch.object(s, 'READONLY', False):
        create = s.ALL_OPS['create_item']
        with rejects():
            await s.execute(create, {})
        await s.execute(create, {'body': {'name': 'new'}})
        assert json.loads(requests[-1].content) == {'name': 'new'}
        nullable = s.Op('/nullable', 'post', {'requestBody': {'required': True, 'content': {
            'application/json': {'schema': {'type': ['object', 'null']}},
        }}}, [])
        await s.execute(nullable, {'body': None})
        assert requests[-1].content == b'null'
        assert s.annotations(create).destructiveHint
        await s.execute(s.ALL_OPS['upload'], {'body': {'file': {'content_base64': base64.b64encode(b'hello').decode(), 'filename': 'hello.txt'}}})
        assert b'hello' in requests[-1].content
        assert requests[-1].headers['content-type'].startswith('multipart/form-data;')
        with rejects():
            await s.execute(s.ALL_OPS['upload'], {'body': {'file': {'content_base64': '%%%'}}})
        with rejects():
            await s.execute(s.ALL_OPS['raw'], {'body': {'path': 'secret'}})
        await s.execute(s.ALL_OPS['raw'], {'body': {'base64': 'aGk='}})
        assert requests[-1].content == b'hi'
        with patch.object(s, 'MAX_FILE_BYTES', 1), rejects():
            await s.execute(s.ALL_OPS['raw'], {'body': {'base64': 'aGk='}})

    files = root / 'files'
    files.mkdir()
    (root / 'secret').write_text('private')
    (files / 'escape').symlink_to(root, target_is_directory=True)
    with patch.object(s, 'FILES_DIR', files):
        for path in ('../secret', str(root / 'secret'), 'escape/secret', '.'):
            with rejects():
                s.local_file(path)
        await s.execute(s.ALL_OPS['download'], {'save_to': 'new.bin'})
        assert (files / 'new.bin').read_bytes() == b'file data'
        with rejects(FileExistsError):
            await s.execute(s.ALL_OPS['download'], {'save_to': 'new.bin'})
        assert (files / 'new.bin').read_bytes() == b'file data'
        with rejects(RuntimeError):
            await s.execute(s.ALL_OPS['error'], {'save_to': 'error.json'})
        assert not (files / 'error.json').exists()
        with patch.object(s, 'MAX_FILE_BYTES', 2), rejects():
            await s.execute(s.ALL_OPS['download'], {'save_to': 'partial.bin'})
        assert not (files / 'partial.bin').exists()
        with patch.object(s, 'READONLY', False):
            await s.execute(s.ALL_OPS['raw'], {'body': {'path': 'new.bin'}})
            assert requests[-1].content == b'file data'

    response = httpx.Response(200, content=b'a' * 100)
    assert await s.read_limited(response, 10) == (b'a' * 10, False)
    with patch.object(s, 'MAX_CHARS', 20):
        response = httpx.Response(200, content=b'data: ' + b'a' * 100, headers={'content-type': 'text/event-stream'}, request=httpx.Request('GET', 'https://silo.example.com'))
        content, _ = await s.render(response, None)
        assert '[truncated' in content[0].text
    s.SPEC['components'] = {'schemas': {'Loop': {'$ref': '#/components/schemas/Loop'}}}
    with rejects():
        s.shallow({'$ref': '#/components/schemas/Loop'})
    assert s.deref({'$ref': '#/components/schemas/Loop'})['type'] == 'object'
    await s.client.aclose()

    # A second server must not silently reuse the first server's cached spec.
    with patch.dict(os.environ, {'SILO_OPENAPI_FILE': '', 'SILO_CACHE_DIR': str(root / 'cache')}):
        response = httpx.Response(200, json=s.SPEC, request=httpx.Request('GET', s.BASE))
        with patch.object(httpx, 'get', return_value=response):
            assert s.load_spec()['paths'] == s.SPEC['paths']
        with patch.object(httpx, 'get', side_effect=httpx.ConnectError('offline')):
            assert s.load_spec()['paths'] == s.SPEC['paths']
            with patch.dict(os.environ, {'SILO_OPENAPI_URL': 'https://other.example/spec.json'}), rejects(SystemExit):
                s.load_spec()

    # Exercise the real stdio protocol against a separate server process.
    params = StdioServerParameters(command=sys.executable, args=[str(Path(s.__file__).resolve())], env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            assert len((await session.list_tools()).tools) == 4
            result = await session.call_tool('silo_describe_operation', {'operation_id': 'get_item'})
            assert not result.isError
            result = await session.call_tool('silo_call_operation', {'operation_id': 'create_item'})
            assert result.isError


def check_http(s):
    with patch.dict(os.environ, {'SILO_MCP_AUTH_TOKEN': ''}), rejects():
        s.http_app()
    with patch.dict(os.environ, {'SILO_MCP_AUTH_TOKEN': 'x' * 32, 'SILO_MCP_PATH': 'auto'}), rejects():
        s.http_app()
    with patch.dict(os.environ, {'SILO_MCP_AUTH_TOKEN': 'x' * 32}):
        with TestClient(s.http_app(), base_url='http://localhost') as client:
            assert client.get('/healthz').status_code == 200
            assert client.post('/mcp/').status_code == 401
            assert client.post('/mcp/', headers={'Authorization': 'Bearer wrong'}).status_code == 401
            headers = {'Authorization': 'Bearer ' + 'x' * 32, 'Accept': 'application/json, text/event-stream'}
            payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}}}
            assert client.post('/mcp/', json=payload, headers={**headers, 'Host': 'evil.example'}).status_code == 421
            assert client.post('/mcp/', json=payload, headers={**headers, 'Origin': 'https://evil.example'}).status_code == 403
            assert client.post('/mcp/', content=b'x' * (4 * 1024 * 1024 + 1), headers={**headers, 'Content-Type': 'application/json'}).status_code == 413
            response = client.post('/mcp/', json=payload, headers=headers)
            assert response.status_code == 200, response.text
            assert 'serverInfo' in response.text
            payload = {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}
            response = client.post('/mcp/', json=payload, headers=headers)
            assert response.status_code == 200 and 'silo_call_operation' in response.text
            payload = {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'silo_call_operation', 'arguments': {'operation_id': 'get_item', 'arguments': {'id': 'x', 'limit': 'bad'}}}}
            response = client.post('/mcp/', json=payload, headers=headers)
            assert response.status_code == 200 and '"isError":true' in response.text


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        spec = {'openapi': '3.1.0', 'info': {'title': 'Offline test', 'version': '1'}, 'paths': {
            '/items/{id}': {'get': {'operationId': 'get_item', 'parameters': [
                {'in': 'path', 'name': 'id', 'required': True, 'schema': {'type': 'string'}},
                {'in': 'query', 'name': 'limit', 'schema': {'type': 'integer', 'minimum': 1}},
            ]}},
            '/items': {'post': {'operationId': 'create_item', 'requestBody': {'required': True, 'content': {'application/json': {'schema': {'type': 'object', 'properties': {'name': {'type': 'string'}}, 'required': ['name']}}}}}},
            '/upload': {'post': {'operationId': 'upload', 'requestBody': {'required': True, 'content': {'multipart/form-data': {'schema': {'type': 'object', 'properties': {'file': {'type': 'string', 'format': 'binary'}}, 'required': ['file']}}}}}},
            '/raw': {'post': {'operationId': 'raw', 'requestBody': {'required': True, 'content': {'application/octet-stream': {'schema': {'type': 'string', 'format': 'binary'}}}}}},
            **{f'/{name}': {'get': {'operationId': name}} for name in ('redirect', 'download', 'error')},
        }}
        spec_file = root / 'spec.json'
        spec_file.write_text(json.dumps(spec))
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith('SILO_')}
        clean_env.update(SILO_BASE_URL='https://silo.example.com', SILO_TOKEN='fake-api-key', SILO_OPENAPI_FILE=str(spec_file))
        with patch.dict(os.environ, clean_env, clear=True):
            module = importlib.util.spec_from_file_location('silo_server_under_test', Path(__file__).with_name('server.py'))
            server = importlib.util.module_from_spec(module)
            module.loader.exec_module(server)
            asyncio.run(check(server, root))
            check_http(server)
    print('All offline regression checks passed (API mapping, safety boundaries, stdio, HTTP).')


if __name__ == '__main__':
    main()
