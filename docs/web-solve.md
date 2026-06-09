# Web UI solving and solved DB workflow

This document describes the web UI solve workflow used by the Sessions panel and the Solved DB controls.

AetherWard records raw sessions first. Solving is a post-processing step that turns many GPS-tagged RSS/RSSI observations into approximate source positions. Treat solved positions as **triage-grade estimates**, not ground truth.

---

## Core ideas

| Concept | Meaning |
|---------|---------|
| Session file | Append-only JSONL capture under `~/.aetherward/sessions/` |
| Raw map | Route and observations drawn directly from session JSONL records |
| Session solve | One solve run over selected sessions; creates/display a solved result |
| Solved DB | Persistent SQLite DB under `~/.aetherward/solver/` |
| Canonical position | One trusted solved marker for a source or source geo-cluster |
| Evidence cell | Aggregated observation cell used as solver input/debug evidence |
| Evidence layer | Optional map-only display of weak/underconstrained observation centroids |

The solved DB stores more than marker coordinates. It keeps:

- canonical solved positions;
- per-session sample/evidence cells;
- route previews for fast map loading;
- a session manifest used for incremental updates;
- metadata about solver settings.

---

## Session panel selection

The Sessions panel has checkboxes for each solvable session.

- Select one or more sessions to limit solve/update work to those sessions.
- Use **Select all** to explicitly select every visible/solvable session.
- Use **Clear** to clear the selection.
- When no sessions are selected, **Solve Selected** and **Update DB** use all solvable sessions, or all new/changed sessions where applicable.

This makes two workflows possible:

```text
All sessions workflow:
  leave selection empty or Select all
  → Solve Selected

Targeted append workflow:
  select only the new session(s)
  → choose existing solved DB
  → Update DB
```

---

## Evidence layer checkbox

The **evidence layer** checkbox is a debug aid.

When it is off:

```text
Only real RSS/RSSI solved positions are shown in the Positions layer.
Underconstrained sources are not drawn as trusted positions.
```

When it is on:

```text
Sources with too little geometry can be drawn as weak observation centroids.
These evidence markers are map-only diagnostics and are not treated as canonical solved positions.
```

Use it when asking questions like:

- Why did this session produce few solved APs?
- Did we observe this MAC at all?
- Are there weak/underconstrained observations near my route?

Leave it off for normal review and exports.

---

## Solved DB buttons

| Button | Behavior | Mutates DB? |
|--------|----------|-------------|
| **Refresh** | Refresh list of solved DB files | No |
| **Load Map** | Replace current solved map positions with selected DB | No |
| **Overlay** | Draw selected DB over current map for visual comparison | No |
| **Update DB** | Ingest selected/new/changed sessions into selected DB and recompute touched sources | Yes |
| **Import DB** | Copy an external solved DB into `~/.aetherward/solver/` | Yes, by importing a copy |

`Overlay` is intentionally read-only. It is for comparing two DBs visually. It does not merge them.

`Update DB` is the real incremental append workflow. It modifies the selected DB in place.

---

## Recommended workflows

### Create a first solved DB

```text
1. Capture one or more wardriver sessions.
2. Open the Sessions panel.
3. Select the sessions to include, or leave all unchecked to use all solvable sessions.
4. Keep evidence layer off unless debugging.
5. Click Solve Selected.
6. A new bulk solved DB is created under ~/.aetherward/solver/ and loaded on the map.
```

### Append new sessions to an existing DB

```text
1. Capture/import new session files.
2. Open the Sessions panel.
3. Select only the new/changed sessions, or leave all unchecked to let the DB manifest decide.
4. In the Solved DB box, select the existing reference DB.
5. Click Update DB.
6. The DB ingests new/changed evidence and recomputes only touched sources.
```

### Compare two solved DBs

```text
1. Select DB A → Load Map.
2. Select DB B → Overlay.
```

This is visual-only. To really rebuild a combined DB, solve/update from session files rather than overlaying solved DB files.

### Rebuild from scratch

Use **Solve Selected** again when:

- solver settings changed;
- you suspect stale DB state;
- you changed code affecting solve behavior;
- you want a clean reference DB from a known session selection.

---

## Incremental update behavior

The DB contains a `sessions` manifest. For each imported session it records identifying metadata such as path, size, mtime, hash, record count, and source count.

During **Update DB**:

```text
unchanged session → skipped
new session       → indexed and added
changed session   → old contribution removed, then re-indexed
missing file      → warning; existing solved data is kept unless explicitly rebuilt
```

Only touched source IDs are recomputed. Sources not present in the new/changed sessions are left alone.

This avoids the cost of recalculating a full multi-session DB after every drive.

---

## Same-MAC geo guard

AetherWard treats a same BSSID/MAC as a **candidate identity**, not as guaranteed proof of one physical AP.

Before creating canonical positions, the web bulk solver aggregates observations into small geo-cells and checks geographic consistency per source ID.

Behavior:

```text
same MAC, close samples
  → merge into one canonical position

same MAC, one strong cluster plus tiny far evidence
  → solve the strong cluster
  → mark tiny far evidence as suspicious/outlier

same MAC, multiple strong distant clusters
  → split into MAC#geo1, MAC#geo2, ...
```

Why this exists:

- GPS can stall or jump.
- A parser can misidentify a source.
- MACs/BSSIDs can be reused or spoofed.
- Different devices can occasionally share misleading identifiers.
- A single distant bad sample should not drag an AP away from ten consistent nearby samples.

When a split happens, the child position keeps the parent source ID/MAC in metadata so the UI can still show the relationship.

---

## Path colors

The web map uses a fixed distinct palette for session paths and sample links. Colors are assigned by first-seen path identity and reused for route previews and sample-link overlays.

This is meant to avoid hard-to-read collisions such as:

```text
path 1 blue, path 2 green, path 3 blue, path 4 green
```

The palette is finite, so very large numbers of sessions can still eventually reuse colors, but normal multi-session reviews should remain visually distinct.

---

## Caveats

RSS/RSSI solving is approximate. Results are influenced by:

- path-loss exponent;
- antenna gain and orientation;
- obstruction/NLOS multipath;
- GPS quality and route geometry;
- how many distinct observation positions exist;
- whether the source moved during capture.

Use solved positions for exploration and ranking. For exact localization, compare raw samples, confidence radius, sample links, route geometry, and repeat captures.
