# Analog Agent

Simulator-grounded five-transistor OTA optimization, debugging and explanation
using Python, Azure AI Foundry and ngspice. See [architecture.md](architecture.md)
for system design and authority boundaries, and [EVOLUTION.md](EVOLUTION.md)
for the complete experiment history and run metrics.

Source repository: <https://github.com/shubhangi013/analog-agent>

## Scope and limitations

This is an experimental, simulator-grounded engineering tool, not a
production sign-off flow. Its predictive transistor models are educational;
results do not replace process-qualified simulation or engineering review.
See [model provenance and limitations](models/README.md).

## Requirements

- Windows PowerShell
- Python 3.11+
- ngspice 47 or compatible
- An Azure AI Foundry deployment supporting the OpenAI-compatible v1 Chat
  Completions API

## Install

Run all commands from the repository root:

```powershell
git clone https://github.com/shubhangi013/analog-agent.git
Set-Location analog-agent
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

ngspice is not included. Download the Windows build from
<https://ngspice.sourceforge.io/download.html> and either add its `bin` folder
to `PATH`, extract it to `tools\ngspice` (so `tools\ngspice\Spice64\bin\ngspice_con.exe`
exists), or point `NGSPICE_BIN` at the console executable:

```powershell
$env:NGSPICE_BIN = 'C:\path\to\Spice64\bin\ngspice_con.exe'
& $env:NGSPICE_BIN -v
```

Lookup order is `NGSPICE_BIN`, then `PATH`, then `tools\ngspice`.

### Install the transistor model locally

The PTM model is not distributed with this repository. Before running OTA
simulations or the regression suite, obtain the model under terms that permit
your use and save it as `models\ptm180.lib`. See
[model provenance and setup](models/README.md) for the source and expected
checksum. Model files remain local and are ignored by Git.

## Configure the model

```powershell
Copy-Item .env.example .env
notepad .env
```

Set:

```text
LLM_PROVIDER=azure_foundry
LLM_MODEL=<deployment-name>
AZURE_FOUNDRY_ENDPOINT=https://<resource>.services.ai.azure.com
AZURE_FOUNDRY_API_KEY=<resource-key>
```

`.env` is ignored by Git. Environment variables override values in the file.

## Verify the installation

```powershell
.\.venv\Scripts\python.exe -m agent.smoke
.\.venv\Scripts\python.exe -m pytest tests -q
```

The smoke test checks Python, ngspice and a measured RC response. Unit tests use
mock model endpoints and do not require cloud access.

## Run the agent

Optimize from natural language:

```powershell
.\.venv\Scripts\python.exe -m agent.orchestrator `
  --spec "Minimize power with gain >= 35 dB, UGF >= 25 MHz and phase margin >= 60 deg" `
  --budget 200 `
  --timeout 900
```

Add `--yes` for non-interactive approval. Use `--run-id ID` to resume an
interrupted identical request.

Run diagnostics and explanation:

```powershell
.\.venv\Scripts\python.exe -m agent.debug_mode --planted-fault
.\.venv\Scripts\python.exe -m agent.debug_mode --clipping-fault --budget 6 --steps 4
.\.venv\Scripts\python.exe -m agent.explain
```

## Add the MCP server to Copilot CLI

```powershell
$repoRoot = (Get-Location).Path
$python = (Resolve-Path '.\.venv\Scripts\python.exe').Path

copilot mcp add analog-circuit-engineer `
  --env "PYTHONPATH=$repoRoot" `
  --tools "*" `
  -- $python -m agent.mcp_server
```

Verify:

```powershell
copilot mcp get analog-circuit-engineer --json
```

Restart Copilot CLI after changing MCP server code. Available tools:

- `optimize_circuit`
- `debug_circuit`
- `explain_circuit`
- `get_run_result`

Completed MCP run IDs are immutable. Use a new run ID to retry with a different
budget, timeout, specification or circuit.

`optimize_circuit` starts work in the background and returns a run ID
immediately. Poll `get_run_result` until its status changes from `running`.

## Use the MCP server in VS Code

The repository includes `.vscode\mcp.json`. Open the repository folder in VS
Code, enable the `analog-circuit-engineer` server in Copilot agent mode, and use
the four tools listed above. The configuration uses the workspace virtual
environment and does not contain a machine-specific path.

## Outputs

| Output | Location |
|---|---|
| CLI traces | `traces\<task-id>_<run-id>.jsonl` |
| CLI exports | `runs\exports\<run-id>` |
| MCP evidence and exports | `runs\mcp\<run-id>` |
| Evaluation results | `runs\eval\<evaluation-id>` |

`runs\` and `traces\` are local and ignored by Git. Compact evidence for the
published results (manifests, reports, charts and MCP summaries) is in
`results\`.

An approved export contains `sized.cir`, `ptm180.lib`, `report.md` and
`design.json`.

## View an exported response in ngspice

```powershell
$repoRoot = (Get-Location).Path
$exportDir = Join-Path $repoRoot 'runs\exports\<run-id>'

Copy-Item "$exportDir\sized.cir" "$exportDir\view.cir"
notepad "$exportDir\view.cir"
```

In `view.cir`, replace the `quit` line immediately before `.endc` with:

```text
plot db(v(out))
```

Then launch the GUI build (`ngspice.exe`, not `ngspice_con.exe`; use its full
path if it is not on `PATH`):

```powershell
Push-Location $exportDir
ngspice.exe view.cir
Pop-Location
```

ngspice displays waveforms, not a graphical schematic.

## Run evaluations

```powershell
.\.venv\Scripts\python.exe -m eval.run_eval --strategy both --seed 0
.\.venv\Scripts\python.exe -m eval.spec_list --specs PATH.yaml --strategy both --case-workers 1
```

`eval.spec_list` takes an engineer-supplied YAML specification list. The
50-specification list behind the results below is not included.

## Results

| Evaluation | Agent | Random |
|---|---:|---:|
| Historical 50-spec run | 22/45 | 28/45 |
| Current easy/medium | 17.33/20 +/- 0.47 | 15.33/20 +/- 2.49 |
| Untouched hard/objective | 13.00/18 +/- 1.63 | 9.33/18 +/- 0.94 |

The strongest evidence is the untouched hard/objective result. Random reaches
first verification sooner on matched solved cases; total simulation use is
nearly equal on hard/objective but substantially lower for random on
easy/medium. See [EVOLUTION.md](EVOLUTION.md) for all runs, costs, exclusions,
judge scores and failure analysis.

The compact parser case `at least 30dB gain, 20MHz bandwidth, 60deg phase
margin, 200uW max` is covered by a deterministic regression test and is
accepted as 30 dB, 20 MHz, 60 degrees and 200 uW.

## Repository contents

- `agent\`: orchestration, simulation, verification and MCP interfaces.
- `circuits\` and `models\`: circuit templates and local model setup instructions.
- `tests\`: deterministic regression tests with mocked model endpoints.
- `eval\`, `tasks\` and `prompts\`: evaluation suites, examples and prompts.
- `results\`: compact historical evidence; raw runtime output stays local.
- `architecture.md` and `EVOLUTION.md`: design and experiment history.

## Contributing

Keep changes focused and run the smoke test and regression suite documented
above before submitting a pull request. Never commit credentials, local
databases, raw traces, downloaded tools or virtual environments. Only
placeholder values belong in `.env.example`.

## License status

No license has been selected for the original project code. Public visibility
alone does not grant permission to reuse or redistribute it. Third-party
model provenance is documented in [models/README.md](models/README.md).
The transistor model must be obtained separately and is not included in Git.
