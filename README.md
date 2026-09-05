# az-gh
Replacement for `gh` cli that works on Azure Devops instead. Relies on `az` cli.
To launch an app on linux the USER can use PATH="/home/nsa/repo/python/az-gh:$PATH" app

## Setup

Install the Azure DevOps extension, authenticate, and configure the
organization and project as separate defaults:

```sh
az extension add --name azure-devops
az login
az devops configure --defaults organization=https://dev.azure.com/ORG project=PROJECT
python -m pip install /path/to/az-gh
```

The shim has no Python runtime dependencies. If running the checked-out
`gh.cmd` directly, package installation is optional.

The `organization` value must end at the organization name. For a URL such as
`https://dev.azure.com/ORG/PROJECT`, use
`organization=https://dev.azure.com/ORG project=PROJECT`.

Verify that the shim, rather than the official GitHub CLI, is being invoked:

```sh
gh --version
```

The output should contain `Azure DevOps`. If it does not, put the installed
shim first on `PATH` (or use the included `gh.cmd` on Windows).

If `gh pr list` reports Windows `WinError 2`, verify that Azure CLI itself is
available in the same terminal with `az --version`. On Windows, restart the
terminal after installing Azure CLI so its `az.cmd` directory is included in
`PATH`.

To bypass the Azure DevOps shim and run the real GitHub CLI at `/usr/bin/gh`,
set `AZ_GH_PASSTHROUGH`:

```sh
AZ_GH_PASSTHROUGH=1 gh pr list
```

All arguments, input, output, and the exit status are passed through to the
official CLI.

## Inspecting command recordings

The JSONL recordings can be inspected without manually decoding base64 output:

```sh
python tools/parse_jsonl.py official_commands.jsonl --summary
python tools/parse_jsonl.py az-gh-commands.jsonl --shapes
python tools/parse_jsonl.py az-gh-commands.jsonl --json-output 2
```

The shape view omits values while retaining object fields and JSON value types,
which is useful when comparing GitHub and Azure responses for different PRs.

## Replaying the official command contract

The compatibility suite replays every `argv` in `official_commands.jsonl` in
order. It compares command order, output streams, exit codes, and
value-independent output shapes; mismatches include the JSON path where a
field is missing, unexpected, or has a different type.

Repository contents are allowed to differ between the GitHub recording and
Azure replay: empty versus populated connections, nullable values, GraphQL
error envelopes, and success versus not-found outcomes are treated as
data-dependent. Stable field presence and concrete value types are still
checked.

Run the deterministic suite with:

```sh
python -m unittest discover -s tests -v
python tools/replay_compat.py official_commands.jsonl
```

The replay command uses the checked-in Azure fixture by default. To exercise a
live Azure CLI instead, provide `--az az` and the Azure context flags, for
example `--org https://dev.azure.com/ORG --project PROJECT --repository REPO`.

For a focused recording containing only some GraphQL commands, compare by
normalized query structure with `--actual capture.jsonl --partial`.

Azure repository URLs may be passed with or without the `https://` prefix, for
example `gh pr list --repo dev.azure.com/ORG/PROJECT`. The shim also discovers
the organization and project from the current Git `origin` remote when the
remote uses a full Azure repository URL.
