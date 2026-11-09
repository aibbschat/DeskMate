#!/usr/bin/env python3
"""Tauri 前后端依赖门禁（本地打包 preflight 调用）。

背景（2026-11-09）：仓库只有 package-lock.json（npm），但有人用 pnpm 重装依赖，
`^2.7.2` / `^2.11.2` 被解析成 2.8.4 / 2.12.4，与 Rust 侧 tauri-plugin-dialog
2.7.1 的 minor 不再对齐 ⇒ tauri CLI 直接 `Found version mismatched Tauri packages`
退出。坑在于 vite build 已经跑完才炸，错误信息完全指不到根因。

契约：
  - package.json 与 package-lock.json 对两个 tauri 依赖必须写**精确版本**（无 ^/~）；
  - node_modules 实际安装版本必须与声明完全一致（唯一事实源 = package-lock.json，
    重装一律 `npm ci`，禁止 pnpm/yarn 混装）；
  - 前端 plugin-dialog 的 minor 必须与 Cargo.lock 里 tauri-plugin-dialog 的 minor 一致；
  - tauri CLI 的 minor 必须与 Cargo.lock 里 tauri crate 的 minor 一致。

纯 stdlib，云端不 import、不参与部署清单（本地构建工具）。
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
GUI = ROOT / "surfaces/gui"

PINNED = {
    "@tauri-apps/plugin-dialog": "2.7.2",
    "@tauri-apps/cli": "2.11.2",
}
INSTALLED_AT = {
    "@tauri-apps/plugin-dialog": "node_modules/@tauri-apps/plugin-dialog/package.json",
    "@tauri-apps/cli": "node_modules/@tauri-apps/cli/package.json",
}


def read_json(rel: str) -> dict | None:
    p = GUI / rel
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"✗ 读不到 {p}: {exc}", file=sys.stderr)
        return None


def minor(version: str) -> str:
    return ".".join(version.split(".")[:2])


def main() -> int:
    pkg = read_json("package.json") or {}
    lock = read_json("package-lock.json") or {}
    cargo_path = GUI / "src-tauri/Cargo.lock"
    try:
        cargo = cargo_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"✗ 读不到 {cargo_path}: {exc}", file=sys.stderr)
        return 2

    declared = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    lock_root = lock.get("packages", {}).get("", {})
    lock_declared = {
        **lock_root.get("dependencies", {}),
        **lock_root.get("devDependencies", {}),
    }
    installed = {}
    for name, rel in INSTALLED_AT.items():
        data = read_json(rel)
        installed[name] = data.get("version") if data else None

    errors: list[str] = []
    for name, pinned in PINNED.items():
        if declared.get(name) != pinned:
            errors.append(
                f"{name}: package.json 必须精确锁 {pinned}（当前 {declared.get(name)!r}）"
            )
        if lock_declared.get(name) != pinned:
            errors.append(
                f"{name}: package-lock.json 根声明应为 {pinned}（当前 {lock_declared.get(name)!r}）"
            )
        if installed.get(name) != pinned:
            errors.append(
                f"{name}: node_modules 实际为 {installed.get(name)!r}，期望 {pinned}"
                " —— 别用 pnpm/yarn 装，跑 `cd surfaces/gui && npm ci`"
            )

    def crate_version(name: str) -> str | None:
        m = re.search(
            r'\[\[package\]\]\nname = "' + re.escape(name) + r'"\nversion = "([^"]+)"',
            cargo,
        )
        return m.group(1) if m else None

    rust_dialog = crate_version("tauri-plugin-dialog")
    rust_tauri = crate_version("tauri")
    if rust_dialog is None or rust_tauri is None:
        errors.append("Cargo.lock 里找不到 tauri-plugin-dialog / tauri 的版本条目")
    else:
        if minor(rust_dialog) != minor(installed["@tauri-apps/plugin-dialog"] or ""):
            errors.append(
                f"minor 不一致：Rust tauri-plugin-dialog {rust_dialog} vs "
                f"前端 plugin-dialog {installed['@tauri-apps/plugin-dialog']}"
                "（tauri CLI 会拒绝构建）"
            )
        if minor(rust_tauri) != minor(installed["@tauri-apps/cli"] or ""):
            errors.append(
                f"minor 不一致：Rust tauri {rust_tauri} vs "
                f"tauri CLI {installed['@tauri-apps/cli']}"
            )

    if errors:
        print("✗ Tauri 依赖门禁未通过，打包前先修：", file=sys.stderr)
        for err in errors:
            print(f"   - {err}", file=sys.stderr)
        print(
            "   修复：cd surfaces/gui && npm ci（本仓库以 package-lock.json 为唯一依赖事实源）",
            file=sys.stderr,
        )
        return 1

    print(
        "    ✓ Tauri 依赖门禁：plugin-dialog "
        f"{installed['@tauri-apps/plugin-dialog']} / CLI "
        f"{installed['@tauri-apps/cli']} 与 package-lock.json、Cargo.lock 对齐"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
