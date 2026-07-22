# Layout contracts

## Decision inputs

- Explicit user theme, template mode, fonts, sizes, margins, numbering, and formats have highest product-level precedence.
- Project preferences apply only when the current request is silent.
- A template owns heading numbering only when numbering.xml links levels to semantic Heading styles.
- Low-confidence template intent returns clarify; dangerous packages return profile-only.

## Canonical outputs

- NumberingContract: one owner for headings, lists, figures, tables, equations, appendices, and pages.
- TypographyTheme: renderer-neutral PageSpec, font roles, NamedStyle tokens, component tokens, numbering template, and visual rules.
- TemplateContractV2: source hash, sections, styles, semantic map, numbering, fonts, chrome, tables, slots, fixed hashes, capabilities, fidelity, diagnostics.
- LayoutProfile: resolved theme/page/style/numbering/TOC/pagination plus provenance.

## Revision invariants

- Style-only: content, asset, citation, experiment, and numbering hashes stay unchanged.
- Numbering-only: content, asset, citation, experiment, and presentation hashes stay unchanged.
- Template repair: content, asset, and citation hashes stay unchanged.
- All requested formats share one document id and revision.
