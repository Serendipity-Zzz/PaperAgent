# Nature Skills integration notice

PaperAgent integrates the upstream `nature-figure` workflow by installing a complete, pinned upstream checkout after security review and user approval. It does not execute the upstream installer automatically and does not copy only `SKILL.md`.

- Upstream: https://github.com/Yuan1z0825/nature-skills
- Pinned commit: `3169759afa66ef5286108b77b4a7f72544ad4d46`
- License: Apache-2.0
- Required complete directories: `skills/nature-figure`, `skills/nature-shared`
- Required root files: `LICENSE`, `README.md`

The installed snapshot retains upstream `SKILL.md`, manifests, static fragments, references, scripts, assets, evals and shared support files. PaperAgent's Seedream adapter is maintained separately and does not modify the upstream snapshot.
