/**
 * emotePack — 全局「当前激活表情包」状态（v2：单套，仅保留兼容壳）。
 *
 * v2 起只有一套「xiaoju-clay」黏土贴纸，不再有换包机制。
 * 此文件保留 getActivePack/useActivePack 接口供老调用方使用，永远返回 xiaoju-clay；
 * localStorage 里可能存着旧 pack 名（moling-default 等），这里直接忽略。
 */

import { EMOTE_MANIFEST } from "./emoteManifest";

const ACTIVE: string = EMOTE_MANIFEST.defaultPack || "xiaoju-clay";

// 清理旧 localStorage key（v2 不再需要）
try {
  localStorage.removeItem("emote-active-pack");
} catch {
  /* ignore */
}

export function getActivePack(): string {
  return ACTIVE;
}

export function setActivePack(_packId: string): void {
  // v2 单套，no-op
}

export function reportActivePackToBackend(): void {
  // v2 单套，后端无需区分；保留空实现兼容调用点
}

export function subscribePack(_cb: () => void): () => void {
  return () => {};
}

export function useActivePack(): string {
  return ACTIVE;
}
