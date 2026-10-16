"""Test-server stop wakeups retain Uvicorn's real request and lifespan drain."""
import asyncio
import ast
from contextlib import asynccontextmanager
import inspect
import socket
import threading
import textwrap

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
import uvicorn

pytestmark = pytest.mark.unit


def server_type():
    from tests.loopback_server import LoopbackServer
    return LoopbackServer


def test_stop_adapter_retains_stock_lifecycle_and_pinned_tick_semantics():
    import importlib.metadata
    assert importlib.metadata.version("uvicorn") == "0.54.0"
    adapter = server_type()
    for name in ("serve", "_serve", "startup", "on_tick", "shutdown", "_wait_tasks_to_complete"):
        assert getattr(adapter, name) is getattr(uvicorn.Server, name)
    stock = inspect.getsource(uvicorn.Server.main_loop)
    assert "counter = 0" in stock and "counter = counter % 864000" in stock
    assert "await asyncio.sleep(0.1)" in stock
    assert stock.count("await self.on_tick(counter)") == 2
    # Normalize only the explicit idle-wait adapter and loop bookkeeping.
    stock_ast = ast.parse(textwrap.dedent(stock)).body[0]
    adapter_ast = ast.parse(textwrap.dedent(inspect.getsource(adapter.main_loop))).body[0]
    assert ast.unparse(adapter_ast.body[0]) == "self._serving_loop = asyncio.get_running_loop()"
    adapted = adapter_ast.body[1:]
    assert isinstance(adapted[-1].body[2], ast.Try)
    adapted[-1].body[2] = stock_ast.body[-1].body[2]
    assert [ast.dump(node) for node in adapted] == [ast.dump(node) for node in stock_ast.body]
    from tests.contract import test_mcp
    fixture = inspect.getsource(test_mcp.running)
    assert "await asyncio.wait_for(task,10)" in fixture
    assert "runner.request_exit()" in fixture


def test_idle_stop_is_per_server_and_thread_safe():
    async def scenario():
        first = server_type()(uvicorn.Config(lambda scope, receive, send: None))
        second = server_type()(uvicorn.Config(lambda scope, receive, send: None))
        assert first._stop_requested is not second._stop_requested
        waiting = asyncio.Event()
        ticks = []

        async def tick(counter):
            ticks.append(counter)
            waiting.set()
            return first.should_exit

        first.on_tick = tick
        task = asyncio.create_task(first.main_loop())
        try:
            await asyncio.wait_for(waiting.wait(), 1)
            thread = threading.Thread(target=first.request_exit)
            thread.start()
            thread.join(timeout=1)
            assert not thread.is_alive()
            await asyncio.wait_for(task, 1)
            assert ticks == [0, 1]
            assert first.should_exit and first._stop_requested.is_set()
            assert not second.should_exit and not second._stop_requested.is_set()
        finally:
            first.request_exit()
            await asyncio.wait_for(task, 1)
    asyncio.run(scenario())


def test_normal_tick_waits_keep_the_stock_cadence_and_counter(monkeypatch):
    from tests import loopback_server
    async def scenario():
        runner = server_type()(uvicorn.Config(lambda scope, receive, send: None))
        ticks, timeouts = [], []

        async def tick(counter):
            ticks.append(counter)
            return counter == 3

        async def timeout(awaitable, timeout):
            awaitable.close()
            timeouts.append(timeout)
            raise TimeoutError

        runner.on_tick = tick
        with monkeypatch.context() as patch:
            patch.setattr(loopback_server.asyncio, "wait_for", timeout)
            await runner.main_loop()
        assert ticks == [0, 1, 2, 3]
        assert timeouts == [0.1, 0.1, 0.1]
    asyncio.run(scenario())


def test_stop_drains_an_in_flight_real_request_and_completes_lifespan():
    async def scenario():
        entered, release, shutting_down = asyncio.Event(), asyncio.Event(), asyncio.Event()
        lifecycle = []

        @asynccontextmanager
        async def lifespan(app):
            lifecycle.append("start")
            try:
                yield
            finally:
                lifecycle.append("stop")

        async def gated(request):
            entered.set()
            await release.wait()
            lifecycle.append("response")
            return PlainTextResponse("completed")

        class ObservedServer(server_type()):
            async def shutdown(self, sockets=None):
                shutting_down.set()
                await super().shutdown(sockets=sockets)

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0)); sock.listen(128)
        port = sock.getsockname()[1]
        runner = ObservedServer(uvicorn.Config(
            Starlette(routes=[Route("/gate", gated)], lifespan=lifespan),
            log_level="critical", access_log=False, lifespan="on"))
        task = asyncio.create_task(runner.serve(sockets=[sock]))
        request = None
        try:
            for _ in range(200):
                if runner.started: break
                if task.done(): await task
                await asyncio.sleep(0.01)
            assert runner.started
            async with httpx.AsyncClient(trust_env=False) as client:
                request = asyncio.create_task(client.get(f"http://127.0.0.1:{port}/gate"))
                await asyncio.wait_for(entered.wait(), 2)
                runner.request_exit()
                await asyncio.wait_for(shutting_down.wait(), 2)
                assert not task.done() and not request.done()
                assert lifecycle == ["start"] and not runner.force_exit
                release.set()
                response = await asyncio.wait_for(request, 2)
                assert response.status_code == 200 and response.text == "completed"
                await asyncio.wait_for(task, 10)
            assert lifecycle == ["start", "response", "stop"]
            assert not runner.server_state.tasks and not runner.server_state.connections
            assert all(not server.is_serving() for server in runner.servers)
            assert sock.fileno() == -1
        finally:
            release.set()
            runner.request_exit()
            if request is not None:
                await asyncio.wait_for(request, 2)
            await asyncio.wait_for(task, 10)
            sock.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("server_kind", ["stock", "stop_wakeup"])
def test_startup_failure_does_not_leave_a_server_or_background_task(server_kind):
    async def scenario():
        events = []
        @asynccontextmanager
        async def failed(app):
            events.append("startup")
            raise RuntimeError("test startup failure")
            yield
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0)); sock.listen(128)
        factory = uvicorn.Server if server_kind == "stock" else server_type()
        runner = factory(uvicorn.Config(Starlette(lifespan=failed),
            log_level="critical", access_log=False, lifespan="on"))
        async def startup():
            try:
                await runner.serve(sockets=[sock])
            except SystemExit as error:
                return error.code
        task = asyncio.create_task(startup())
        try:
            assert await asyncio.wait_for(task, 10) == 3
            assert not runner.started
            assert events == ["startup"]
            assert not runner.server_state.tasks and not runner.server_state.connections
        finally:
            if server_kind == "stock":
                runner.should_exit = True
            else:
                runner.request_exit()
            await asyncio.wait_for(task, 10)
            sock.close()
        assert sock.fileno() == -1 and runner.should_exit
    asyncio.run(scenario())


def test_default_runtime_fixture_keeps_the_stock_server(north_principal, clock, monkeypatch):
    from tests.contract import test_mcp
    created = []
    stock = uvicorn.Server
    def observe(config):
        runner = stock(config)
        created.append(runner)
        return runner
    monkeypatch.setattr(test_mcp.uvicorn, "Server", observe)
    async def scenario():
        async with test_mcp.running(test_mcp.services(north_principal, clock)) as (base, _):
            async with httpx.AsyncClient(trust_env=False) as client:
                assert (await client.get(base + "/healthz")).status_code == 200
        assert len(created) == 1 and type(created[0]) is stock
        assert created[0].should_exit and not created[0].force_exit
        assert all(not server.is_serving() for server in created[0].servers)
    asyncio.run(scenario())


@pytest.mark.parametrize("fixture_name", ["running", "running_contract"])
def test_actual_helper_closes_its_socket_when_server_startup_raises(
    north_principal, clock, monkeypatch, fixture_name,
):
    from types import SimpleNamespace
    from tests.contract import test_mcp
    async def scenario():
        sockets, tasks = [], []
        def allocate():
            sock = socket.socket()
            sockets.append(sock)
            return sock
        async def fail_startup(self, sockets):
            tasks.append(asyncio.current_task())
            raise RuntimeError("synthetic startup failure")
        with monkeypatch.context() as patch:
            patch.setattr(test_mcp, "socket", SimpleNamespace(socket=allocate))
            patch.setattr(uvicorn.Server, "serve", fail_startup)
            try:
                with pytest.raises(RuntimeError, match="^synthetic startup failure$"):
                    async with getattr(test_mcp, fixture_name)(test_mcp.services(north_principal, clock)):
                        pytest.fail("startup failure must not yield a running server")
                assert len(sockets) == 1 and sockets[0].fileno() == -1
                assert len(tasks) == 1 and tasks[0].done()
            finally:
                for sock in sockets:
                    sock.close()
                for task in tasks:
                    if not task.done():
                        with pytest.raises(RuntimeError):
                            await asyncio.wait_for(task, 10)
    asyncio.run(scenario())
