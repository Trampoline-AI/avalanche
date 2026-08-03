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

#### Scenario: Watched sources remain unchanged
- **WHEN** no watched workflow source or effective catalog content has changed
- **THEN** the operator retains the current catalog revision and does not publish a replacement catalog update

### Requirement: Isolate runs from later catalog revisions

A catalog reload SHALL affect workflow selection and runs started after the reload. It SHALL NOT mutate the recorded definition or execution state of an existing run.

#### Scenario: Reload while a run is active
- **WHEN** a workflow is reloaded while one of its runs is active
- **THEN** the active run continues against its recorded workflow definition and later workflow views show the reloaded definition

### Requirement: Expose configurable reload lifecycle logs

The operator CLI SHALL accept `--log-level` with `DEBUG`, `INFO`, `WARNING`, and
`ERROR` levels and SHALL default to `WARNING`. The selected level SHALL configure
terminal logging before operator services start. At `INFO`, the source watcher SHALL
log watcher startup and shutdown, each detected reload attempt, successful catalog
replacement with its revision transition, and unchanged reload results. A failed
reload SHALL emit a `WARNING` with its structured discovery diagnostic summary while
the last valid catalog remains active.

#### Scenario: Operator starts with informational logging
- **WHEN** a user starts `ava operator` with `--log-level INFO`
- **THEN** operator service startup and hot-reload lifecycle messages are visible in the terminal

#### Scenario: Reload changes the catalog
- **WHEN** a watched source change produces a valid catalog replacement
- **THEN** the operator logs the reload attempt and successful old-to-new catalog revision transition

#### Scenario: Reload has no effective change
- **WHEN** a watched source change produces catalog content identical to the current catalog
- **THEN** the operator logs that the reload completed without an effective catalog change

#### Scenario: Reload fails
- **WHEN** a watched source change produces discovery or catalog validation diagnostics
- **THEN** the operator logs a warning summarizing the failure and continues serving the last valid catalog
