# ADR-002: Checkpointed long-form transcription

**Status:** Accepted
**Date:** 2026-07-23
**Deciders:** Video Masa maintainers

## Context

Release 3.1.1 proved that the existing Whisper workflow can transcribe a
40-minute podcast when its former ten-minute wrapper timeout is removed.
However, one uninterrupted Whisper process still has poor recovery behavior:
after a late failure, retrying repeats all completed work, and the UI cannot
show meaningful progress beyond “Transcribing.”

The next reliability slice must:

1. preserve the proven single-pass path for short media;
2. keep original-timeline timestamps suitable for SRT export;
3. expose useful progress without depending on Whisper's terminal output;
4. resume completed work after a retryable failure;
5. isolate concurrent job/model outputs; and
6. avoid introducing a database or persistent-library contract.

## Decision

Media at or above the configurable long-form threshold is converted into
fixed-duration, mono 16 kHz WAV chunks in a job-and-model-specific checkpoint
directory. Whisper processes chunks sequentially. After each successful chunk,
Video Masa atomically records its result in a checkpoint manifest.

Retry validates the source identity and manifest, skips completed chunks, and
continues at the first incomplete chunk. Completed segment timestamps are
shifted by each chunk's measured audio offset and merged into one standard
Whisper result, so existing transcript and SRT formatting remain unchanged.

Short media continues to use the existing single-pass Whisper command.
Checkpoint state is transient: it survives a retry while the app is running,
but normal app shutdown still clears it. Cross-launch resume is deferred until
Video Masa has an explicit persistent job library and ownership model.

## Options considered

### Fixed chunks with atomic checkpoints

| Dimension | Assessment |
|---|---|
| Complexity | Medium |
| Recovery value | High |
| Timestamp compatibility | High |
| Short-media regression risk | Low |

**Pros:** deterministic progress, bounded retry cost, isolated outputs, no new
runtime dependency, and straightforward timestamp reconstruction.

**Cons:** words at hard chunk boundaries may have less context, preprocessing
uses additional temporary disk, and progress advances per chunk rather than
per word.

### Parse one Whisper process's terminal progress

| Dimension | Assessment |
|---|---|
| Complexity | Low to medium |
| Recovery value | Low |
| Timestamp compatibility | High |
| Short-media regression risk | Medium |

**Pros:** no audio splitting and continuous terminal-derived percentage.

**Cons:** CLI output is not a stable API, progress cannot provide checkpoints,
and a late failure still repeats the entire recording.

### Persist whole jobs across app restarts

| Dimension | Assessment |
|---|---|
| Complexity | High |
| Recovery value | Highest |
| Timestamp compatibility | High |
| Short-media regression risk | High |

**Pros:** survives crashes, reboots, and upgrades.

**Cons:** requires durable job schemas, migration, media ownership, retention
policy, disk quotas, and a startup recovery UI. Those are persistent-library
decisions rather than a contained transcription change.

## Consequences

- Long recordings gain structured prepare/transcribe/finalize progress.
- Retry resumes from the most recent completed chunk.
- Each job/model attempt owns its outputs, removing long-form filename races.
- Existing transcript, timestamp, and SRT consumers receive the same merged
  result shape as single-pass Whisper.
- A failed long-form job retains both its source and valid checkpoints.
- Successful jobs and explicit cleanup remove their checkpoint directories.
- Cross-launch resume remains intentionally unsupported in this release.

## Follow-up

1. Measure boundary accuracy on conversational recordings and tune chunk size.
2. Add cancellation using process-group termination.
3. Add durable manifests only with the persistent local library.
4. Consider silence-aware boundaries or overlapping alignment if field tests
   reveal clipped words at fixed boundaries.
