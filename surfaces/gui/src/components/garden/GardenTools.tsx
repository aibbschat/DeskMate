/**
 * GardenTools — 工具房（蓝图第二层 · 工具房·设置/任务，保持克制）。
 *
 * 打理花园的日常工具都在这：同步、定位、口令保险箱、分身开关、洞察。
 * 克制铺陈，不喧宾夺主 —— 它是花园的工具房，不是清单页。
 */

import { AlterEgoCard } from "../deskmate/AlterEgoCard";
import { InsightPanel } from "../deskmate/InsightPanel";
import { PositioningCard } from "../deskmate/PositioningCard";
import { SyncCard } from "../deskmate/SyncCard";
import { VaultPanel } from "../deskmate/VaultPanel";

export function GardenTools({ onOpenAlterEgo }: { onOpenAlterEgo: () => void }) {
  return (
    <div className="garden-tools">
      <div className="garden-space-head">
        <div className="garden-space-title">🔧 工具房</div>
        <div className="garden-space-sub">打理花园的日常工具都在这里，克制的，不吵。</div>
      </div>

      <div className="garden-tools-grid">
        <div className="garden-tools-col">
          <PositioningCard />
          <SyncCard />
          <VaultPanel />
        </div>
        <div className="garden-tools-col">
          <AlterEgoCard onOpenPage={onOpenAlterEgo} />
          <InsightPanel />
        </div>
      </div>
    </div>
  );
}
