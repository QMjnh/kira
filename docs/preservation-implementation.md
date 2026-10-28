# Preservation foundation: implementation status

October 27, 2026. Implements the first local preservation slice of the image version-control hub. Complete editor compatibility and the wider hub remain under development.

## Implemented

- A local SQLite catalog and operation journal under the chosen working directory's `library/`, immutable content-addressed file copies, and permanent JSON receipts that can reconstruct missing catalog rows. These survive transfer-job cleanup. An OS file lock permits one server writer per working directory and releases on process exit.
- Originals are retained before either individual download or bundle creation. Jobs continue sending their retained original even if its working path changes. Bundle completion merges updates into the latest manifest, preserving concurrent returns.
- Sender SHA-256 in the browser, incremental bounded-memory processing that works without Web Crypto, independent receiver verification, and content-bound resume identities. Different exports with identical names/sizes/timestamps cannot share resume state. Legacy clients without digests receive fresh sessions and explicitly weaker receiver-only verification.
- Destination-volume staging and verified publication for returned files. Receipt and publication intent persist before final output. Retry after a failed manifest save or publication reuses the same file identity and destination. Uploaded staging bytes remain until the commit finishes.
- Source organization checkpoints components, journals moves, verifies outputs, and detects conflicting changes. Moving the source file itself avoids deleting an editor's concurrent writes after a copy. Missing originals are not replaced by unverified same-name files.
- Culling retains byte-distinct metadata variants and exact duplicates. XMP/AAE companions and common native project extensions are included in filename groups; both Adobe-style and darktable-style XMP naming conventions are supported.
- Whole ZIP preservation, per-member hashes/receipts, retained relative paths, opaque unknown components, and no publication of partially verified archives. CRC failures and retries do not leave duplicate or partial extracted files. Unsafe paths, symlinks and extraction budgets are checked.
- Job cleanup verifies permanent copies, keeps final receipts, and refuses partial/unrecorded uploads. Source-linked files stay in their source folders; generic-job files remain restorable from checkpoint storage.
- **Saved files → Restore a copy** on the desktop, with paginated history and collision-safe restoration. History and restore APIs are authenticated and restricted to loopback. Filename associations display as suggestions; no new parent graph is inferred from them.
- Organization warnings are visible and retryable. A former completed flag cannot conceal missing or changed files on repeat verification.

## Validation

The complete Python suite passes **106 tests**, including 24 new preservation tests and one new HTTP history/restore test. The Node/Chromium suite passes **18 tests**, including actual browser checksum submission, saved-file restoration, and incremental SHA-256 comparison with Node's independent cryptographic implementation beyond 512 MiB. Desktop and mobile layout regressions pass; the new saved-files dialog was visually inspected.

Tests use generated fixtures and isolated temporary folders. A real test sends working data from the OS temporary volume to a disposable source folder on the repository volume. Fault injection covers destination failure, receipt failure, final manifest failure, restart recovery, source modification during organization, damaged archives, wrong same-name destinations, changed completed returns, cleanup, and interrupted catalog reconstruction. These tests characterize application/process failures; they do not simulate hardware power loss or every filesystem fault.

Tests use generated fixtures. No existing personal photos were modified, and no live cloud mutations or production-data migrations were performed. Use the regression suite to check the implementation.

## Storage and migration behavior

Checkpoints use additional space on the configured working-data volume. Identical hashes share a permanent object, but new data is staged and verified before publication, so free space is also needed for in-progress copies, archives and extraction. Source organization can retain unselected components as it moves them. The UI and README explain this storage change. No retention/deletion policy for permanent objects has been introduced.

Existing jobs remain readable and are adopted when downloaded, organized, returned to, or cleaned up. Legacy source/return bytes are checked before permanent adoption. A legacy missing file cannot be reconstructed from its name. Existing external imports are not automatically migrated or marked protected.

Back up the entire working directory while Kira is stopped, including objects, receipts and the database. A local checkpoint on one disk is not an independent backup. Keep the original photo drive and other existing backups.

## Next milestones and remaining gates

1. Read-only source enrollment with stable volume identity, library-wide inventory/replicas, migration of retained evidence and explicit unresolved states.
2. Asset-level revision/component manifests and optional edit sessions, including consistent multi-file project checkpoints and branch/history UX.
3. Correct and evaluate visual descriptors, candidate retrieval and ranking on the real edited/resized cases. Current visual thresholds and Photos association behavior have not been changed in this preservation slice.
4. Actual iPad Lightroom/Safari/Wi-Fi round trips, browser suspension and external-drive reconnect tests; supported desktop editor/project restore validation.
5. Independently verified Drive backup and restore, then Photos coverage/organization repairs and reviewed inferred associations.

The code addresses local preservation and basic component inclusion. Full project-save consistency, hard power-loss durability, volume-letter reuse, large-library behavior and physical editor/device validation remain open. Visual association remains part of the planned core product.
