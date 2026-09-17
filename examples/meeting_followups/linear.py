"""Personal-key Linear GraphQL publication; no automatic mutation retries."""

import json
from urllib.request import Request, urlopen

from pydantic import BaseModel, Field

from .schema import IssueReceipt, PlannedIssue

ENDPOINT = "https://api.linear.app/graphql"
CREATE_ISSUE = """
mutation CreateMeetingFollowup($input: IssueCreateInput!) {
  issueCreate(input: $input) {
    success
    issue { identifier url }
  }
}
"""


class GraphQLError(BaseModel):
    message: str


class CreatedIssue(BaseModel):
    identifier: str
    url: str


class IssueCreatePayload(BaseModel):
    success: bool
    issue: CreatedIssue | None = None


class MutationData(BaseModel):
    issueCreate: IssueCreatePayload  # noqa: N815 -- Linear's external field name


class MutationResponse(BaseModel):
    data: MutationData | None = None
    errors: list[GraphQLError] = Field(default_factory=list)


def create_issue(issue: PlannedIssue, *, api_key: str) -> IssueReceipt:
    """Create one issue or raise on HTTP, GraphQL, or malformed-response failures."""
    destination = issue.destination
    if destination is None:
        raise ValueError(f"No Linear destination for {issue.department.value}")
    body = json.dumps(
        {
            "query": CREATE_ISSUE,
            "variables": {
                "input": {
                    "teamId": str(destination.team_id),
                    "labelIds": [str(destination.category_label_ids[issue.category])],
                    "title": issue.title,
                    "description": issue.description,
                }
            },
        }
    ).encode("utf-8")
    request = Request(
        ENDPOINT,
        data=body,
        headers={"Authorization": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        result = MutationResponse.model_validate_json(response.read())
    if result.errors:
        raise RuntimeError(
            "Linear rejected the mutation: "
            + "; ".join(error.message for error in result.errors)
        )
    if result.data is None:
        raise RuntimeError("Linear returned no mutation data")
    payload = result.data.issueCreate
    if not payload.success or payload.issue is None:
        raise RuntimeError("Linear did not confirm issue creation")
    return IssueReceipt(
        item_id=issue.item_id,
        identifier=payload.issue.identifier,
        url=payload.issue.url,
    )
