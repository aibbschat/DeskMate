// 审批卡顶部那行「为什么这张卡现在要你来看」。
//
// 上游同名组件。三档原因（types.ts: ApprovalEscalationInfo）：
//   reviewer_unsure      —— 审查者拿不准
//   human_required       —— 策略要求必须人工点头
//   reviewer_unavailable —— 审查者不可用，退回人工
// 语气是"平静说明"，不是警告：审查者犹豫是上下文，不是危险。
//
// 兼容旧字段：后端在只走 `reviewer_unsure` 这条路时（engine.py 的 unsure 分支）不会发
// escalation，所以两个字段都要认。
import { useTranslation } from "react-i18next";
import type { ApprovalEscalationInfo } from "../types";

export function ApprovalEscalation({
  escalation,
  reviewerUnsure,
}: {
  escalation?: ApprovalEscalationInfo | null;
  reviewerUnsure?: string;
}) {
  const { t } = useTranslation();
  const detail =
    escalation || (reviewerUnsure ? { kind: "reviewer_unsure" as const, reason: reviewerUnsure } : null);
  if (!detail) return null;
  return (
    <div className="text-meta text-muted mt-1" data-testid="approval-escalation">
      {t(`approval.escalation_${detail.kind}`)}
      {detail.reason ? ` ${detail.reason}` : ""}
    </div>
  );
}
