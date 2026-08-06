---
name: release-prep
description: >-
  Prepare and publish an Avalanche release from main. Use when asked to prepare,
  version, tag, or push a release. Accepts an optional release version.
metadata:
  author: Avalanche
  version: "1.0"
---

# Prepare a release

**Input:** optional version. Accept `0.1.0`, `0.1.0-rc3`, `0.1.0rc3`, or the
corresponding `v`-prefixed tag. Normalize it to:

- package version: PEP 440 (`0.1.0rc3`)
- changelog version: hyphenated (`0.1.0-rc3`)
- Git tag: `v`-prefixed and hyphenated (`v0.1.0-rc3`)

Do not perform any release action until all preflight checks pass and the user
explicitly approves the proposed version. Do not switch branches, stash,
discard, commit, or otherwise repair a failed preflight.

## 1. Mandatory preflight

Run these commands from the repository root before reading tags, modifying
files, creating a commit or tag, or pushing:

```bash
test "$(git branch --show-current)" = main \
  || { echo "Release preparation requires the main branch." >&2; exit 1; }
test -z "$(git status --porcelain=v1 --untracked-files=all)" \
  || { echo "Release preparation requires a completely clean worktree." >&2; exit 1; }
git remote get-url origin >/dev/null \
  || { echo "Release publication requires an origin remote." >&2; exit 1; }
```

If any check fails, stop and report the exact failure. Do not inspect tags,
propose a version, edit files, commit, create a tag, or push.

## 2. Resolve a proposed version

If the user supplied a version, validate and normalize it with this script.
Set `RELEASE_VERSION` exactly to the supplied value; leading or trailing
whitespace is invalid. It prints `<tag> <package-version> <changelog-version>`.

```bash
RELEASE_VERSION='<user-input>' uv run python - <<'PY'
from __future__ import annotations

import os
import re

match = re.fullmatch(
    r"v?(?P<base>(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))"
    r"(?:-?(?P<channel>alpha|beta|rc)(?P<serial>0|[1-9]\d*))?",
    os.environ["RELEASE_VERSION"],
)
if match is None:
    raise SystemExit("Invalid release version.")

base = match["base"]
channel = match["channel"]
if channel is None:
    print(f"v{base} {base} {base}")
else:
    serial = match["serial"]
    package_channel = {"alpha": "a", "beta": "b", "rc": "rc"}[channel]
    print(f"v{base}-{channel}{serial} {base}{package_channel}{serial} {base}-{channel}{serial}")
PY
```

Reject a tag that already exists.

Without input, infer the proposal from the highest supported existing tag. Read
only tags matching `vMAJOR.MINOR.PATCH` with an optional
`-alphaN`, `-betaN`, or `-rcN` suffix. Ignore unrelated tags. Use this policy:

- Latest stable tag → increment patch: `v1.2.3` becomes `v1.2.4`.
- Latest prerelease tag → increment that prerelease serial without changing its
  base or channel: `v1.2.3-rc4` becomes `v1.2.3-rc5`.
- No supported release tags → stop and ask for an explicit version; do not guess.

Use this script to compute the proposal. It performs no mutation:

```bash
uv run python - <<'PY'
from __future__ import annotations

import re
import subprocess

pattern = re.compile(
    r"^v(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<channel>alpha|beta|rc)(?P<serial>0|[1-9]\d*))?$"
)
channel_order = {"alpha": 0, "beta": 1, "rc": 2, None: 3}
releases = []
for tag in subprocess.run(
    ["git", "tag", "--list"], check=True, text=True, capture_output=True
).stdout.splitlines():
    match = pattern.fullmatch(tag)
    if match is None:
        continue
    parts = match.groupdict()
    releases.append((
        (
            int(parts["major"]),
            int(parts["minor"]),
            int(parts["patch"]),
            channel_order[parts["channel"]],
            int(parts["serial"] or 0),
        ),
        parts,
    ))

if not releases:
    raise SystemExit("No supported release tags; provide an explicit version.")

_, latest = max(releases)
major, minor, patch = (int(latest[name]) for name in ("major", "minor", "patch"))
channel = latest["channel"]
if channel is None:
    proposal = f"v{major}.{minor}.{patch + 1}"
else:
    proposal = f"v{major}.{minor}.{patch}-{channel}{int(latest['serial']) + 1}"
print(proposal)
PY
```

Before asking for approval, ensure `git rev-parse -q --verify
"refs/tags/$TAG"` fails for the normalized proposed tag. Then present:

```text
Proposed release: <tag>
pyproject.toml version: <package-version>
CHANGELOG.md heading: <changelog-version>
A release commit containing only those two files will be created, then tagged and pushed to origin.
Proceed?
```

Wait for an explicit affirmative response that names or unambiguously accepts
the proposal. A version suggestion alone is not approval.

## 3. Apply the approved release

Re-run the mandatory preflight after approval. Revalidate that the selected tag
does not exist. Update only these release artifacts:

1. Change `[project].version` in `pyproject.toml` to the normalized package
   version.
2. Move the entire non-empty body immediately below `## Unreleased` in
   `CHANGELOG.md` under a new `## <changelog-version>` heading, leaving a new
   empty `## Unreleased` heading above it. Preserve every release-note line.

Do not manufacture release notes. If `Unreleased` has no content, stop and
report that there is nothing to release.

Verify the intended diff before committing:

```bash
git diff --check
test "$(git diff --name-only)" = $'CHANGELOG.md\npyproject.toml' \
  || { echo "Only CHANGELOG.md and pyproject.toml may change." >&2; exit 1; }
grep -Fx "version = \"<package-version>\"" pyproject.toml >/dev/null \
  || { echo "pyproject.toml version did not match the approved release." >&2; exit 1; }
grep -Fx "## <changelog-version>" CHANGELOG.md >/dev/null \
  || { echo "CHANGELOG.md release heading was not created." >&2; exit 1; }
```

A tag identifies a commit, not uncommitted files. Therefore create one release
commit before tagging; never tag a pre-release `HEAD` that lacks the version
and changelog updates:

```bash
git add pyproject.toml CHANGELOG.md
git commit -m "Release <tag>"
test "$(git branch --show-current)" = main
test -z "$(git status --porcelain=v1 --untracked-files=all)"
```

If the commit fails, stop. Do not create or push a tag.

## 4. Tag, push, and verify

Create an annotated tag and push that tag only:

```bash
git tag -a "<tag>" -m "Release <tag>"
git push origin "<tag>"
```

If the push fails, report the failed command and leave the local tag intact; do
not delete it or force-push. Confirm the published release points at the release
commit and carries the approved files:

```bash
test "$(git rev-list -n 1 "<tag>")" = "$(git rev-parse HEAD)"
git show "<tag>:pyproject.toml" | grep -Fx 'version = "<package-version>"'
git show "<tag>:CHANGELOG.md" | grep -Fx '## <changelog-version>'
```

Report the release tag, commit SHA, pushed remote (`origin`), and the exact
verification commands that passed.
