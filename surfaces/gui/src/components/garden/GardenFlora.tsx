/**
 * GardenFlora — 花园图鉴（P1）。
 *
 * 在花开横幅下方可展开的「花园图鉴」：展示每一种花（后端 FLOWER_INFO 权威定义）
 * + 已解锁数量。花语 desc 为零 LLM 静态文案，解锁数为真实数据。
 *
 * 克制：默认收起，点击「花园图鉴」展开；不打扰主流程。
 */

import { useState } from "react";

export interface GardenFloraItem {
  kind: string;
  label: string;
  emoji: string;
  desc: string;
  unlocked: number;
}

export function GardenFlora({ flora }: { flora?: GardenFloraItem[] | null }) {
  const [open, setOpen] = useState(false);
  if (!flora || flora.length === 0) return null;

  const totalKinds = flora.length;
  const unlockedKinds = flora.filter((f) => f.unlocked > 0).length;

  return (
    <div className="garden-flora">
      <button
        type="button"
        className="garden-flora-toggle"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span className="garden-flora-toggle-title">
          🌿 花园图鉴
          <span className="garden-flora-toggle-meta">
            {unlockedKinds}/{totalKinds} 种见过
          </span>
        </span>
        <span className="garden-flora-toggle-arrow">{open ? "▾" : "▸"}</span>
      </button>

      {open && (
        <div className="garden-flora-grid">
          {flora.map((f) => (
            <div
              key={f.kind}
              className={`garden-flora-card${f.unlocked > 0 ? " is-unlocked" : ""}`}
            >
              <span className="garden-flora-emoji">{f.emoji}</span>
              <div className="garden-flora-name">
                {f.label}
                {f.unlocked > 0 && (
                  <span className="garden-flora-count">{f.unlocked}</span>
                )}
              </div>
              <div className="garden-flora-desc">{f.desc}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
