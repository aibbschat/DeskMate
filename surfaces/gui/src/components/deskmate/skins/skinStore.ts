/**
 * skinStore — 全局「当前激活皮肤」状态。
 *
 * 范式参考 src/components/deskmate/emotes/emotePack.ts：
 *   模块级单例 + localStorage 记忆 + 发布订阅 + useActiveSkin() hook。
 * 切肤副作用：
 *   ① 写 localStorage（key = "openwork-skin"）
 *   ② 设 document.documentElement.dataset.skinPre = id
 *      —— styles.css 用 :root[data-skin-pre="<id>"] 注入品牌色 + 装饰变量，
 *        这样 :root 里默认的 cobalt accent 就被本皮肤覆盖；与明暗（data-theme）正交。
 *   ③ 联动 emote pack（如皮肤指定了 emotePack，调 setActivePack）
 *   ④ 广播 CustomEvent "openwork:skin-changed"（保留供 hero bg / 装饰角标等 v2 元素用）
 *
 * 视觉偏好（颜色/形象）只放 localStorage，不入 prefs.json：跟 openwork-theme
 * （亮/暗/自动）一致——这是设备级偏好，跨设备没必要同步。
 *
 * 产品红线：小桔的桌宠/头像形象**永远不被皮肤替换**——切肤只改整站配色/装饰。
 * 若以后要加 hero bg 之类"含小桔立绘的大背景图"，应该是 WorkBuddy 范式（皮肤图作为
 * 视觉元素嵌入到某处背景），而不是 swap 桌宠。
 */

import { useEffect, useState } from "react";
import { setActivePack, getActivePack } from "../emotes/emotePack";
import {
  DEFAULT_SKIN_ID,
  getSkinMeta,
  resolveSkinId,
} from "./skinRegistry";

const KEY = "openwork-skin";
const SKIN_EVENT = "openwork:skin-changed";

function readInitial(): string {
  try {
    const v = localStorage.getItem(KEY);
    if (v && getSkinMeta(v)) return v;
  } catch {
    /* localStorage 不可用（隐私模式等）静默回落默认 */
  }
  return DEFAULT_SKIN_ID;
}

let activeSkin: string = readInitial();
const listeners = new Set<(id: string) => void>();

/** 读取当前激活皮肤 id。 */
export function getActiveSkin(): string {
  return activeSkin;
}

/** 读取当前激活皮肤的元数据。default 皮肤始终存在，故非空。 */
export function getActiveSkinMeta() {
  return getSkinMeta(resolveSkinId(activeSkin)) ?? getSkinMeta(DEFAULT_SKIN_ID)!;
}

/** 应用皮肤到 DOM：设 data-skin-pre 让 styles.css 命中对应选择器。 */
function applyToDom(skinId: string): void {
  if (typeof document === "undefined") return;
  document.documentElement.dataset.skinPre = skinId;
}

/** 切换皮肤。非法 id 静默忽略。已激活则 no-op（不广播、不写盘）。 */
export function setActiveSkin(skinId: string): void {
  const resolved = resolveSkinId(skinId);
  if (resolved === activeSkin) return;
  activeSkin = resolved;
  try {
    localStorage.setItem(KEY, resolved);
  } catch {
    /* 持久化失败不阻塞切换（本次会话内仍生效） */
  }
  applyToDom(resolved);
  // 联动 emote pack：皮肤指定了且与当前不同才切（避免循环）
  const skin = getSkinMeta(resolved);
  if (skin?.emotePack && skin.emotePack !== getActivePack()) {
    setActivePack(skin.emotePack);
  }
  listeners.forEach((cb) => cb(resolved));
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent(SKIN_EVENT, { detail: resolved }));
  }
}

// 模块加载即应用一次（不广播：boot 路径，组件挂载时各自读 useActiveSkin() 即可）
applyToDom(activeSkin);

/** 显式初始化（main.tsx 启动时调用；幂等）。 */
export function initSkin(): void {
  applyToDom(activeSkin);
}

/** 订阅皮肤变化。返回取消订阅函数。 */
export function subscribeSkin(cb: (id: string) => void): () => void {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}

/** 在组件内订阅当前激活皮肤（切换时自动重渲染）。 */
export function useActiveSkin(): string {
  const [skin, setSkin] = useState(activeSkin);
  useEffect(() => subscribeSkin((id) => setSkin(id)), []);
  return skin;
}

/** 在组件内订阅皮肤变化事件（保留以备后续 v2 装饰元素命令式响应使用）。 */
export function useSkinChanged(handler: (id: string) => void): void {
  useEffect(() => {
    const onEvent = (e: Event) => handler((e as CustomEvent<string>).detail);
    window.addEventListener(SKIN_EVENT, onEvent as EventListener);
    return () => window.removeEventListener(SKIN_EVENT, onEvent as EventListener);
  }, [handler]);
}

/** 仅供测试：在每个 case 间重置 store 状态。production 勿调。 */
export function _resetForTests(): void {
  activeSkin = DEFAULT_SKIN_ID;
  listeners.clear();
  try {
    localStorage.removeItem(KEY);
  } catch {
    /* ignore */
  }
  applyToDom(DEFAULT_SKIN_ID);
}
