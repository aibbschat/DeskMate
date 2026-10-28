// 卡片画廊（仅内部使用）：把每个卡片在**各种状态下**渲染出来。
//
// 为什么值得有：我们的前端有 100+ 自定义卡片，改一个共用样式/字号 token 之后，靠开 app
// 手点根本覆盖不到"已失效的审批卡"或"审查者不可用的升级提示"这类状态 —— 那些恰恰是最
// 容易坏、也最少被看见的地方。这里把状态摊平成一页，改完扫一眼就知道有没有塌。
//
// 边界：
//   · 只渲染 fixtures.ts 里的**编造**数据，不读后端、不写任何东西、不接管聊天。
//   · 没有导航入口 —— 地址栏加 `#gallery` 才进得来（打包后的 app 同样可用）。
//   · 不参与生产逻辑：App.tsx 里只在 surface === "gallery" 时挂载。
import { useState } from "react";
import type { ApprovalDecision, Item } from "../types";
import { ApprovalCard } from "../components/ApprovalCard";
import { InboxItemCard } from "../components/InboxItemCard";
import { Transcript } from "../components/Transcript";
import { PanelHead } from "../components/IntegrationsView";
import { approvalStates, inboxStates, otherStates, transcriptSample } from "./fixtures";

const noop = () => {};

function Section({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  return (
    <section className="mt-7" data-testid={`gallery-section-${title}`}>
      <h3 className="text-heading font-semibold mb-0.5">{title}</h3>
      {hint && <p className="text-meta text-muted mb-2.5">{hint}</p>}
      <div className="space-y-3">{children}</div>
    </section>
  );
}

function Case({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-label text-faint uppercase tracking-wide mb-1">{label}</div>
      <div className="transcript">{children}</div>
    </div>
  );
}

export function Gallery() {
  // 窄栏 = 卡片被挤到最小可用宽度时的样子（侧栏展开 + 右栏打开）。这是最容易塌的宽度。
  const [narrow, setNarrow] = useState(false);

  return (
    <main className="flex-1 min-w-0 flex bg-paper">
      <div className="flex-1 min-w-0 overflow-y-auto hairline-scroll">
        <div className={"mx-auto px-7 py-6 " + (narrow ? "max-w-xl" : "max-w-3xl")}>
          <PanelHead
            title="画廊 · 卡片状态"
            sub="全部是编造的假数据。改完共用样式/字号之后在这里扫一眼，比在 app 里手点快得多。"
          />
          <div className="flex items-center gap-2">
            <button
              className={
                "text-meta px-2.5 py-1 rounded-full border " +
                (!narrow ? "border-accent text-accent bg-accentSoft" : "border-line text-muted hover:border-lineStrong")
              }
              onClick={() => setNarrow(false)}
              data-testid="gallery-width-wide"
            >
              宽栏
            </button>
            <button
              className={
                "text-meta px-2.5 py-1 rounded-full border " +
                (narrow ? "border-accent text-accent bg-accentSoft" : "border-line text-muted hover:border-lineStrong")
              }
              onClick={() => setNarrow(true)}
              data-testid="gallery-width-narrow"
            >
              窄栏（最挤的情况）
            </button>
            <span className="text-label text-faint ml-auto">#{location.hash.slice(1) || "gallery"}</span>
          </div>

          <Section title="审批卡" hint="每一种权限状态，含三种升级原因与「已失效」。">
            {approvalStates.map(({ label, item }) => (
              <Case key={label} label={label}>
                <ApprovalCard
                  item={item as Extract<Item, { kind: "approval" }>}
                  onApprove={noop as (d: ApprovalDecision) => void}
                />
              </Case>
            ))}
          </Section>

          <Section title="信箱卡" hint="无人值守时停在这里的那几张（含升级原因）。">
            {inboxStates.map(({ label, item }) => (
              <Case key={label} label={label}>
                <InboxItemCard item={item} onResolve={noop} />
              </Case>
            ))}
          </Section>

          <Section title="整个回合" hint="用户 → 工具步骤 → 门卡 → 回答，一处看完整节奏。">
            <div className="transcript">
              <Transcript items={transcriptSample} onApprove={noop} running={false} />
            </div>
          </Section>

          <Section title="其它卡片" hint="提问 / 目录 / 计划 / 工具安装 / 组队 / 工作项 / 灵感 / 意图讨论。">
            {otherStates.map(({ label, item }) => (
              <Case key={label} label={label}>
                <Transcript items={[item]} onApprove={noop} running={false} />
              </Case>
            ))}
          </Section>

          <p className="text-label text-faint mt-8">
            本页不在生产流程里：没有导航入口，地址栏加 #gallery 才到。要加新状态，只改 fixtures.ts。
          </p>
        </div>
      </div>
    </main>
  );
}
