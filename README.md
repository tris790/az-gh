# az-gh

`az-gh` provides the small `gh` command surface used by the current tooling,
but retrieves pull requests and identity information from Azure DevOps through
the Azure CLI (`az`). The executable keeps the name `gh`, so an existing tool
that runs `gh pr list` or `gh pr diff` does not need a provider-specific change.

## Supported commands

```text
gh --version
gh auth status [--active] [--hostname HOST]
gh api user [--hostname HOST] [--jq EXPRESSION]
gh api graphql -f query=... -f searchQuery=... -F first=N [-f after=...]
gh pr list [--head BRANCH] [--base BRANCH] [--author USER|@me]
           [--state open|closed|all|merged] [--json FIELDS]
           [--jq EXPRESSION] [--limit N] [--repo PROJECT/REPOSITORY|AZURE_URL]
gh pr diff NUMBER [--repo PROJECT/REPOSITORY|AZURE_URL]
gh pr view NUMBER [--json FIELDS] [--repo PROJECT/REPOSITORY|AZURE_URL]
```

The recorded `gh pr list --json number,url,state,headRefName` shape is
preserved. Azure fields are translated as follows: `pullRequestId` becomes
`number`, Azure `active/completed/abandoned` becomes `OPEN/MERGED/CLOSED`,
Azure source and target refs lose their `refs/heads/` prefix, and the Azure
pull-request web link becomes `url`.

`pr diff` uses the Azure DevOps Git diff and item APIs via `az devops invoke`
and emits a standard unified diff on stdout. This keeps the output consumable
by tools that already parse `gh pr diff`.

## Setup

Install Azure CLI and its DevOps extension, then authenticate as usual:

```sh
az extension add --name azure-devops
az login
az devops configure --defaults organization=https://dev.azure.com/ORG project=PROJECT
```

The CLI resolves context in this order:

1. `--repo` (`PROJECT/REPOSITORY`, `ORGANIZATION/PROJECT/REPOSITORY`, or an
   Azure DevOps organization, project, or repository URL)
2. `AZ_GH_AZDO_*` / `AZDO_*` environment variables
3. the current Git `origin` Azure DevOps remote
4. Azure CLI configured defaults

Useful explicit variables are `AZDO_ORG_URL`, `AZDO_PROJECT`,
`AZDO_REPOSITORY`, and `AZDO_USER`. `AZ_GH_AZ` can point to a non-default
Azure CLI executable for testing.

Both Azure DevOps URL forms are accepted. For example, this project can be
selected directly with:

```sh
gh pr list --repo https://tris790.visualstudio.com/ClaudeOps
```

The equivalent modern URL is
`https://dev.azure.com/tris790/ClaudeOps`.

On Linux/macOS, put this directory before other `gh` installations on `PATH`.
On Windows, use the included `gh.cmd`, or install the package:

```sh
python -m pip install .
```

The package installs a cross-platform `gh` console script.

The launcher supports both providers. Commands run from a GitHub checkout,
with a GitHub `--hostname`, or against a GitHub URL are forwarded to the
installed GitHub CLI. Azure DevOps commands use `az`; setting `AZ_GH_AZ`
explicitly forces Azure behavior, which is also useful for tests.

`gh api graphql` supports the pull-request search query shape used by common
GitHub integrations and translates it to Azure DevOps pull requests. It
supports `query`, `searchQuery`, `first`, and an empty or `null` `after` field.

## Command recording

Every invocation produces `start`, zero or more `output`, and `result` records
in `commands.jsonl` by default. Set `AZ_GH_LOG_FILE` to choose another path.
Output chunks are base64 encoded in the same schema as the original wrapper,
with stdout/stderr identified separately and sequence numbers preserving event
order. Logs are created with mode `0600`; a small `.lock` sidecar enables safe
append locking on both POSIX and Windows. Logging is best effort and never
prevents the CLI from running.

## Development

Run the standard-library test suite with:

```sh
python -m unittest discover -s tests -v
```
