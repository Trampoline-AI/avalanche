"""Graph-level wait constructors. No worker is dispatched for these nodes."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from pydantic import BaseModel, JsonValue, TypeAdapter

from runtime._async import call_sync_or_async

from .dag import Node, NodeFuture, NodeType
from .policies import Outcome, StepPolicy, WaitSpec
from .step_interface import resolve_step_signature

_JSON = TypeAdapter(JsonValue)


def _seconds(duration: timedelta | float) -> float:
    seconds = duration.total_seconds() if isinstance(duration, timedelta) else duration
    # Validate here rather than trusting a float conversion to accept strings/bools.
    return TypeAdapter(float).validate_python(seconds, strict=True)


class WaitNode(Node):
    _expand_single_return = False

    def __init__(
        self, spec: WaitSpec, model: type[BaseModel] | None, *, slug: str | None = None
    ) -> None:
        def wait_node() -> None:
            raise RuntimeError("Wait nodes are resolved by the driver, not executed")

        wait_node.__name__ = "wait_for_event" if spec.kind == "event" else spec.kind
        super().__init__(wait_node, NodeType.WAIT, slug=slug, policy=StepPolicy(retries=0))
        self.spec = spec
        self.model = model
        self.map_values: tuple[JsonValue | BaseModel, ...] | None = None


def sleep(
    duration: timedelta | float | None = None,
    *,
    until: datetime | None = None,
    slug: str | None = None,
) -> NodeFuture:
    """Pause until an absolute instant, or for seconds/timedelta from arming."""
    if (duration is None) == (until is None):
        raise ValueError("sleep requires exactly one of duration or until")
    if until is not None and (until.tzinfo is None or until.utcoffset() is None):
        raise ValueError("until must be timezone-aware")
    spec = WaitSpec(
        kind="sleep",
        until=until,
        timeout_seconds=_seconds(duration) if duration is not None else None,
    )
    return WaitNode(spec, None, slug=slug)()


def wait_for_event(
    model: type[BaseModel],
    *,
    key: str,
    timeout: timedelta | float | None,
    slug: str | None = None,
) -> NodeFuture:
    """One exact, declared run-scoped event key; None output means timeout."""
    if not isinstance(model, type) or not issubclass(model, BaseModel):
        raise TypeError("wait_for_event requires a Pydantic model class")
    if not isinstance(key, str) or not key:
        raise ValueError("event key must be a nonempty string")
    spec = WaitSpec(
        kind="event",
        key=key,
        schema=model.model_json_schema(),
        timeout_seconds=_seconds(timeout) if timeout is not None else None,
    )
    return WaitNode(spec, model, slug=slug)()


def _decision_node(
    outcome: type[Outcome],
    *,
    prompt: str,
    timeout: timedelta | float | None,
    show: NodeFuture | JsonValue | BaseModel = None,
    on_timeout: Outcome | None = None,
    assignees: list[str] | tuple[str, ...] | None = None,
    approvers: list[str] | tuple[str, ...] | None = None,
    slug: str | None = None,
) -> tuple[WaitNode, NodeFuture | None]:
    """Typed human decision. Missing on_timeout means decision_timeout failure."""
    if not isinstance(outcome, type) or not issubclass(outcome, Outcome):
        raise TypeError("decision requires an ava.Outcome subclass")
    if on_timeout is not None and not isinstance(on_timeout, outcome):
        raise TypeError("on_timeout must be an instance of the declared Outcome")
    show_ref = show if isinstance(show, NodeFuture) else None
    shown = (
        None
        if show_ref is not None
        else (show.model_dump(mode="json") if isinstance(show, BaseModel) else show)
    )
    spec = WaitSpec(
        kind="decision",
        schema=outcome.model_json_schema(),
        prompt=prompt,
        timeout_seconds=_seconds(timeout) if timeout is not None else None,
        show_node_id=show_ref.future_id if show_ref is not None else None,
        show_index=show_ref.tuple_index if show_ref is not None else None,
        show=_JSON.validate_python(shown),
        on_timeout=on_timeout.model_dump(mode="json") if on_timeout is not None else None,
        assignees=tuple(assignees) if assignees is not None else (),
        approvers=tuple(approvers) if approvers is not None else None,
    )
    return WaitNode(spec, outcome, slug=slug), show_ref


class _DecisionShowNode(Node):
    def __init__(self, fn: Callable, decision: NodeFuture) -> None:
        super().__init__(fn, NodeType.STEP, slug=f"{decision.node_slug}.show")
        self.decision_id = decision.future_id

    def _invocation_id(self, instance: int) -> str:
        return f"{self.decision_id}_show"


class DecisionFactory:
    def __call__(
        self,
        outcome: type[Outcome],
        *,
        prompt: str,
        timeout: timedelta | float | None,
        show: NodeFuture | JsonValue | BaseModel = None,
        on_timeout: Outcome | None = None,
        assignees: list[str] | tuple[str, ...] | None = None,
        approvers: list[str] | tuple[str, ...] | None = None,
        slug: str | None = None,
    ) -> NodeFuture:
        node, show_ref = _decision_node(
            outcome,
            prompt=prompt,
            timeout=timeout,
            show=show,
            on_timeout=on_timeout,
            assignees=assignees,
            approvers=approvers,
            slug=slug,
        )
        return node(show=show_ref) if show_ref is not None else node()

    def map[Item: JsonValue | BaseModel](
        self,
        outcome: type[Outcome],
        items: NodeFuture | list[Item],
        *,
        prompt: str,
        timeout: timedelta | float | None,
        on_timeout: Outcome | None = None,
        assignees: list[str] | tuple[str, ...] | None = None,
        approvers: list[str] | tuple[str, ...] | None = None,
        max_items: int = 8,
        slug: str | None = None,
        show: Callable[[Item], BaseModel] | None = None,
    ) -> NodeFuture:
        if show is not None and not callable(show):
            raise TypeError("decision.map show must be a callable receiving one item")
        node, _ = _decision_node(
            outcome,
            prompt=prompt,
            timeout=timeout,
            on_timeout=on_timeout,
            assignees=assignees,
            approvers=approvers,
            slug=slug,
        )
        if isinstance(items, NodeFuture):
            node.spec = WaitSpec.model_validate(
                {
                    **node.spec.model_dump(by_alias=True),
                    "map_max_items": max_items,
                    "map_source_node_id": items.future_id,
                    "map_source_index": items.tuple_index,
                }
            )
            result = node(items=items)
        else:
            if not isinstance(items, list):
                raise TypeError("decision.map items must be a list or NodeFuture list")
            node.map_values = tuple(items)
            wire = [
                item.model_dump(mode="json") if isinstance(item, BaseModel) else item
                for item in items
            ]
            node.spec = WaitSpec.model_validate(
                {**node.spec.model_dump(by_alias=True), "map_max_items": max_items,
                 "map_items": wire}
            )
            result = node()
        if show is not None:
            def decision_show(item: JsonValue | BaseModel) -> BaseModel:
                shown = call_sync_or_async(show, item)
                if not isinstance(shown, BaseModel):
                    raise TypeError("decision.map show must return a Pydantic model")
                return shown

            signature = resolve_step_signature(show)
            parameter = next(iter(signature.parameters.values()))
            decision_show.__annotations__["item"] = parameter.annotation
            decision_show.__annotations__["return"] = signature.return_annotation
            shown = _DecisionShowNode(decision_show, result).map(items, max_items=max_items)
            node.spec = node.spec.model_copy(update={"show_node_id": shown.future_id})
            result.kwargs["show"] = shown
            shown >> result
            result.chain_start = shown
        return result


decision = DecisionFactory()
