// 思考与执行显示风格（上游 OpenWorker 官方无此功能，本 fork 新增）：
//   timeline 时间线（默认，运行中即见状态节点、随时可点开看每步详情）
//   cards    折叠卡片（沿用官方 stepgroup，默认收起，点开看详情）
//   bubble   极简气泡（一句话状态，点开看全部步骤）
//
// 纯显示偏好，按设备存 localStorage，与 textSize.ts 同构，零后端改动。
// 与"皮肤"正交：皮肤换小桔形象（红线保护），本偏好只换思考/执行的呈现结构。
import { useEffect, useState } from "react";

export type TurnStyle = "timeline" | "cards" | "bubble";

const KEY = "openwork-turn-style";
const PREF_EVENT = "openwork:turn-style";

export function getTurnStyle(): TurnStyle {
  try {
    const v = localStorage.getItem(KEY);
    return v === "cards" || v === "bubble" ? v : "timeline";
  } catch {
    // 隐私模式等读不到 localStorage —— 退回默认，不影响本次会话
    return "timeline";
  }
}

export function setTurnStyle(style: TurnStyle) {
  try {
    if (style === "timeline") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, style);
  } catch {
    /* 存不下也要让本次会话生效 */
  }
  window.dispatchEvent(new CustomEvent(PREF_EVENT));
}

/** 设置页控件的 hook —— 别处改了这个偏好也能同步。 */
export function useTurnStyle(): [TurnStyle, (s: TurnStyle) => void] {
  const [style, setStyle] = useState<TurnStyle>(getTurnStyle);
  useEffect(() => {
    const sync = () => setStyle(getTurnStyle());
    window.addEventListener(PREF_EVENT, sync);
    return () => window.removeEventListener(PREF_EVENT, sync);
  }, []);
  return [style, setTurnStyle];
}
