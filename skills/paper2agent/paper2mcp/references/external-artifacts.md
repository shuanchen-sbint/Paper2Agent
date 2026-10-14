# External artifacts: models and reference data

Read when a selected tool needs a model checkpoint, weights, or reference data that is too large, licensed, or variant-specific to ship in the ZIP. Users and clients choose artifacts by **name**; they cannot be expected to know a file path on the server's machine. A tool must never depend on a file that only exists inside the build workspace (tutorial assets, `tmp/`, notebook downloads).

## Declare every artifact

Copy [mcp_artifacts.py](../scripts/mcp_artifacts.py) unchanged to `src/mcp_artifacts.py` and write `src/artifacts.json` next to it. Both ship in the package beside the server entry point. Declare each variant the source documents (for example every published checkpoint), not only the one the tutorials used:

```json
{
  "server": "<repo-name>-mcp",
  "artifacts": [
    {
      "name": "pistachio",
      "kind": "model",
      "description": "Main checkpoint trained on Pistachio; strongest general-purpose model.",
      "default": true,
      "used_by": ["<repo>_predict"],
      "source": "https://github.com/<org>/<repo>#checkpoints",
      "license": "MIT",
      "size_bytes": 7000000000,
      "files": [{"url": "https://.../download/123", "sha256": "<64 hex>", "filename": "pistachio.zip"}],
      "check": ["models.json"]
    },
    {
      "name": "uspto-50k",
      "kind": "model",
      "description": "Benchmark checkpoint trained on USPTO-50K.",
      "used_by": ["<repo>_predict"],
      "source": "https://github.com/<org>/<repo>#checkpoints",
      "supported": false,
      "unsupported_reason": "needs the <repo>[graphium] extra, which this package does not install",
      "check": ["models.json"]
    }
  ]
}
```

| Field | Requirement |
| --- | --- |
| `name` | Short lowercase identifier users pass to tools (`^[a-z0-9][a-z0-9_-]*$`). |
| `kind`, `description` | `model`, `dataset`, or `other`; say what it is and when to choose it. |
| `default` | At most one per kind; tools use it when no name is given. |
| `used_by` | Final exposed tool names that accept it. |
| `source`, `license` | Where the artifact is documented upstream, and its license when stated. |
| `files` | Direct download URLs with a plain `filename`; archives are extracted. Pin `sha256` from the upstream publication, or from the download you verified. Use the host's direct-download URL (for figshare, `https://ndownloader.figshare.com/files/<id>`); browser links can return a bot challenge to scripts. |
| `check` | Relative paths that exist in a correct installation (for example `models.json`). |
| `supported: false` | Keep a documented variant the package cannot run, with `unsupported_reason`; do not drop it silently. |

Run `python src/mcp_artifacts.py check` after editing the manifest.

## Install location

Artifacts live under `$MCP_ARTIFACTS_DIR/<server>/<name>/`, or `src/artifacts/<name>/` when the variable is unset. A deployment host sets `MCP_ARTIFACTS_DIR` to persistent shared storage. If the build host already sets it, use it; otherwise set it to `<project>/artifacts` for the whole run so every stage and runtime check shares one copy.

During source execution, install through the helper instead of leaving downloads in tutorial folders: `python src/mcp_artifacts.py fetch <name>`, or `adopt <name> --from <extracted-dir>` when the tutorial already downloaded and extracted it (hard links, no second copy). Never package artifact contents in the ZIP unless they are small and required by every call.

## Tool interface

- Accept the artifact by name, e.g. `model: Annotated[str, "Checkpoint name from <repo>_list_artifacts; default 'pistachio'"] = "pistachio"`, and resolve it with `from mcp_artifacts import resolve` → `resolve(model)`. `resolve` raises an error naming the install command when it is missing; let it reach the client.
- When a file another tool needs ships inside a declared artifact (for example a template library or vocabulary inside a checkpoint), that tool also accepts the artifact name and resolves the file within it, and the artifact lists that tool in `used_by`. A user must be able to run every tool from artifact names alone; file paths remain for outputs of other tools (chaining) and user data.
- A path parameter (e.g. `model_dir`) may be added only as an optional override when upstream supports user-supplied artifacts (such as checkpoints from a training tool). It is never required and never the documented default route.
- Expose exactly one `<prefix>_list_artifacts` tool returning `{"message": ..., "artifacts": mcp_artifacts.list_artifacts()}`: names, descriptions, defaults, which tools use each, install status, and install commands. It is server plumbing, exempt from the upstream-binding rule, and part of the expected inventory with acceptance cases like any tool.

## Validation

- Acceptance cases pass artifacts by name, not absolute path. Every tool in an artifact's `used_by` has a positive case that names the artifact. Include a positive case for the list tool (assert the default is `installed: true`) and an `error_contains` case for an unknown name (`unknown artifact`).
- Project, clean-runtime, and delivery validation run with the same `MCP_ARTIFACTS_DIR`; artifacts are shared state, not reinstalled per environment. The delivery verifier also runs the extracted package's `mcp_artifacts.py check` and `list`.
- Record in `USAGE.md`: each artifact (name, description, size, license, source), `MCP_ARTIFACTS_DIR`, and `python mcp_artifacts.py fetch --default` as the installation step before first start. Deployers run that command before launching the server.

The `complete` workflow gate rejects acceptance cases that pass model/data arguments by absolute path without a manifest, a modified helper, an invalid manifest, unknown `used_by` tools, and a missing or duplicated `*list_artifacts` tool.
