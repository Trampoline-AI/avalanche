"""Classify a support ticket with the real TypeSafe SDK.

Set TYPESAFE_API_KEY in the executing environment, then run:
    uv run python examples/classifier_workflow.py

Or inspect questions and run through the local browser UI:
    uv run ava dev examples/classifier_workflow.py

Importing or discovering this workflow needs neither a key nor a network call.
Execution requires a valid key; service failures are not replaced with fake answers.
"""

from __future__ import annotations

import avalanche as ava


@ava.source
def load_ticket() -> str:
    """Load a sample customer support ticket."""
    return (
        "Our account was charged twice for this month's subscription. "
        "Please reverse the duplicate charge today; we can still use the service."
    )


@ava.classifier_step(
    questions={
        "department": {
            "type": "choice",
            "instructions": {"task": "Route the ticket", "context": ["customer support"]},
            "criteria": {"billing": {"examples": ["duplicate charge"]}, "technical": None},
        },
        "urgent": {
            "type": "noul",
            "instructions": "Does the ticket require action today?",
            "criteria": None,
        },
        "severity": {
            "type": "score",
            "instructions": ["Assess severity", {"consider": "customer impact"}],
            "criteria": ["minor", {"impact": ["service unavailable"]}],
        },
    },
)
async def classify_ticket(
    ticket: str, *, classifier: ava.Classifier
) -> ava.ClassificationResult:
    """Return typed answers with their original probabilities, legend, and usage."""
    return await classifier(state=ticket)


@ava.workflow(classifier_defaults={"model": "jev-latest", "timeout": 10.0})
def classifier_workflow():
    return load_ticket() >> classify_ticket()


def _main() -> None:
    result = classifier_workflow().run(executor=ava.LocalExecutor()).result()
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    _main()
