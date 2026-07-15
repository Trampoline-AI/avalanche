"""Sync/async workflow behavior matrix tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import wraps
from typing import Any

import pytest

import avalanche as ava

TASK_MODES = [
    pytest.param(False, id="sync"),
    pytest.param(True, id="async"),
]


def maybe_async(async_mode: bool, fn: Callable[..., Any]) -> Callable[..., Any]:
    """Return fn unchanged or wrapped as an async function for matrix tests."""
    if not async_mode:
        return fn

    @wraps(fn)
    async def async_fn(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0)
        return fn(*args, **kwargs)

    return async_fn


@pytest.mark.parametrize("async_mode", TASK_MODES)
def test_source_step_dest_chain_matrix(async_mode: bool):
    load = ava.source(maybe_async(async_mode, lambda: {"data": [1, 2, 3]}))
    double = ava.step(
        maybe_async(async_mode, lambda data: {"data": [item * 2 for item in data["data"]]})
    )
    save = ava.dest(maybe_async(async_mode, lambda data: f"saved_{sum(data['data'])}"))

    @ava.workflow
    def matrix_workflow():
        return load() >> double() >> save()

    assert matrix_workflow().run(executor=ava.LocalExecutor()).result() == "saved_12"


@pytest.mark.parametrize("async_mode", TASK_MODES)
def test_explicit_args_and_multiple_returns_matrix(async_mode: bool):
    def load_pair_impl():
        return [1, 2], [3, 4]

    def sum_values_impl(values: list[int]) -> int:
        return sum(values)

    def combine_impl(left: int, right: int) -> dict[str, int]:
        return {"left": left, "right": right, "total": left + right}

    load_pair = ava.source(maybe_async(async_mode, load_pair_impl), num_returns=2)
    sum_values = ava.step(maybe_async(async_mode, sum_values_impl))
    combine = ava.dest(maybe_async(async_mode, combine_impl))

    @ava.workflow
    def matrix_workflow():
        pair = load_pair()
        left = sum_values(pair[0])
        right = sum_values(pair[1])
        combined = combine(left, right)
        return pair, left, right, combined

    pair, left, right, combined = matrix_workflow().run(executor=ava.LocalExecutor()).result()

    assert pair == ([1, 2], [3, 4])
    assert left == 3
    assert right == 7
    assert combined == {"left": 3, "right": 7, "total": 10}


@pytest.mark.parametrize("async_mode", TASK_MODES)
def test_runtime_injection_matrix(async_mode: bool):
    def capture_impl(data: list[int], *, logger=ava.Logger()) -> dict[str, Any]:
        from avalanche.runtime import get_current_run_context
        from avalanche.runtime.providers.logger import LoggerInstance

        context = get_current_run_context()
        assert context is not None
        assert isinstance(logger, LoggerInstance)
        return {
            "data": data,
            "node": context.node_name,
            "metadata": context.metadata,
            "logger_type": type(logger).__name__,
        }

    capture = ava.step(maybe_async(async_mode, capture_impl))

    @ava.workflow
    def matrix_workflow():
        return capture([1, 2, 3])

    result = (
        matrix_workflow()
        .run(
            executor=ava.LocalExecutor(),
            context={"metadata": {"tenant": "acme"}},
        )
        .result()
    )

    assert result == {
        "data": [1, 2, 3],
        "node": "capture_impl",
        "metadata": {"tenant": "acme"},
        "logger_type": "LoggerInstance",
    }


@pytest.mark.asyncio
async def test_local_executor_resolves_async_task_inside_existing_event_loop():
    executor = ava.LocalExecutor()

    async def add(left: int, right: int) -> int:
        await asyncio.sleep(0)
        return left + right

    assert executor.submit(add, 2, 3) == 5


@pytest.mark.ray
def test_ray_executor_resolves_async_step():
    pytest.importorskip("ray")
    import ray

    if ray.is_initialized():
        ray.shutdown()

    ray.init(
        num_cpus=2,
        ignore_reinit_error=True,
        include_dashboard=False,
        runtime_env={"working_dir": None},
    )

    try:
        load = ava.source(lambda: [1, 2, 3])

        async def double_impl(data: list[int]) -> list[int]:
            await asyncio.sleep(0)
            return [item * 2 for item in data]

        double = ava.step(double_impl)
        save = ava.dest(lambda data: f"saved_{sum(data)}")

        @ava.workflow
        def matrix_workflow():
            return load() >> double() >> save()

        assert matrix_workflow().run(executor=ava.RayExecutor()).result() == "saved_12"
    finally:
        ray.shutdown()
