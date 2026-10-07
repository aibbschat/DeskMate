/**
 * EmotePicker — 表情选择面板（v2：单套黏土贴纸）。
 *
 * 网格列出全部小桔表情，点击某项把 ![中文名](emote:NAME) 交给 onPick。
 * v2 只有一套「xiaoju-clay」，不再有 pack 切换 tab。
 */

import { Emote } from "./Emote";
import { emoteList } from "./emoteManifest";

interface Props {
  /** 选中某个表情名（不带包裹）时触发 */
  onPick: (name: string) => void;
}

export function EmotePicker({ onPick }: Props) {
  return (
    <div
      className="emote-picker absolute z-40 bottom-full mb-1 left-0 rounded-xl border border-line bg-panel shadow-2xl p-3"
      role="menu"
      data-testid="emote-picker"
    >
      <div className="emote-grid">
        {emoteList().map((e) => (
          <button
            key={e.id}
            type="button"
            className="emote-cell"
            title={e.name_zh}
            aria-label={e.name_zh}
            onClick={() => onPick(e.id)}
          >
            <Emote name={e.id} size={52} />
            <span className="emote-cell-label">{e.name_zh}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
