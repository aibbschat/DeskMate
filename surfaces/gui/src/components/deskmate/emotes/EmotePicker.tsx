/**
 * EmotePicker — 表情选择面板。
 *
 * 顶部一排 Pack（表情专辑）切换 tab，下方网格列出当前语义层的全部墨灵表情，
 * 点击某项把 ![中文名](emote:NAME) 交给 onPick。切换 Pack 后网格里的预览会随之变画风。
 */

import { Emote } from "./Emote";
import { emoteList, EMOTE_MANIFEST } from "./emoteManifest";
import { useActivePack, setActivePack } from "./emotePack";

interface Props {
  /** 选中某个表情名（不带包裹）时触发 */
  onPick: (name: string) => void;
}

export function EmotePicker({ onPick }: Props) {
  const activePack = useActivePack();
  const packs = Object.values(EMOTE_MANIFEST.packs);

  return (
    <div
      className="emote-picker absolute z-40 bottom-full mb-1 left-0 rounded-xl border border-line bg-panel shadow-2xl p-2.5"
      role="menu"
      data-testid="emote-picker"
    >
      {packs.length > 1 && (
        <div className="emote-pack-tabs" role="tablist" aria-label="表情包">
          {packs.map((p) => (
            <button
              key={p.id}
              type="button"
              role="tab"
              aria-selected={p.id === activePack}
              className={"emote-pack-tab" + (p.id === activePack ? " active" : "")}
              onClick={() => setActivePack(p.id)}
            >
              {p.name}
            </button>
          ))}
        </div>
      )}
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
            <Emote name={e.id} size={30} live />
            <span className="emote-cell-label">{e.name_zh}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
