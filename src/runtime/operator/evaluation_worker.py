"""Owned evaluation snapshots and bounded, operator-owned process execution."""

from __future__ import annotations

import asyncio
import io
import multiprocessing
import os
import queue
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import cloudpickle
from dotenv import find_dotenv
from dotenv.main import DotEnv
from pydantic import BaseModel, ConfigDict, Field, model_validator

from avalanche.dag import Workflow
from avalanche.evaluation_capture import EvaluationSubmission
from avalanche.evaluations import EvalContext, EvaluationResult, Evaluations

MAX_EVALUATION_PAYLOAD_BYTES = 8 * 1024 * 1024
_SNAPSHOT_LOCK = threading.Lock()


class EvaluationRequest(BaseModel):
    """Preterminal handoff; executable bytes are never unpickled by the operator."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)

    evaluation_id: str = Field(min_length=1)
    created_at: float
    payload: bytes | None = Field(default=None, max_length=MAX_EVALUATION_PAYLOAD_BYTES)
    error: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_submission(self) -> EvaluationRequest:
        if (self.payload is None) == (self.error is None):
            raise ValueError("Evaluation submission requires either a snapshot or an error")
        return self


class EvaluationOutcome(BaseModel):
    """The independent result pipe carries validated JSON, not executable objects."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    result: EvaluationResult | None = None
    error: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_outcome(self) -> EvaluationOutcome:
        if (self.result is None) == (self.error is None):
            raise ValueError("Evaluation outcome requires either a result or an error")
        return self


@dataclass(frozen=True)
class _EvaluationInput:
    submission: EvaluationSubmission
    environment: dict[str, str]


class _SnapshotPickler(cloudpickle.CloudPickler):
    def reducer_override(self, obj: object) -> object:
        if isinstance(obj, Workflow):
            raise TypeError("Evaluation selectors cannot capture a live Workflow")
        return super().reducer_override(obj)


def _local_modules(root: Path) -> list[ModuleType]:
    """Capture user definitions by value, including sibling/package model classes."""
    modules = []
    installed = Path(sys.prefix).resolve()
    for name, module in tuple(sys.modules.items()):
        if module is None or name.split(".", 1)[0] in {"avalanche", "runtime"}:
            continue
        filename = getattr(module, "__file__", None)
        if not isinstance(filename, str):
            continue
        path = Path(filename).absolute()
        if path.is_relative_to(root) and not path.is_relative_to(installed):
            modules.append(module)
    return modules


def _evaluation_environment() -> dict[str, str]:
    environment = dict(os.environ)
    # Match classifier loading: only load dotenv when the key is absent, preserve
    # existing environment values, and interpolate with override=False. Snapshot
    # now so evaluation does not depend on the coordinator's cwd or later edits.
    if "TYPESAFE_API_KEY" not in environment:
        disabled = environment.get("PYTHON_DOTENV_DISABLED", "").lower()
        if disabled not in {"1", "true", "t", "yes", "y"}:
            values = DotEnv(find_dotenv(usecwd=True), override=False, encoding="utf-8").dict()
            for name, value in values.items():
                if value is not None:
                    environment.setdefault(name, value)
        # Do not let the SDK discover an unrelated operator-directory dotenv.
        environment.setdefault("TYPESAFE_API_KEY", "")
    return environment


def snapshot_evaluation(submission: EvaluationSubmission) -> EvaluationRequest:
    """Synchronously own the evidence, without running selectors or model calls."""
    evaluation_id = uuid4().hex
    created_at = time.time()
    if submission.error is not None:
        return EvaluationRequest(
            evaluation_id=evaluation_id, created_at=created_at, error=submission.error
        )
    try:
        value = _EvaluationInput(submission, _evaluation_environment())
        with _SNAPSHOT_LOCK:
            registered = cloudpickle.list_registry_pickle_by_value()
            modules = [m for m in _local_modules(Path.cwd()) if m.__name__ not in registered]
            try:
                for module in modules:
                    cloudpickle.register_pickle_by_value(module)
                buffer = io.BytesIO()
                _SnapshotPickler(buffer).dump(value)
                payload = buffer.getvalue()
            finally:
                for module in modules:
                    cloudpickle.unregister_pickle_by_value(module)
        if len(payload) > MAX_EVALUATION_PAYLOAD_BYTES:
            raise ValueError("Evaluation snapshot exceeds the 8 MiB submission limit")
        return EvaluationRequest(
            evaluation_id=evaluation_id, created_at=created_at, payload=payload
        )
    except Exception as error:
        return EvaluationRequest(
            evaluation_id=evaluation_id,
            created_at=created_at,
            error=f"Evaluation snapshot failed: {type(error).__name__}: {error}",
        )


def evaluation_worker(payload: bytes, result_pipe: Connection) -> None:
    """Evaluate one snapshot in a fresh process, never importing workflow source."""
    try:
        value: object = cloudpickle.loads(payload)
        if not isinstance(value, _EvaluationInput):
            raise TypeError("Invalid evaluation snapshot")
        submission = value.submission
        if not isinstance(submission, EvaluationSubmission):
            raise TypeError("Invalid evaluation submission")
        if not isinstance(submission.evaluations, Evaluations) or not isinstance(
            submission.context, EvalContext
        ):
            raise TypeError("Evaluation snapshot is missing its declaration or context")
        if not isinstance(value.environment, dict) or not all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in value.environment.items()
        ):
            raise TypeError("Invalid evaluation environment")
        os.environ.clear()
        os.environ.update(value.environment)
        result = asyncio.run(
            submission.evaluations.evaluate(
                submission.context, runtime_defaults=submission.runtime_defaults
            )
        )
        outcome = EvaluationOutcome(result=result)
    except BaseException as error:
        outcome = EvaluationOutcome(error=f"{type(error).__name__}: {error}")
    try:
        result_pipe.send_bytes(outcome.model_dump_json().encode())
    finally:
        result_pipe.close()


@dataclass(frozen=True)
class _EvaluationJob:
    run_id: str
    evaluation_id: str
    payload: bytes


class EvaluationWorkers:
    """Two dispatchers, a bounded nonblocking queue, and one fresh process per job.

    Processes belong to the operator, not coordinator/Ray process groups. A fresh
    process prevents source-module caches, author globals and dotenv from leaking
    across executions. Close interrupts in-flight work instead of draining forever.
    """

    def __init__(self, publish: Callable[[str, str, EvaluationOutcome], None]) -> None:
        self._publish = publish
        self._queue: queue.Queue[_EvaluationJob] = queue.Queue(maxsize=32)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._mp = multiprocessing.get_context("spawn")

    def submit(self, run_id: str, request: EvaluationRequest) -> None:
        if request.payload is None:
            raise ValueError("Cannot execute an evaluation without a snapshot")
        with self._lock:
            if self._stop.is_set():
                raise RuntimeError("Operator evaluation workers are closed")
            if not self._threads:
                for index in range(2):
                    thread = threading.Thread(
                        target=self._dispatch,
                        name=f"avalanche-evaluation-{index}",
                        daemon=True,
                    )
                    thread.start()
                    self._threads.append(thread)
            try:
                self._queue.put_nowait(
                    _EvaluationJob(run_id, request.evaluation_id, request.payload)
                )
            except queue.Full:
                raise RuntimeError("Evaluation queue is full") from None

    def _dispatch(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                outcome = self._execute(job.payload)
            except Exception as error:
                outcome = EvaluationOutcome(
                    error=f"Evaluation worker failed: {type(error).__name__}: {error}"
                )
            self._publish(job.run_id, job.evaluation_id, outcome)

    def _execute(self, payload: bytes) -> EvaluationOutcome:
        receiver, sender = self._mp.Pipe(duplex=False)
        process = self._mp.Process(
            target=evaluation_worker, args=(payload, sender), name="avalanche-evaluator"
        )
        try:
            process.start()
            sender.close()
            while not self._stop.is_set():
                if receiver.poll(0.1):
                    return EvaluationOutcome.model_validate_json(
                        receiver.recv_bytes(MAX_EVALUATION_PAYLOAD_BYTES)
                    )
                # A child can send its result between the poll and exit observation.
                if not process.is_alive() and not receiver.poll():
                    raise RuntimeError(
                        "Evaluation process exited without a result "
                        f"(exit code {process.exitcode})"
                    )
            return EvaluationOutcome(error="Operator closed before evaluation completed")
        finally:
            sender.close()
            receiver.close()
            if process.pid is not None:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=1.0)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=1.0)
                if not process.is_alive():
                    process.close()

    def close(self) -> None:
        with self._lock:
            self._stop.set()
            threads = tuple(self._threads)
        while True:
            try:
                job = self._queue.get_nowait()
            except queue.Empty:
                break
            self._publish(
                job.run_id,
                job.evaluation_id,
                EvaluationOutcome(error="Operator closed before evaluation started"),
            )
        for thread in threads:
            thread.join(timeout=3.0)
