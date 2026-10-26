# TUI extension navigation

A hosting provider can contribute an account home and project tree without
pretending they are workflows. Its tree remains visible beside the selected
resource view. The provider still owns authorization, scope binding, transport
cancellation and deadlines; navigation is a local selection, not a backend action.

## Opt in without changing the run protocol

Add `"navigation"` to both `TuiExtensions.features` and the features returned by
`describe()`. Return `ExtensionDescription.navigation: NavigationContribution`.
The TUI extension version remains `(1, 0)`; unknown features are additive and
ignored. `avalanche.PROTOCOL_VERSION` is unchanged: the run protocol is untouched.

Without the feature, the workflow sidebar, dashboard, keyboard behavior and modal
resource panels are unchanged. A contribution without the negotiated flag is
ignored. With the feature declared but no contribution, the legacy UI remains.
Providers supporting older installed Avalanche versions should only import and
construct these new types when available; otherwise omit the contribution and
flag. An older app ignoring the feature needs no new run protocol or provider
methods. `TuiNavigation` is an optional companion protocol, not a new required
method on `TuiExtensions` or `StateProvider`.

When active, navigation **replaces** the workflow sidebar and dashboard. Provider
panels use the existing table, tree, JSON, diff and trusted-widget fallback
renderers in the central area, not modal screens. The provider can describe the
panels appropriate to each selected scope. Removing the contribution restores
the legacy layout.

## Value types

All types below are frozen dataclasses in `tui.state`. Existing positional
constructors remain valid because the new fields have defaults.

```text
NavigationTarget(
    context: TuiContext,
    panel_id: str | None = None,
    resource_id: str | None = None,
)
NavigationNode(
    id: str,
    label: str,
    target: NavigationTarget | None = None,
    badge: str | None = None,
    children: tuple[NavigationNode, ...] = (),
    lazy: bool = False,
    expanded: bool = False,
)
NavigationContribution(
    nodes: tuple[NavigationNode, ...],
    initial_target: NavigationTarget,
    home_target: NavigationTarget,
)
NavigationPage(
    context: TuiContext,
    nodes: tuple[NavigationNode, ...],
    next_cursor: Cursor | None,
    truncated: bool,
    revision: Revision,
)
```

`ExtensionDescription.navigation` defaults to `None`.
`ExtensionPage.row_targets: Mapping[str, NavigationTarget]` defaults to an empty
immutable mapping. Supply an immutable mapping keyed by the **string `id` of each
row on that page**. Row IDs must be unique. The mapping is replaced with the page;
old-page links cannot activate against a newer selection.

A target selects a panel **or** resource, never both. `panel_id` resolves against
currently described panels; unknown panels render an inline unavailable message.
`resource_id` selects the described panel for that resource, or a generic JSON
panel if no renderer was described. With neither field set, the first described
panel is selected; no panels means an explicit unknown/unavailable view. The entire `TuiContext`
(scope, flow, version, run, node, attempt) is preserved, except that the app owns
`generation` and overwrites the provider's value.

Node IDs must be stable and unique within a tree. Labels and badges are plain
text, not markup or status enums: unknown values remain visible verbatim.
`expanded` opens a branch on first display, including one initial request for a
lazy branch. Further lazy pages are loaded only by user activation, not polling.

## Minimal extension provider

This local example is the extension attached to an otherwise complete
`StateProvider` as `provider.tui_extensions`. It does not replace that provider's
run, stream or reset/baseline interface.

```python
from types import MappingProxyType

from tui.state import (
    ExtensionDescription, ExtensionPage, NavigationContribution, NavigationNode,
    NavigationPage, NavigationTarget, PanelDescriptor, TuiContext, TuiExtensionError,
)


class AccountNavigation:
    protocol_version = (1, 0)
    features = frozenset({"paged-resources", "navigation"})

    def __init__(self):
        home = NavigationTarget(TuiContext((("org", "example"),)), panel_id="home")
        self.project = NavigationTarget(
            TuiContext((("org", "example"), ("project", "sample"))),
            resource_id="project.overview",
        )
        self.contribution = NavigationContribution(
            nodes=(NavigationNode("projects", "Projects", lazy=True),),
            initial_target=home,
            home_target=home,
        )

    def describe(self, context):
        return ExtensionDescription(
            context, self.protocol_version, self.features, "catalog-1", (), (),
            (PanelDescriptor("home", "Account home", "navigation", "projects", "table"),),
            navigation=self.contribution,
        )

    def read_navigation(self, parent_id, context, *, cursor=None,
                        limit=100, byte_limit=262144):
        if parent_id != "projects":
            raise TuiExtensionError("not_found", "Unknown navigation parent")
        return NavigationPage(
            context,
            (NavigationNode("sample", "Sample project", target=self.project,
                            badge="status unknown"),),
            None, False, "projects-1",
        )

    def read(self, resource, context, *, cursor=None, limit=100, byte_limit=262144):
        return ExtensionPage(
            context, (MappingProxyType({"id": "sample", "label": "Sample project"}),),
            None, False, "projects-1",
            row_targets=MappingProxyType({"sample": self.project}),
        )

    def invoke(self, action, context, payload, *, intent_id):
        raise TuiExtensionError("unsupported", "No actions contributed", context=context)

    def subscribe(self, callback):
        return lambda: None
```

## Lazy paging and lifecycle

Only extensions with lazy nodes need to implement the optional `TuiNavigation`
method:

```python
def read_navigation(
    self, parent_id: str, context: TuiContext, *,
    cursor: Cursor | None = None, limit: int = 100, byte_limit: int = 262144,
) -> NavigationPage: ...
```

It runs through the existing off-thread `ExtensionSession`: at most two calls,
no executor backlog, and exact echoed-context checks. Scope/target selection
advances the app generation, cancels queued obsolete work and drops running or
already-queued late completions. Synchronous transports cannot be forcibly
interrupted by cancelling a Python future; providers must bound their deadlines.
Unsubscription and close retain the existing ownership/join behavior.

Each eager contribution or lazy page is bounded to **100 total nodes** (including
nested eager children), **256 KiB encoded JSON** (`dataclasses.asdict` followed by
`json.dumps`, UTF-8), and depth 32. Resource-page bounds include row-target metadata.
Violations and safe `TuiExtensionError` failures render inline. For a lazy node
without a loader, expansion shows unsupported inline. Errors provide a retry row.

A non-null cursor produces a selectable **More…** row. More replaces the loaded
child page instead of accumulating siblings; empty pages with a cursor are valid.
A changed revision rejects continuation and offers first-page retry. `truncated`
shows unknown/incomplete children even when no cursor is available. There is also
a 2,000-node retained tree-window ceiling. Narrow the contributed tree when that
ceiling is reached. Loaded branches persist while the contribution's `nodes`
remain equal; return changed nodes to replace the tree after a catalog change.

Navigation fetches only the selected description/resource and explicitly requested
children. It does not reload workflow catalogs or histories, add timers, or poll
for resources. The existing UI tick only drains completions and invalidation hints.
Activating the same target again is a no-op; use the resource's Reload control for
an explicit refresh. Existing invalidations and reset/baseline reconciliation remain
available; the provider controls which selected resources need invalidation.

## Keyboard and mouse

- `h`: Home, when navigation is active; no new shortcut in the legacy UI.
- `c`: focus navigation. `Tab` / `Shift+Tab`: native widget focus traversal.
- Up/down: tree or resource row cursor. Enter or mouse click: activate a node/row.
- Left: collapse the branch or move to its parent. Right/Space: expand/toggle.
- More and retry rows use the same keyboard/mouse activation as ordinary nodes.
- Table and tree resource rows navigate through `row_targets`; other renderers
  expose labeled `Open <row-id>` buttons. Read-only mode permits local navigation
  while retaining both UI and provider mutation fences.
