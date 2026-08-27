// 终审提案卡（知情批准 · 线路 B → 线路 A 桥）。
// 把「送审片段 / 推荐专家 / 验收标准 / 前序意见」四要素完整呈现给用户，
// 用户在推荐基础上增减专家后「批准终审」——决定权在用户，不虚。
import { Icon } from "./Icon";

export interface FinalProposal {
  item_kind: string;
  topic: string;
  manuscript_text: string;
  criteria: string;
  prior_review: string;
  all_experts: { id: string; name: string; role?: string }[];
  recommended: string[];
}

function Block({ label, body }: { label: string; body: string }) {
  return (
    <div>
      <div className="text-[12px] font-medium text-muted mb-1">{label}</div>
      <div className="text-[13px] text-ink whitespace-pre-wrap max-h-44 overflow-auto rounded-lg bg-black/[0.03] dark:bg-white/[0.04] p-2.5">
        {body || "（空）"}
      </div>
    </div>
  );
}

export function ProposalCard({
  proposal,
  selected,
  onToggle,
  onApprove,
  onCancel,
}: {
  proposal: FinalProposal;
  selected: string[];
  onToggle: (id: string) => void;
  onApprove: (selected: string[]) => void;
  onCancel: () => void;
}) {
  return (
    <div className="rounded-xl border border-black/10 dark:border-white/10 bg-paper p-4 space-y-3">
      <div className="flex items-center gap-2 text-[13px] font-semibold text-ink">
        <Icon name="sparkle" size={16} className="text-accent" />
        <span>终审提案 · {proposal.topic}</span>
      </div>

      <Block label="① 送审片段" body={proposal.manuscript_text} />
      <Block label="③ 验收标准" body={proposal.criteria} />
      <Block label="④ 前序单点审核意见" body={proposal.prior_review} />

      <div>
        <div className="text-[12px] font-medium text-muted mb-1">
          ② 终审专家（可在推荐基础上增减）
        </div>
        <div className="flex flex-wrap gap-2">
          {proposal.all_experts.map((e) => {
            const on = selected.includes(e.id);
            return (
              <button
                key={e.id}
                onClick={() => onToggle(e.id)}
                className={
                  "px-2.5 py-1 rounded-full text-[12px] border transition " +
                  (on
                    ? "border-accent bg-accent-soft text-accent font-medium"
                    : "border-black/10 dark:border-white/15 text-muted hover:text-ink")
                }
              >
                {e.name}
                {e.role ? ` · ${e.role}` : ""}
              </button>
            );
          })}
        </div>
      </div>

      <div className="flex gap-2 justify-end pt-1">
        <button className="btn" onClick={onCancel}>
          取消
        </button>
        <button
          className="btn primary"
          disabled={selected.length === 0}
          onClick={() => onApprove(selected)}
        >
          批准终审（{selected.length} 位专家）
        </button>
      </div>
    </div>
  );
}
