## Purpose

Keep the operator's current workflow catalog synchronized with local source changes and make catalog revisions visible to connected user interfaces.

## ADDED Requirements

### Requirement: Atomically publish the current workflow catalog

The operator SHALL replace the current workflow catalog only with one complete, valid discovery result. A catalog revision SHALL describe the current workflows, their topology, and their metadata without mixing information from different discovery results.

#### Scenario: Source change creates a workflow
- **WHEN** a watched workflow source change yields a newly discoverable workflow
- **THEN** the current catalog includes that workflow and publishes a catalog change to connected clients

#### Scenario: Source change changes workflow topology
- **WHEN** a watched workflow source change changes a workflow's nodes, edges, ordering, or display metadata
- **THEN** the current catalog exposes the new workflow definition and connected clients receive an update sufficient to refresh the workflow view

#### Scenario: Discovery fails during reload
- **WHEN** a watched source change cannot produce a valid replacement catalog
- **THEN** the operator retains the last valid catalog and exposes the discovery diagnostic without publishing a partial catalog

### Requirement: Isolate runs from later catalog revisions

A catalog reload SHALL affect workflow selection and runs started after the reload. It SHALL NOT mutate the recorded definition or execution state of an existing run.

#### Scenario: Reload while a run is active
- **WHEN** a workflow is reloaded while one of its runs is active
- **THEN** the active run continues against its recorded workflow definition and later workflow views show the reloaded definition
