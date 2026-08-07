/**
 * emotePack — 全局「当前激活表情包」状态。
 *
 * Pack（表情专辑）机制：manifest 里可定义多套表情包（moling-default 黄脸 /
 * moling-mono 线描 …），同一组表情 id 可切换不同画风。用户在某处切换 Pack 后，
 * 聊天气泡里渲染的表情应立即跟随变画风——所以激活状态必须全局共享。
 *
 * 这里用一个极轻的发布订阅单例 + React hook，避免把 pack 透传进每条消息渲染链：
 * 气泡里的 <Emote> 只调 useActivePack() 即可订阅，切换时自动重渲染。
 * 状态持久化到 localStorage，下次打开记住选择。
 */

import { useEffect, useState } from "react";
import { EMOTE_MANIFEST } from "./emoteManifest";
import { authedFetch, httpBase } from "../../../api";

const KEY = "emote-active-pack";
const DEFAULT_PACK = EMOTE_MANIFEST.defaultPack || "moling-default";

function readInitial(): string {
  try {
    const v = localStorage.getItem(KEY);
    if (v && EMOTE_MANIFEST.packs[v]) return v;
  } catch {
    /* localStorage 不可用时静默回落默认包 */
  }
  return DEFAULT_PACK;
}

let activePack: string = readInitial();
const listeners = new Set<() => void>();

export function getActivePack(): string {
  return activePack;
}

export function setActivePack(packId: string): void {
  if (!EMOTE_MANIFEST.packs[packId] || packId === activePack) return;
  activePack = packId;
  try {
    localStorage.setItem(KEY, packId);
  } catch {
    /* 持久化失败不阻塞切换 */
  }
  listeners.forEach((cb) => cb());
  // 同步给后端：让桌搭（assistant）回复的表情冻结到该包画风。fire-and-forget。
  reportActivePackToBackend();
}

/**
 * 把当前激活包上报后端（POST /v1/deskmate/emote-pack）。fire-and-forget，网络/后端
 * 未就绪时静默吞掉——下一次切换或应用启动时仍会重报，不影响正确性。
 */
export function reportActivePackToBackend(): void {
  const pack = activePack;
  authedFetch(`${httpBase()}/v1/deskmate/emote-pack`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pack }),
  }).catch(() => {});
}

// 应用启动即上报一次（localStorage 记住了上次选择），确保后端在首次聊天前已持有激活包。
reportActivePackToBackend();

export function subscribePack(cb: () => void): () => void {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}

/** 在组件内订阅当前激活 Pack（切换时自动重渲染）。 */
export function useActivePack(): string {
  const [pack, setPack] = useState(activePack);
  useEffect(() => subscribePack(() => setPack(activePack)), []);
  return pack;
}
