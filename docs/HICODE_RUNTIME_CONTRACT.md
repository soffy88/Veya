# HiCode runtime contract

HiCode is Veya's product name for its coding-executor integration. Reasonix is
the underlying MIT-licensed execution runtime. Veya does not claim Reasonix as
Veya-native and does not redistribute its source as a fork.

## Supported release artifact

The supported production artifact is the Veya server/container image. Its build
installs the exact `reasonix@1.21.3` package from
`deploy/hicode-runtime/package-lock.json`, verifies the package executable
reports `reasonix v1.21.3`, and places it at
`/opt/veya/hicode-runtime/node_modules/.bin/reasonix`.

The Python wheel contains Veya's adapter and coding integration, but does not
carry Node or the Reasonix runtime. A wheel-only deployment is therefore not a
HiCode deployment. It must either run the official server image or provide an
explicitly documented compatible deployment assembly; it must not silently
discover a user's global installation.

## Runtime boundary

```text
MasterAgent
  → hicode_run
  → HicodeExecutorAdapter
  → managed Reasonix 1.21.3
  → isolated workspace / provider
```

`server/hicode_runtime.py` is the single Veya-owned runtime locator and
boundary. Production resolution is deterministic:

1. Veya-managed runtime prefix;
2. explicit `HICODE_BIN` only for development/advanced operation;
3. fail closed.

It does not scan `PATH`, `~/.nvm`, or historical global installations.

The adapter probes the executable version before execution. A missing or
incompatible runtime is unhealthy and is never replaced by `latest` or a
random executable.

## Configuration and credentials

The adapter generates a secret-free Reasonix config under
`HICODE_RUNTIME_DATA_ROOT` (default `~/.veya/hicode-runtime`). Reasonix state
and config do not use the user's global `~/.reasonix` directory. Credential
values remain late-bound in the child environment through `api_key_env`; only
environment-variable names appear in the generated config and diagnostics.

The container entrypoint prepares this Veya-owned runtime directory and starts
the managed executable. It does not write API keys to `.env` or config files.

## 3O packaging

The 3O libraries are assembly dependencies for the server image. The image
copies the checked-out `platform/3O` submodules and sets a deterministic image
`PYTHONPATH` for that assembly. They are not assumed to exist because of the
host working directory. The Python wheel intentionally does not include the
large 3O submodules; importing the wheel's lightweight Veya package is
supported, while the server image is the supported full runtime.

## Attribution

Reasonix upstream: `https://github.com/esengine/DeepSeek-Reasonix`

Reasonix 1.21.3 is distributed under the MIT License, Copyright (c) 2026
Reasonix Contributors. Veya's modifications are the adapter, queue, config
boundary, and deployment integration described above. The pinned package and
integrity evidence are recorded in
`docs/evidence/hicode-upstream.json`.
