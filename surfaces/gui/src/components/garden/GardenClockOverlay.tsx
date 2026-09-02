/**
 * GardenClockOverlay — 花园时钟的「轻问候」浮层。
 *
 * 由 visit 上报后触发（后端 maybe_visit），事件都是确定发生的：
 * - morning 晨间问候 / dusk 黄昏点灯 / night 深夜陪伴 / comeback 久别归来 / festival 节日
 * 每次进入花园最多一个事件，文案由模板 + 真实变量（离开天数/花开数）组成，
 * 绝不编造未发生的互动。节日（festival）按节日名给专属氛围文案（P1）。
 * 可一键关闭。
 */

import { Icon } from "../Icon";

export interface GardenEvent {
  kind: "morning" | "dusk" | "night" | "comeback" | "festival";
  date?: string;
  gap_days?: number;
  flower_delta?: number;
  festival_name?: string;
  message?: string;
}

interface Props {
  event: GardenEvent;
  flowerCount: number;
  onClose: () => void;
}

/** 节日专属氛围（P1）：文案 + emoji，命中即确定显示，未命中回退通用祝福。 */
const FESTIVAL_FLAVOR: Record<string, { icon: string; text: string }> = {
  春节: { icon: "🧧", text: "春节快乐。花园里挂起了红灯笼，替你热闹了一回。" },
  中秋: { icon: "🌕", text: "中秋快乐。今晚的月光，也照进了你的花园。" },
  元旦: { icon: "🎆", text: "元旦快乐。新的一年，花园等你种下新的花。" },
  国庆节: { icon: "🎉", text: "国庆快乐。花园里也替你高兴了一回。" },
  情人节: { icon: "💞", text: "情人节快乐。花园里有一朵花，是专门为你开的。" },
  平安夜: { icon: "🎄", text: "平安夜快乐。花园的灯，今晚替你多亮一会儿。" },
  圣诞节: { icon: "🎄", text: "圣诞快乐。花园里也落了点节日的光。" },
  跨年夜: { icon: "🥂", text: "跨年快乐。这一年的花都替你收好了，明年继续。" },
};

function renderMessage(ev: GardenEvent, flowerCount: number): { icon: string; text: string } {
  switch (ev.kind) {
    case "morning":
      return { icon: "🌅", text: "早晨好。花园的晨露还没干，欢迎回来。" };
    case "dusk":
      return { icon: "🌇", text: "黄昏了，给花园点上一盏暖灯。" };
    case "night":
      return { icon: "🌙", text: "夜深了，花园里的灯还亮着。想聊的话，我都在。" };
    case "comeback": {
      const days = ev.gap_days ?? 0;
      const flowers = ev.flower_delta ?? flowerCount;
      const d = days > 0 ? `${days} 天` : "一段时间";
      const f = flowers > 0 ? `，花园新开了 ${flowers} 朵花` : "";
      return { icon: "🏡", text: `好久不见，你离开了 ${d}${f}。` };
    }
    case "festival": {
      const name = ev.festival_name ?? "";
      const flavor = FESTIVAL_FLAVOR[name] ?? {
        icon: "🎉",
        text: `${name}快乐，花园里也替你高兴。`,
      };
      return flavor;
    }
    default:
      return { icon: "🌸", text: ev.message ?? "" };
  }
}

export function GardenClockOverlay({ event, flowerCount, onClose }: Props) {
  const { icon, text } = renderMessage(event, flowerCount);
  return (
    <div className="garden-clock-toast" role="status">
      <span className="garden-clock-toast-icon">{icon}</span>
      <span className="garden-clock-toast-text">{text}</span>
      <button type="button" className="garden-clock-toast-close" onClick={onClose} aria-label="关闭问候">
        <Icon name="x" size={14} />
      </button>
    </div>
  );
}
