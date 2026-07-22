# QA and repair routing

| Diagnostic family | Resume node | Change strategy |
| --- | --- | --- |
| duplicate/owner/sequence numbering | `numbering.labels.normalize` or `numbering.owner.resolve` | normalize labels, change owner, or repair sequence |
| template macro/external/embedded | `document.template.inspect` | discard active parts and use profile-only |
| missing/fallback font | `document.layout.resolve` | select installed metric-compatible fallback or request approval |
| missing image/derivative | `asset.resolve` | bind stable artifact or rebuild derivative |
| renderer compile failure | `document.render` | repair classified diagnostic or switch verified renderer |
| overflow/widow/dense page | `document.layout.resolve` | patch anchored spacing, width, keep, or break rule |
| preview-only failure | `preview.render` | invalidate and rebuild preview derivative only |

Before completing, verify package readability, requested formats, revision links, hashes, embedded required assets, numbering uniqueness, actual fonts, page count, clipping, overflow, blank-page ratio, heading hierarchy, captions, tables, cover fields, headers, footers, and download/preview availability.

Never retry the same strategy fingerprint against unchanged input. Persist the diagnostic, attempted strategy, safe checkpoint, and next resume node.
