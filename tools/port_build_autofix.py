#!/usr/bin/env python3
"""Universal Build Triage and Copilot Auto-Fix Tool for zopencommunity ports.

This tool:
1. Parses the port's buildenv to extract upstream source URL, version tags, submodules, and patches.
2. Runs `zopen-build -vv` (or dry-run patch verification) on the port checkout.
3. On failure, analyzes the error log and identifies broken patches or build steps.
4. Extracts rejected patch hunks, upstream diffs, and target source files.
5. Generates a structured context bundle for GitHub Copilot to produce the exact fix.
6. Validates newly generated patches with `git apply --check`.

Usage:
  python3 tools/port_build_autofix.py --port-dir ./syftport --meta-dir . --output /tmp/autofix_context.md
  python3 tools/port_build_autofix.py --port-dir . --test-patch-fix /tmp/fix.patch
"""

from __future__ import annotations

import argparse
import glob
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request


def parse_buildenv(port_dir: Path) -> dict:
    """Parse buildenv file from port directory into key-value pairs and module mappings."""
    buildenv_file = port_dir / "buildenv"
    if not buildenv_file.exists():
        raise FileNotFoundError(f"buildenv not found in {port_dir}")

    content = buildenv_file.read_text(encoding="utf-8", errors="replace")
    
    # Extract simple export VAR="VAL" and VAR="VAL" definitions
    env_vars = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r'^(?:export\s+)?([A-Za-z0-9_]+)=(?:"([^"]*)"|\'([^\']*)\'|([^#\s]+))', line)
        if m:
            key = m.group(1)
            val = m.group(2) if m.group(2) is not None else (m.group(3) if m.group(3) is not None else m.group(4))
            env_vars[key] = val

    # Resolve variable substitutions like ${SYFT_VERSION} in ZOPEN_STABLE_TAG
    for k, v in list(env_vars.items()):
        if isinstance(v, str) and "${" in v:
            for sub_k, sub_v in env_vars.items():
                v = v.replace(f"${{{sub_k}}}", sub_v).replace(f"${sub_k}", sub_v)
            env_vars[k] = v

    # Extract clone_module calls: clone_module <url> <dir> <ref>
    modules = {}
    for match in re.finditer(r'clone_module\s+(?:"([^"]+)"|([^\s]+))\s+([^\s]+)\s+(?:"([^"]+)"|([^\s]+))', content):
        url = match.group(1) or match.group(2)
        dir_name = match.group(3)
        ref = match.group(4) or match.group(5)
        # substitute vars in url and ref
        for sub_k, sub_v in env_vars.items():
            url = url.replace(f"${{{sub_k}}}", sub_v).replace(f"${sub_k}", sub_v)
            ref = ref.replace(f"${{{sub_k}}}", sub_v).replace(f"${sub_k}", sub_v)
        modules[dir_name] = {"url": url, "ref": ref}

    # Extract apply_port_patch calls: apply_port_patch <dir> <patch>
    patch_targets = {}
    for match in re.finditer(r'apply_port_patch\s+([^\s]+)\s+([^\s]+)', content):
        target_dir = match.group(1)
        patch_name = match.group(2)
        patch_targets[patch_name] = target_dir

    env_vars["_MODULES"] = modules
    env_vars["_PATCH_TARGETS"] = patch_targets
    return env_vars


def run_command(cmd: list[str] | str, cwd: Path | None = None, env: dict | None = None) -> tuple[int, str, str]:
    """Run a shell or subprocess command and return (returncode, stdout, stderr)."""
    shell = isinstance(cmd, str)
    proc_env = os.environ.copy()
    if env:
        proc_env.update(env)

    res = subprocess.run(
        cmd,
        cwd=cwd,
        env=proc_env,
        shell=shell,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return res.returncode, res.stdout, res.stderr


def clone_repo(url: str, ref: str, dest_dir: Path) -> bool:
    """Clone a git repository at the given branch/tag."""
    if dest_dir.exists():
        return True
    clone_cmd = ["git", "clone", "--depth", "1"]
    if ref:
        clone_cmd.extend(["--branch", ref])
    clone_cmd.extend([url, str(dest_dir)])
    rc, out, err = run_command(clone_cmd)
    if rc != 0:
        # Fallback to full clone if shallow branch clone fails
        rc, out, err = run_command(["git", "clone", url, str(dest_dir)])
        if rc == 0 and ref:
            run_command(["git", "checkout", ref], cwd=dest_dir)
    return dest_dir.exists()


def test_patches_against_upstream(port_dir: Path, buildenv_vars: dict, work_dir: Path) -> dict:
    """Clone upstream source(s) and test applying each patch in patches/."""
    patches_dir = port_dir / "patches"
    modules = buildenv_vars.get("_MODULES", {})
    patch_targets = buildenv_vars.get("_PATCH_TARGETS", {})

    upstream_url = buildenv_vars.get("ZOPEN_STABLE_URL") or buildenv_vars.get("ZOPEN_DEV_URL")
    upstream_tag = buildenv_vars.get("ZOPEN_STABLE_TAG") or buildenv_vars.get("ZOPEN_DEV_TAG") or ""

    results = {
        "upstream_url": upstream_url,
        "upstream_tag": upstream_tag,
        "failed_patches": [],
        "passed_patches": [],
        "upstream_cloned": False,
    }

    patch_files = sorted(list(patches_dir.glob("*.patch")) + list(patches_dir.glob("*.gopatch"))) if patches_dir.exists() else []
    if not patch_files:
        return results

    cloned_dirs = {}
    if modules:
        for mod_name, mod_info in modules.items():
            mod_dir = work_dir / mod_name
            if clone_repo(mod_info["url"], mod_info["ref"], mod_dir):
                cloned_dirs[mod_name] = mod_dir
        results["upstream_cloned"] = len(cloned_dirs) > 0
    elif upstream_url:
        main_dir = work_dir / "upstream_src"
        if clone_repo(upstream_url, upstream_tag, main_dir):
            cloned_dirs["root"] = main_dir
            results["upstream_cloned"] = True

    # Test each patch
    for patch_path in patch_files:
        patch_name = patch_path.name
        target_name = patch_targets.get(patch_name, "root")
        target_dir = cloned_dirs.get(target_name) or cloned_dirs.get("root")

        if not target_dir or not target_dir.exists():
            continue

        rc, out, err = run_command(["git", "apply", "--check", str(patch_path.resolve())], cwd=target_dir)
        if rc == 0:
            results["passed_patches"].append(patch_name)
        else:
            # Patch failed! Extract rejected hunks
            src_copy = work_dir / f"src_{patch_name}"
            shutil.copytree(target_dir, src_copy, symlinks=True, ignore=shutil.ignore_patterns(".git"))
            rc_rej, out_rej, err_rej = run_command(["git", "apply", "--reject", str(patch_path.resolve())], cwd=src_copy)
            
            rej_files = list(src_copy.rglob("*.rej"))
            rejected_info = []
            for rf in rej_files:
                rel_path = rf.relative_to(src_copy)
                orig_file = str(rel_path)[:-4]  # strip .rej
                target_src_file = target_dir / orig_file
                src_content = ""
                if target_src_file.exists():
                    try:
                        src_content = target_src_file.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        pass
                
                rejected_info.append({
                    "target_file": orig_file,
                    "rejected_hunk": rf.read_text(encoding="utf-8", errors="replace"),
                    "current_upstream_content": src_content,
                })

            results["failed_patches"].append({
                "patch_name": patch_name,
                "target_module": target_name,
                "patch_path": str(patch_path.relative_to(port_dir)),
                "patch_content": patch_path.read_text(encoding="utf-8", errors="replace"),
                "error_message": err or out,
                "rejections": rejected_info,
            })

    return results


def run_zopen_build_local(port_dir: Path, meta_dir: Path, log_file: Path) -> tuple[int, str]:
    """Execute zopen-build -vv from meta/bin in port directory and capture log."""
    zopen_build_bin = meta_dir / "bin" / "zopen-build"
    if not zopen_build_bin.exists():
        return 1, f"zopen-build binary not found at {zopen_build_bin}"

    env = os.environ.copy()
    env["PATH"] = f"{meta_dir / 'bin'}:{env.get('PATH', '')}"
    env["ZOPEN_DONT_PROCESS_CONFIG"] = "1"

    print(f"Running zopen-build -vv on {port_dir}...")
    rc, out, err = run_command(f"{zopen_build_bin.resolve()} -vv > {log_file} 2>&1", cwd=port_dir, env=env)
    
    log_content = ""
    if log_file.exists():
        log_content = log_file.read_text(encoding="utf-8", errors="replace")
    return rc, log_content


def generate_copilot_facts_bundle(
    port_dir: Path,
    buildenv_vars: dict,
    patch_results: dict,
    build_log: str,
    output_file: Path,
) -> None:
    """Format failure details, rejected hunks, and upstream code into a prompt for Copilot."""
    lines = []
    port_name = buildenv_vars.get("ZOPEN_NAME") or port_dir.name
    version = buildenv_vars.get("ZOPEN_STABLE_TAG") or buildenv_vars.get("SYFT_VERSION") or "latest"

    lines.append(f"# Port Build Failure Analysis & Fix Request: `{port_name}` ({version})")
    lines.append("")
    lines.append("## Overview")
    lines.append(f"- **Port Name**: `{port_name}`")
    lines.append(f"- **Target Version/Tag**: `{version}`")
    lines.append(f"- **Upstream Repository**: `{buildenv_vars.get('ZOPEN_STABLE_URL', 'N/A')}`")
    lines.append("")

    # Section 1: Patch Application Failures
    failed_patches = patch_results.get("failed_patches", [])
    if failed_patches:
        lines.append("## ❌ Failed Patches Detected")
        lines.append(f"Found {len(failed_patches)} patch(es) that failed to apply cleanly to upstream `{version}`:")
        lines.append("")
        for fp in failed_patches:
            lines.append(f"### Patch: `{fp['patch_name']}` (Target Module: `{fp.get('target_module', 'root')}`)")
            lines.append(f"**Error Details:**\n```\n{fp['error_message']}\n```")
            lines.append("")
            lines.append("#### Original Patch Content:")
            lines.append(f"```diff\n{fp['patch_content']}\n```")
            lines.append("")

            for rej in fp.get("rejections", []):
                lines.append(f"#### Rejected Hunk for `{rej['target_file']}`:")
                lines.append(f"```diff\n{rej['rejected_hunk']}\n```")
                lines.append("")
                if rej.get("current_upstream_content"):
                    lines.append(f"#### Upstream Source File (`{rej['target_file']}` at tag `{version}`):")
                    src = rej["current_upstream_content"]
                    src_lines = src.splitlines()
                    if len(src_lines) > 200:
                        lines.append(f"```text\n{chr(10).join(src_lines[:200])}\n... [truncated {len(src_lines)-200} lines]\n```")
                    else:
                        lines.append(f"```text\n{src}\n```")
                    lines.append("")
    else:
        lines.append("## Patches Status")
        lines.append(f"All {len(patch_results.get('passed_patches', []))} existing patches applied cleanly.")
        lines.append("")

    # Section 2: Build Log Tail
    if build_log:
        log_lines = build_log.splitlines()
        tail_lines = log_lines[-100:] if len(log_lines) > 100 else log_lines
        lines.append("## 📋 Build Log Output (Last 100 lines)")
        lines.append("```text")
        lines.extend(tail_lines)
        lines.append("```")
        lines.append("")

    # Section 3: Port's buildenv
    buildenv_content = (port_dir / "buildenv").read_text(encoding="utf-8", errors="replace")
    lines.append("## ⚙️ Port `buildenv` File")
    lines.append(f"```bash\n{buildenv_content}\n```")
    lines.append("")

    # Section 4: Instructions for Copilot
    lines.append("## 🎯 Copilot Instructions")
    lines.append("1. **Root Cause Analysis**: Explain precisely what changed upstream and why the patch/build broke.")
    lines.append("2. **Exact Patch Fix**: Provide the updated patch content in a ```diff block with exact unified diff headers (`--- a/...`, `+++ b/...`).")
    lines.append("3. If `buildenv` needs modification (e.g. version tag bump or new submodule), specify the exact updated `buildenv` lines.")
    lines.append("")

    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text("\n".join(lines), encoding="utf-8")
    print(f"Context bundle written to {output_file}")


def validate_patch(target_dir: Path, patch_file: Path) -> bool:
    """Validate whether a patch applies cleanly to target_dir."""
    rc, out, err = run_command(["git", "apply", "--check", str(patch_file.resolve())], cwd=target_dir)
    if rc == 0:
        print(f"✅ Patch {patch_file.name} applies cleanly!")
        return True
    else:
        print(f"❌ Patch {patch_file.name} failed validation:\n{err or out}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="zopencommunity Port Build Triage & Copilot Auto-Fix")
    parser.add_argument("--port-dir", default=".", help="Path to port directory (e.g. ./syftport)")
    parser.add_argument("--meta-dir", default=".", help="Path to meta repo directory")
    parser.add_argument("--output", default="/tmp/copilot_autofix_facts.md", help="Output path for Copilot context markdown")
    parser.add_argument("--skip-build", action="store_true", help="Skip running zopen-build and only test patch applicability")
    parser.add_argument("--validate-patch", help="Path to patch file to test against port's upstream source")

    args = parser.parse_args()
    port_dir = Path(args.port_dir).resolve()
    meta_dir = Path(args.meta_dir).resolve()
    output_file = Path(args.output).resolve()

    if not (port_dir / "buildenv").exists():
        print(f"Error: {port_dir} does not contain a buildenv file", file=sys.stderr)
        return 1

    buildenv_vars = parse_buildenv(port_dir)
    print(f"Parsed port: {buildenv_vars.get('ZOPEN_NAME', port_dir.name)}")

    with tempfile.TemporaryDirectory(prefix="zopen_autofix_") as tmp_dir_str:
        work_dir = Path(tmp_dir_str)
        
        # Test patches against upstream
        print("Testing patches against upstream source...")
        patch_results = test_patches_against_upstream(port_dir, buildenv_vars, work_dir)

        # Run zopen-build if requested
        build_log = ""
        if not args.skip_build:
            log_path = work_dir / "zopen_build.log"
            rc, build_log = run_zopen_build_local(port_dir, meta_dir, log_path)

        # If validating a newly generated patch
        if args.validate_patch:
            patch_to_test = Path(args.validate_patch).resolve()
            upstream_dir = work_dir / "upstream_src"
            if upstream_dir.exists():
                return 0 if validate_patch(upstream_dir, patch_to_test) else 1

        # Generate Copilot prompt bundle
        generate_copilot_facts_bundle(port_dir, buildenv_vars, patch_results, build_log, output_file)

    return 0


if __name__ == "__main__":
    sys.exit(main())
