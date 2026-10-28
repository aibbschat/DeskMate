/**
 * SkinPicker — 皮肤选择器 UI（设置页"小桔"分类下挂载）。
 *
 * 设计：
 *   卡片网格（响应式 1→2 列），每张 = 色板预览 + 名字 + 描述 + tag + "使用中"角标。
 *   点选 → setActiveSkin(id) → 立即改 :root 品牌色 + 联动 emote pack。
 *
 * 数据驱动：新增皮肤 = 追加 skinRegistry 一条 + styles.css 加 [data-skin-pre] 选择器，
 * 本组件无需改动。
 */

import { SKIN_REGISTRY } from "./skinRegistry";
import { setActiveSkin, useActiveSkin } from "./skinStore";

const CARD = "rounded-xl2 border border-line bg-panel";
const FIELD_LABEL = "text-[12.5px] font-medium text-ink";
const FIELD_HELP = "text-meta text-muted mt-1.5 leading-relaxed";

interface SkinCardProps {
  skin: (typeof SKIN_REGISTRY)[number];
  active: boolean;
  onPick: () => void;
}

function SkinCard({ skin, active, onPick }: SkinCardProps) {
  // 色板优先展示"表面色"（决定第一观感的那层）：纸底 → 面板 → 浅品牌 → 品牌色。
  // 没有 surface 的皮肤（如 default）退回手写 preview，保持出厂观感。
  const swatch: string[] = skin.surface
    ? [
        skin.surface.paper ?? skin.preview[0],
        skin.surface.panel ?? skin.preview[1],
        skin.brand.accentSoft,
        skin.brand.accent,
      ]
    : [...skin.preview];
  return (
    <button
      type="button"
      onClick={onPick}
      aria-pressed={active}
      data-testid={`skin-card-${skin.id}`}
      data-skin-id={skin.id}
      className={
        "group relative text-left rounded-xl border-2 p-3 transition-all cursor-pointer " +
        "bg-panel hover:border-accent/60 hover:shadow-sm " +
        (active
          ? "border-accent ring-2 ring-accent-soft shadow-sm"
          : "border-line")
      }
    >
      {/* 色板：表面色 → 品牌色 */}
      <div className="flex h-10 rounded overflow-hidden border border-line/60 mb-2.5">
        {swatch.map((c, i) => (
          <div
            key={i}
            className="flex-1 transition-transform group-hover:scale-y-105"
            style={{ background: c }}
            aria-hidden
          />
        ))}
      </div>
      <div className="flex items-baseline gap-2 mb-0.5">
        <span className="text-body font-semibold text-ink">{skin.name}</span>
        <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-accent-soft text-accent font-medium">
          {skin.tag}
        </span>
      </div>
      <div className="text-[11.5px] text-muted leading-snug line-clamp-2 min-h-[28px]">
        {skin.desc}
      </div>
      {active && (
        <span
          className="absolute top-2 right-2 text-[10px] px-1.5 py-0.5 rounded-full
                     bg-accent text-on-accent font-medium"
          style={{ color: "white" }}
        >
          使用中
        </span>
      )}
    </button>
  );
}

export function SkinPicker() {
  const active = useActiveSkin();
  return (
    <div className={CARD + " p-4 mb-4"} data-testid="skin-picker">
      <div className={FIELD_LABEL}>小桔皮肤</div>
      <div className={FIELD_HELP}>
        给小桔换件衣裳——切换品牌色与装饰色，联动整站高亮。
        {SKIN_REGISTRY.find((s) => s.emotePack)?.emotePack && " 部分皮肤会自动切换表情包。"}
        小桔的桌宠与头像始终保持出厂立绘，换肤不动形象。
      </div>
      <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-3">
        {SKIN_REGISTRY.map((skin) => (
          <SkinCard
            key={skin.id}
            skin={skin}
            active={active === skin.id}
            onPick={() => setActiveSkin(skin.id)}
          />
        ))}
      </div>
    </div>
  );
}
