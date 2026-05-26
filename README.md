# Update Image Tag (Helm & Kustomize)

A composite GitHub Action that bumps a container image tag inside
**Helm `values.yaml`** or **Kustomize `kustomization.yaml`** files.

- Auto-detects file type (kustomize vs helm)
- Supports glob / multiple files / recursive directory
- Optional `git commit` & push (and target branch for PR flows)
- Pure shell + [`yq`](https://github.com/mikefarah/yq) (v4); no Node deps

## Inputs

| Name                   | Required | Default                    | Description |
| ---------------------- | -------- | -------------------------- | ----------- |
| `files`                | yes      | -                          | Path(s) or glob(s), newline- or comma-separated. e.g. `apps/**/values.yaml` |
| `image`                | yes      | -                          | Image to match. Kustomize: matches by `images[].name`/`newName`. Helm: used when `update-repository=true`. |
| `tag`                  | yes      | -                          | New tag value. |
| `mode`                 | no       | `auto`                     | `auto` \| `kustomize` \| `helm`. |
| `new-name`             | no       | `""`                       | Override `newName` (kustomize) / `repository` (helm). |
| `helm-tag-path`        | no       | `.image.tag`               | yq path for tag in helm values. |
| `helm-repository-path` | no       | `.image.repository`        | yq path for repository in helm values. |
| `update-repository`    | no       | `false`                    | Also update repository/newName. |
| `commit`               | no       | `false`                    | Commit & push the change. |
| `commit-message`       | no       | auto                       | Commit message subject. |
| `source-message`       | no       | `""`                       | Source/upstream commit message; appended as commit body. |
| `branch`               | no       | current                    | Branch to push to (created if missing). |
| `git-user`             | no       | `gitops[bot] <41898282+github-actions[bot]@users.noreply.github.com>` | Git author in `Name <email>` form. |
| `yq-version`           | no       | `v4.44.3`                  | yq version to install if missing. |
| `repository`           | no       | `""`                       | GitOps repo to checkout (`owner/name`). Empty = use already-checked-out repo. |
| `ref`                  | no       | default                    | Branch/ref to checkout in the GitOps repo. |
| `token`                | no       | -                          | Token to checkout & push the GitOps repo. Required when `repository` is set. |

## Outputs

| Name            | Description                                |
| --------------- | ------------------------------------------ |
| `updated-files` | Newline-separated list of changed files.   |
| `changed`       | `true` if any file changed, else `false`.  |

## Usage

### 1) Bump a Kustomize image tag and push

```yaml
name: Bump api image
on:
  workflow_dispatch:
    inputs:
      tag:
        required: true

jobs:
  bump:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v4
      - uses: chasenio/gitops-github-action@v1
        with:
          files: apps/my-app/kustomization/dev/kustomization.yaml
          image: ghcr.io/chasenio/api
          tag: ${{ inputs.tag }}
          commit: "true"
```

### 2) Bump a Helm values file

```yaml
- uses: chasenio/gitops-github-action@v1
  with:
    files: apps/litellm/prod/values.yaml
    image: ghcr.io/berriai/litellm-database
    tag: main-v1.81.14-stable
    mode: helm
    helm-tag-path: .image.tag
    commit: "true"
    commit-message: "chore(litellm): bump to main-v1.81.14-stable"
```

### 3) Recursive / multiple files

```yaml
- uses: chasenio/gitops-github-action@v1
  with:
    files: |
      apps/my-app/**/kustomization.yaml
      apps/foo/*/values.yaml
    image: ghcr.io/chasenio/web
    tag: "26289496502"
    commit: "true"
    branch: bot/bump-web
```

### 4) Open a PR with peter-evans/create-pull-request

```yaml
- uses: chasenio/gitops-github-action@v1
  id: bump
  with:
    files: apps/my-app/kustomization/dev/kustomization.yaml
    image: ghcr.io/chasenio/api
    tag: ${{ github.event.inputs.tag }}

- uses: peter-evans/create-pull-request@v6
  if: steps.bump.outputs.changed == 'true'
  with:
    commit-message: "chore(api): bump to ${{ github.event.inputs.tag }}"
    branch: bot/bump-api
    title: "chore(api): bump to ${{ github.event.inputs.tag }}"
```

## Behavior

- `auto` detection treats a file as kustomize when its basename is
  `kustomization.yaml`/`.yml`, or when `kind: Kustomization`, or when an
  `images:` sequence is present.
- For kustomize, the action finds the entry whose `name` or `newName`
  equals `image`; if none matches it logs a warning and skips that file.
- For helm, it writes the tag at `helm-tag-path` (default `.image.tag`)
  using a yq path expression — so you can target nested charts:
  `helm-tag-path: .subchart.image.tag`.
- yq edits preserve YAML comments and key order (yq v4 default).
