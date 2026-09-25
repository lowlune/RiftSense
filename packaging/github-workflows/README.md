# GitHub Actions release workflow (inactive template)

`release.yml` is the release pipeline (tag `v*` → clean-export tests → PowerShell syntax check → ZIP + `SHA256SUMS.txt` + attestations → GitHub Release, prerelease for `-beta` tags).

It is kept here instead of `.github/workflows/` because the currently authenticated GitHub token does not have the **`workflow`** scope, and GitHub rejects pushes that create or modify workflow files with such a token.

## How to activate

Option A — GitHub web UI (no token changes):

1. In the repository, open **Add file → Create new file**.
2. Name it `.github/workflows/release.yml`.
3. Paste the contents of `packaging/github-workflows/release.yml`.
4. Commit directly to `main`.

Option B — CLI with a token that has the `workflow` scope:

```bash
gh auth refresh -h github.com -s workflow
mkdir -p .github/workflows
git mv packaging/github-workflows/release.yml .github/workflows/release.yml
git commit -m "ci: activate release workflow"
git push
```

Option C — classic PAT with `repo` + `workflow` scopes:

```bash
git remote set-url origin https://<user>:<pat>@github.com/lowlune/RiftSense.git
# move the file and push, then reset the remote URL
```

## Before the first release

- Repository **Settings → Releases → Immutable Releases**: enable it so published tags/assets cannot be changed (the workflow also generates a release attestation automatically).
- Ensure Actions are enabled for the repository and the workflow has permission to create releases (the workflow requests `contents: write`, `id-token: write`, `attestations: write`).

See `docs/RELEASING.md` for the full release checklist.
