# Releasing RiftSense

Release flow: tag `v*` -> GitHub Actions runs tests, checks PowerShell syntax,
builds the ZIP from tracked files, attests provenance, and publishes a GitHub
release. This document is the human checklist around that automation.

## One-time repository setup (before the first release)

- [ ] **Enable immutable releases**: GitHub repository -> Settings -> Releases ->
  **Immutable releases** -> Enable. Do this *before* the first tagged release:
  releases published before the setting is enabled are not immutable. Once a
  release is immutable its tag and assets cannot be changed; fixes require a
  new version. See "Hotfixes and rollback" below.
- [ ] Confirm Actions is allowed to create releases: Settings -> Actions ->
  General -> Workflow permissions must permit read/write for the repository
  (or leave the default and let the workflow's `permissions:` block request
  `contents: write`, `id-token: write`, `attestations: write`).
- [ ] Make sure the default branch is protected as usual; tags are created from
  reviewed commits only.

## Versioning

- `VERSION` at the repository root holds the current version (`MAJOR.MINOR.PATCH`).
  It is the source of truth for local builds and is surfaced by the app.
- Stable tags: `v1.2.3`.
- Beta tags: `v1.2.3-beta.1` (any tag containing `-` is published as a
  **prerelease**). `install.ps1` and the Scoop manifest only pick stable
  releases.

## Release checklist

1. [ ] Bump `VERSION` to the release version and commit it on `main`:
   `VERSION` must contain `1.2.3`, not `v1.2.3`.
2. [ ] Run the test suite locally from a clean export (this is exactly what CI
   does):
   ```sh
   rm -rf /tmp/rift && mkdir -p /tmp/rift
   git archive HEAD | tar -x -C /tmp/rift
   cd /tmp/rift && python3 -m unittest discover -s ui
   ```
3. [ ] (Optional) Dry-run the release build locally:
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File tools\build-release.ps1
   ```
   This writes `dist/RiftSense-win-x64.zip`,
   `dist/RiftSense-win-x64-v<version>.zip` and `dist/SHA256SUMS.txt`.
4. [ ] Tag and push the tag:
   ```sh
   git tag -a v1.2.3 -m "RiftSense v1.2.3"
   git push origin v1.2.3
   ```
5. [ ] Watch CI. The `release` workflow has these jobs:
   - `test` (ubuntu): Python 3.12, tests run from a `git archive` export of the
     tag, so gitignored runtime files (`champion.json`, `items.json`) are
     absent. Asset-dependent tests skip themselves; a pass is never faked.
   - `ps-syntax` (windows): every tracked `*.ps1` is parsed with
     `System.Management.Automation.Language.Parser`; missing `*.cmd`/`*.iss`
     release files fail the job.
   - `build` (ubuntu): `git archive --format=zip --prefix=RiftSense/` of the
     tag, `VERSION` overwritten inside the zip with the tag (leading `v`
     stripped), `SHA256SUMS.txt` generated, artifacts uploaded.
   - `attest`: `actions/attest-build-provenance@v2` for the ZIPs,
     `SHA256SUMS.txt` and `install.ps1`.
   - `release`: `gh release create` attaches the assets; tags containing `-`
     get `--prerelease`.
6. [ ] Verify the published release (see below).
7. [ ] Update the Scoop manifest baseline and commit it:
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File tools\update-scoop.ps1
   git add packaging/scoop/riftsense.json
   git commit -m "scoop: riftsense 1.2.3"
   ```
   The manifest's `checkver: "github"` / `autoupdate` keep later releases
   flowing to Scoop users automatically; the committed baseline just has to be
   accurate for first installs.

## Assets published per release

| Asset | Purpose |
|---|---|
| `RiftSense-win-x64.zip` | Stable name used by `/releases/latest/download/RiftSense-win-x64.zip` and `install.ps1`. |
| `RiftSense-win-x64-v<ver>.zip` | Same payload under the versioned name referenced by the Scoop manifest. |
| `SHA256SUMS.txt` | SHA-256 of every asset in the release. |
| `install.ps1` | Per-user bootstrap installer for the latest stable release. |

`install.ps1` is fetched at
`https://github.com/lowlune/RiftSense/releases/latest/download/install.ps1`;
the ZIP is fetched at
`https://github.com/lowlune/RiftSense/releases/latest/download/RiftSense-win-x64.zip`.

## Verifying a release

Requires the GitHub CLI (`gh`):

```sh
gh release verify v1.2.3 --repo lowlune/RiftSense
gh release verify-asset v1.2.3 RiftSense-win-x64.zip --repo lowlune/RiftSense
gh attestation verify RiftSense-win-x64.zip --repo lowlune/RiftSense
sha256sum -c SHA256SUMS.txt
```

Immutable releases show a lock in the web UI and cannot be edited or have
assets added/removed after publication.

## Beta channel

- Tag `v1.2.3-beta.1`; CI publishes it with `--prerelease`.
- It will not be served by `/releases/latest/...`, so `install.ps1` and Scoop
  remain on the stable channel.
- Testers can download the beta ZIP from the release page directly or use
  `gh release download v1.2.3-beta.1`.

## Hotfixes and rollback

- Because releases are immutable, you cannot replace an asset. If a release is
  broken, fix forward: bump `VERSION` to the next patch, tag, and publish.
- The previous stable release remains downloadable by tag, e.g.
  `https://github.com/lowlune/RiftSense/releases/download/v1.2.2/RiftSense-win-x64-v1.2.2.zip`.
- Users can still roll back manually: download the older ZIP by tag and extract
  it over the install directory. `install.ps1` always installs the latest
  stable release, so it is not a rollback tool.

## Optional: Inno Setup (per-user EXE)

`packaging/inno/riftsense.iss` builds a per-user installer (no UAC) once the
payload is staged:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\build-release.ps1
Expand-Archive dist\RiftSense-win-x64-v<version>.zip -DestinationPath dist\payload
Move-Item dist\payload\RiftSense dist\RiftSense
iscc packaging\inno\riftsense.iss /DAppVersion=<version>
```

The result is `dist/RiftSense-Setup-<version>.exe`. Attach it to the release
manually only if the pipeline is updated to build it in CI (it is not today).

> Note: the CI workflow template currently lives at `packaging/github-workflows/release.yml` (inactive) because the push token lacks the `workflow` scope. See `packaging/github-workflows/README.md` for activation steps.
