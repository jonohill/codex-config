#!/usr/bin/env python3
#
# configure-codex.py
#
# Configure the Codex CLI to use a cloud model *directly* through its
# OpenAI-compatible endpoint (no local proxy required), with:
#
#   * the original Codex system prompt injected as the model's base
#     instructions (so harness-specific advice is preserved),
#   * a supplemental sandbox/escalation prompt appended to the base
#     instructions (kept separate for easy editing), and
#   * a proper model catalog so Codex knows the real context window, and
#   * the guardian / auto-review approval agent enabled.
#
# Run this on any machine and plain `codex` will launch against the configured
# cloud model. It is safe to re-run (idempotent); existing project trust
# entries in config.toml are preserved.
#
# Requirements:
#   * `codex` installed (https://developers.openai.com/codex/install)
#   * the provider's API key exported (env var name lives in config.default.toml)
#
# Provider/model defaults are declarative: see config.default.toml (override
# with --config <path>). The shipped defaults target Ollama Cloud / GLM 5.2.
#
# Usage:
#   ./configure-codex.py
#   ./configure-codex.py --config ./my-provider.toml
#   ./configure-codex.py --model glm-5.2:cloud --base-url https://ollama.com/v1
#   ./configure-codex.py --context-window 1000000 --no-probe
#
# Override defaults with flags or env vars (highest priority first):
#   CLI flags  >  CODEX_MODEL, CODEX_BASE_URL, CODEX_CONTEXT_WINDOW, <env_key>,
#   CODEX_HOME  >  config.default.toml

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Embedded Codex system prompt (the built-in BASE_INSTRUCTIONS shipped with
# codex-rs/models-manager/prompt.md). Injected as the model's base_instructions
# so the harness-specific operating advice is present even for custom models.
# ---------------------------------------------------------------------------
# Prompt files. The base instructions are the verbatim Codex
# BASE_INSTRUCTIONS (codex-rs/models-manager/prompt.md); the supplement adds
# sandbox/escalation guidance. Both ship as editable files under prompts/ and
# are injected as the model's base_instructions (base + supplement).
BASE_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "codex-base-instructions.md"
SUPPLEMENT_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "codex-supplement-instructions.md"

INSTRUCTIONS_FILE = "codex-base-instructions.md"
SUPPLEMENT_FILE = "codex-supplement-instructions.md"

# Path to the bundled default provider/model configuration. Edit that file
# (or pass --config <path>) to target a different OpenAI-compatible provider
# or model. The shipped defaults describe Ollama Cloud / GLM 5.2.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.default.toml"


def die(msg, code=1):
    print(f"configure-codex: error: {msg}", file=sys.stderr)
    sys.exit(code)


def info(msg):
    print(f"configure-codex: {msg}")


def warn(msg):
    print(f"configure-codex: warning: {msg}", file=sys.stderr)


def system_prompt() -> str:
    return BASE_PROMPT_PATH.read_text("utf-8")


def supplement_prompt() -> str:
    return SUPPLEMENT_PROMPT_PATH.read_text("utf-8")


# ---------------------------------------------------------------------------
# Small TOML writer (no third-party deps).
# ---------------------------------------------------------------------------
def _escape_str(s: str) -> str:
    return s.replace("\\", "\\\\").replace("\"", "\\\"")


def _toml_key(k: str) -> str:
    if k and all(c.isalnum() or c in "_-." for c in k) and not k.startswith("."):
        # bare keys may not contain '.' when used as a single key segment; we
        # only call _toml_key on individual segments, so allow alnum/_- here.
        if all(c.isalnum() or c in "_-" for c in k):
            return k
    return "\"" + _escape_str(k) + "\""


def _toml_val(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return "\"" + _escape_str(v) + "\""
    raise TypeError(f"unsupported TOML value type: {type(v)}")


def _write_table(lines, path, table):
    has_scalar = any(not isinstance(v, dict) for v in table.values())
    # Emit a table header only when the table holds scalar values. Pure
    # container tables (e.g. [model_providers] with only nested providers)
    # are not written as a bare header; their leaf tables carry full dotted
    # paths like [model_providers.ollama-cloud].
    if has_scalar:
        header = ".".join(_toml_key(seg) for seg in path)
        lines.append(f"\n[{header}]")
        for k, v in table.items():
            if not isinstance(v, dict):
                lines.append(f"{_toml_key(k)} = {_toml_val(v)}")
    for k, v in table.items():
        if isinstance(v, dict):
            _write_table(lines, path + [k], v)


def dump_toml(root: dict, top_order) -> str:
    lines = []
    for k in top_order:
        if k in root and not isinstance(root[k], dict):
            lines.append(f"{_toml_key(k)} = {_toml_val(root[k])}")
    for k, v in root.items():
        if isinstance(v, dict):
            _write_table(lines, [k], v)
    return "\n".join(lines).strip() + "\n"


# ---------------------------------------------------------------------------
# Cloud endpoint probe.
# ---------------------------------------------------------------------------
class ProbeError(Exception):
    """Raised when the cloud endpoint probe fails."""


def probe_models(base_url: str, api_key: str, timeout: float):
    """GET {base_url}/models with bearer auth. Returns a list of model ids.

    Raises ProbeError on any failure so the caller can decide whether to abort.
    """
    url = base_url.rstrip("/") + "/models"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise ProbeError(f"HTTP {e.code} from {url}: {body}")
    except urllib.error.URLError as e:
        raise ProbeError(f"could not reach {url}: {e.reason}")
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        raise ProbeError(f"non-JSON response (first 200 bytes): {payload[:200]!r}")
    ids = []
    if isinstance(data, dict):
        for key in ("data", "models"):
            lst = data.get(key)
            if isinstance(lst, list):
                for m in lst:
                    if isinstance(m, dict):
                        mid = m.get("id") or m.get("model") or m.get("name")
                        if mid:
                            ids.append(mid)
                if ids:
                    break
    return ids


# ---------------------------------------------------------------------------
# Main.
# ---------------------------------------------------------------------------
def load_config(path: Path) -> dict:
    """Load the declarative provider/model config (TOML).

    The file describes the cloud provider and default model so the same script
    can target any OpenAI-compatible endpoint by swapping the config file.
    """
    if not path.exists():
        die(f"config file not found: {path}")
    try:
        import tomllib
    except ModuleNotFoundError:
        die("tomllib is required to read the config file (Python 3.11+).")
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception as e:
        die(f"could not parse config {path}: {e}")


def parse_args():
    p = argparse.ArgumentParser(
        description="Configure Codex CLI for a cloud model via its "
                    "OpenAI-compatible endpoint. Provider/model defaults come "
                    "from a declarative config file (see --config).")
    p.add_argument("--config", default=str(DEFAULT_CONFIG_PATH),
                   help=f"path to provider/model config TOML (default: "
                        f"{DEFAULT_CONFIG_PATH})")
    p.add_argument("--model", default=None,
                   help="model id to use (overrides config/env)")
    p.add_argument("--base-url", default=None,
                   help="OpenAI-compatible base URL (overrides config/env)")
    p.add_argument("--api-key", default=None,
                   help="API key (default: $<env_key> from config)")
    p.add_argument("--context-window", type=int, default=None,
                   help="model context window in tokens (overrides config/env)")
    p.add_argument("--codex-home", default=os.environ.get(
        "CODEX_HOME", str(Path.home() / ".codex")),
                   help="Codex home directory (default: ~/.codex)")
    p.add_argument("--no-probe", action="store_true",
                   help="skip the live endpoint probe")
    p.add_argument("--yes", action="store_true",
                   help="do not prompt before overwriting config.toml")
    return p.parse_args()


def find_codex():
    return shutil.which("codex")


def read_existing_config(path: Path):
    """Return a dict of the existing config.toml (best-effort, tomllib)."""
    if not path.exists():
        return {}
    try:
        import tomllib
    except ModuleNotFoundError:
        # Fallback: extract [projects.*] and [tui.*] tables line-by-line.
        return _parse_config_fallback(path)
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception as e:
        warn(f"existing config.toml could not be parsed ({e}); preserving via "
             f"line-based fallback.")
        return _parse_config_fallback(path)


def _parse_config_fallback(path: Path):
    """Preserve [projects.*] trust_level entries and [tui.*] entries."""
    out = {"projects": {}, "tui": {}}
    cur = None
    cur_key = None
    for raw in path.read_text("utf-8", "replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            header = line[1:-1].strip()
            if header.startswith("projects."):
                cur = "projects"
                cur_key = header[len("projects."):].strip().strip('"')
                out["projects"].setdefault(cur_key, {})
            elif header.startswith("tui."):
                cur = "tui"
                cur_key = header[len("tui."):].strip().strip('"')
                out["tui"].setdefault(cur_key, {})
            elif header == "tui":
                cur = "tui"
                cur_key = None
            else:
                cur = None
                cur_key = None
            continue
        if cur is None:
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"')
            if cur_key:
                out[cur][cur_key][k] = v
            else:
                out[cur][k] = v
    return out


def build_model_catalog(model: str, context_window: int, prompt: str):
    auto_compact = (context_window * 9) // 10
    return {
        "models": [
            {
                "slug": model,
                "display_name": model,
                "supported_reasoning_levels": [],
                "shell_type": "default",
                "visibility": "list",
                "supported_in_api": True,
                "priority": 0,
                "base_instructions": prompt,
                "supports_reasoning_summaries": False,
                "support_verbosity": False,
                "truncation_policy": {"mode": "tokens", "limit": 10000},
                "supports_parallel_tool_calls": False,
                "experimental_supported_tools": [],
                "input_modalities": ["text"],
                "context_window": context_window,
                "max_context_window": context_window,
                "auto_compact_token_limit": auto_compact,
                "effective_context_window_percent": 95,
            }
        ]
    }


def write_text(path: Path, content: str, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, "utf-8")
    os.chmod(tmp, mode)
    tmp.replace(path)


def main():
    args = parse_args()

    cfg = load_config(Path(args.config))
    provider = cfg.get("provider", {})
    model_cfg = cfg.get("model", {})

    provider_id = provider.get("id")
    provider_name = provider.get("name", provider_id)
    env_key = provider.get("env_key")
    wire_api = provider.get("wire_api", "responses")
    env_key_instructions = provider.get("env_key_instructions", "")
    key_url = provider.get("key_url", "")
    catalog_file = model_cfg.get("catalog_file", f"{provider_id}-models.json")

    def env(*names):
        for name in names:
            value = os.environ.get(name)
            if value:
                return value
        return None

    model = args.model or env("CODEX_MODEL") or model_cfg.get("id")
    base_url = (args.base_url or env("CODEX_BASE_URL", "OLLAMA_CLOUD_BASE_URL")
                or provider.get("base_url"))
    ctx = args.context_window or env("CODEX_CONTEXT_WINDOW") or model_cfg.get("context_window")
    api_key = args.api_key or (env_key and env(env_key))

    if not all([provider_id, env_key, model, base_url, ctx]):
        die(f"config is incomplete (provider.id={provider_id!r}, "
            f"provider.env_key={env_key!r}, provider.base_url={base_url!r}, "
            f"model.id={model!r}, model.context_window={ctx!r}). "
            f"Check {args.config}.")
    ctx = int(ctx)

    codex_bin = find_codex()
    if not codex_bin:
        die("`codex` was not found on PATH. Install it first: "
            "npm install -g @openai/codex")
    info(f"found codex: {codex_bin}")
    info(f"using provider config: {args.config} "
         f"(provider={provider_id}, model={model})")

    if not api_key:
        hint = f" Create a key at {key_url} and export {env_key}," if key_url else ""
        die(f"${env_key} is not set.{hint} or pass --api-key.")
    if api_key.strip().lower() == provider_id or len(api_key) < 12:
        warn(f"${env_key} looks like a placeholder ('{api_key}')."
             + (f" The endpoint needs a real API key from {key_url}."
                if key_url else ""))

    base_url = base_url.rstrip("/")

    # Probe the live endpoint for early feedback. Failures are non-fatal:
    # we still write the config so `codex` itself can report any real problem.
    if not args.no_probe:
        info(f"probing {base_url}/models ...")
        try:
            ids = probe_models(base_url, api_key, timeout=20.0)
        except ProbeError as e:
            warn(f"endpoint probe failed ({e}). Writing config anyway — run "
                 f"`codex` to confirm, or re-run with --no-probe to silence.")
            ids = None
        if ids:
            info(f"endpoint reachable; {len(ids)} model(s) listed.")
            if model not in ids:
                warn(f"model '{model}' was not in the listed models "
                     f"({', '.join(ids[:8])}{'...' if len(ids) > 8 else ''}). "
                     f"Proceeding anyway — some listings omit cloud models; the "
                     f"id is sent verbatim in requests.")
        elif ids is not None:
            warn("endpoint reachable but listed no models. Proceeding anyway.")
    else:
        info("skipping live endpoint probe (--no-probe).")

    codex_home = Path(args.codex_home).expanduser()
    codex_home.mkdir(parents=True, exist_ok=True)

    # 1. Write the injected Codex system prompt.
    #    (verbatim) and the supplemental sandbox/escalation instructions.
    prompt = system_prompt()
    instr_path = codex_home / INSTRUCTIONS_FILE
    write_text(instr_path, prompt, mode=0o644)
    info(f"wrote system prompt -> {instr_path}")
    supplement = supplement_prompt()
    supplement_path = codex_home / SUPPLEMENT_FILE
    write_text(supplement_path, supplement, mode=0o644)
    info(f"wrote supplement instructions -> {supplement_path}")

    # The catalog base_instructions combines both so the running model sees
    # the verbatim out-of-the-box prompt plus the sandbox/escalation supplement.
    prompt = prompt + "\n\n" + supplement

    # 2. Write the model catalog (gives Codex the real context window and the
    #    injected base_instructions).
    catalog = build_model_catalog(model, ctx, prompt)
    catalog_path = codex_home / catalog_file
    write_text(catalog_path, json.dumps(catalog, indent=2) + "\n", mode=0o644)
    info(f"wrote model catalog -> {catalog_path}")

    # 3. Build/merge ~/.codex/config.toml.
    config_path = codex_home / "config.toml"
    existing = read_existing_config(config_path)

    # Preserve project trust + tui entries.
    projects = existing.get("projects", {}) if isinstance(existing, dict) else {}
    tui = existing.get("tui", {}) if isinstance(existing, dict) else {}

    root = {}
    root["model"] = model
    root["model_provider"] = provider_id
    root["model_catalog_json"] = str(catalog_path)
    root["model_context_window"] = ctx
    root["approval_policy"] = "on-request"
    root["approvals_reviewer"] = "auto_review"

    root["model_providers"] = {
        provider_id: {
            "name": provider_name,
            "base_url": base_url + "/",
            "env_key": env_key,
            "env_key_instructions": env_key_instructions,
            "wire_api": wire_api,
        }
    }

    # NOTE: [auto_review].policy is intentionally NOT set. The guardian ships
    # with a comprehensive built-in tenant policy (codex-rs .../guardian/policy.md).
    # Setting `auto_review.policy` *replaces* that whole default, so we leave it
    # unset to keep the full built-in rubric. Add it only to override with an
    # org-specific policy (see the comment appended to the generated TOML).

    if projects:
        root["projects"] = {k: v for k, v in projects.items() if isinstance(v, dict)}
    if tui:
        root["tui"] = {k: v for k, v in tui.items() if isinstance(v, dict)}

    top_order = ["model", "model_provider", "model_catalog_json",
                 "model_context_window", "approval_policy", "approvals_reviewer"]
    new_toml = (
        "# Managed by configure-codex.py — Codex CLI configuration.\n"
        f"# {provider_name} is reached directly via its OpenAI-compatible endpoint.\n"
        "# Re-run configure-codex.py to regenerate; existing project trust\n"
        "# entries are preserved. Edit freely below.\n"
        "#\n"
        "# Guardian: the auto-reviewer uses its built-in tenant policy by\n"
        "# default. To override that rubric, uncomment and set a policy:\n"
        "#\n"
        "# [auto_review]\n"
        "# policy = \"...your org-specific risk rules here...\"\n\n"
        + dump_toml(root, top_order)
    )

    # Backup + confirm.
    if config_path.exists():
        bak = config_path.with_name(f"config.toml.bak.{int(time.time())}")
        shutil.copy2(config_path, bak)
        info(f"backed up existing config -> {bak}")
        if not args.yes:
            print("\n" + new_toml)
            ans = input(f"\nOverwrite {config_path} with the above? [y/N] ").strip().lower()
            if ans not in {"y", "yes"}:
                info("aborted; no changes written to config.toml.")
                return

    write_text(config_path, new_toml, mode=0o600)
    info(f"wrote config -> {config_path}")

    # Sanity check: ask codex to load config (non-interactive, no model call).
    info("verifying config loads with codex ...")
    try:
        proc = subprocess.run(
            [codex_bin, "--version"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=20)
        print("  " + proc.stdout.strip().replace("\n", "\n  "))
    except Exception as e:
        warn(f"could not run `codex --version` for verification: {e}")

    print()
    info("done. Launch with:")
    print(f"    {codex_bin}")
    print(f"  (model: {model}  provider: {provider_id}  "
          f"endpoint: {base_url}/responses)")
    print()
    print("Notes:")
    print("  * The guardian auto-reviewer handles approval escalations using the")
    print("    same cloud model. It uses the built-in tenant policy by default;")
    print("    override it via [auto_review].policy if you need org-specific rules.")
    print("  * To switch models later, re-run with --model <id> "
          "[--context-window N].")
    if args.no_probe:
        print("  * Endpoint was not probed; run without --no-probe to verify "
              "connectivity/auth.")


if __name__ == "__main__":
    main()
