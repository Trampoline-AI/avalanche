## Purpose

Retain the exact workflow definition used by each run so operator clients can render historical execution independently from the workflow currently in the catalog.

## ADDED Requirements

### Requirement: Capture an immutable executed topology

When the operator creates a run, it SHALL retain an immutable workflow topology snapshot derived from the workflow that was prepared for that run. The snapshot SHALL include node identity and ordering, graph edges, node types, and display metadata required to render the run's workflow graph.

#### Scenario: Run begins from the current workflow
- **WHEN** a run is created for a workflow
- **THEN** the run has a topology snapshot matching the workflow definition actually prepared for that run

### Requirement: Serve historical run topology

Run detail retrieval SHALL return the run's retained topology snapshot together with its node execution state. It SHALL NOT substitute the topology of the currently discovered workflow.

#### Scenario: Workflow changes after a completed run
- **WHEN** a completed run is viewed after its workflow has changed nodes or edges
- **THEN** the run view renders the nodes and edges from the run's retained snapshot and associates execution state only with those historical nodes

#### Scenario: Workflow is removed after a run
- **WHEN** a workflow is no longer present in the current catalog but historical runs remain retained
- **THEN** each retained run remains retrievable and renderable using its topology snapshot

### Requirement: Keep topology identity stable through updates

All run updates and detail records SHALL be associated with the immutable run identity and its captured topology, rather than the latest catalog revision.

#### Scenario: Active run receives node updates after a reload
- **WHEN** an active run publishes a node status, log, trace, or agent event after its workflow reloads

