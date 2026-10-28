/**
 * InterviewCard — 深度访谈（③b）前端交互：小桔主动「想更懂你」。
 *
 * 两种形态：
 * 1. 邀请气泡（右上角，暖米白）：proactive tick 检测到认知盲区积累后推送，
 *    点「开始」进入问答流，点「稍后」关闭（回答链路不强制打断）。
 * 2. 问答流（居中弹窗）：逐题展示苏格拉底问题，三模式带标签
 *    （澄清 / 探索 / 深化），提交后回灌认知画像，全部答完给感谢收尾。
 *
 * 收敛设计（修复「翻来覆去同一题」）：
 * - 任何关闭路径都必须让后端知道，否则补送机制会反复重放同一轮邀请：
 *   · 邀请气泡 60 秒自动收起 → 上报 dismiss（暂停补送 24h）；
 *   · 问卷流「退出」→ 上报 decline（关联 gap 标记暂不追问 + 结束整轮）；
 *   · 问卷流「跳过此题」→ 上报 skip（该题关联 gap 标记暂不追问，整轮答/跳完即结束）。
 * - 只有「开始回答 / 提交回答」才推进访谈；其余都是「不答」语义，必须收敛。
 *
 * 数据源：DeskMateOverlay 监听 `deskmate:interview` 事件传入
 * { session_id, questions, total }（由 tick 的 interview 负载或手动 start 产生）。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  answerCognitiveInterview,
  dismissCognitiveInterview,
  skipCognitiveInterviewQuestion,
  declineCognitiveInterview,
  type InterviewQuestion,
} from "../../api";

export interface InterviewPayload {
  session_id: string;
  questions: InterviewQuestion[];
  total: number;
  note?: string;
  /** 访谈层（cognitive / emotional），后端 redeliver 按层隔离；前端 dismiss/decline 回传用。 */
  layer?: string;
}

export interface InterviewCardProps {
  /** 访谈负载；null 时组件不渲染。 */
  interview: InterviewPayload | null;
  /** 关闭（忽略 / 稍后 / 完成）。 */
  onClose: () => void;
}

/** 三模式 → 中文标签 + 语义提示。 */
const MODE_META: Record<string, { label: string; hint: string }> = {
  clarify: { label: "澄清", hint: "你之前有过矛盾的说法，想跟你对一下" },
  explore: { label: "探索", hint: "这个领域我还没完全懂你" },
  deepen: { label: "深化", hint: "想听你再多说一点" },
};

function modeMeta(mode?: string) {
  return MODE_META[mode || ""] || { label: "提问", hint: "" };
}

/** 邀请气泡文案（首问预览）。 */
function inviteText(questions: InterviewQuestion[]): string {
  const first = questions[0];
  if (first) return `我最近发现有些地方还不够懂你，想跟你聊聊「${first.topic}」。`;
  return "我想更懂你，聊几句好吗？";
}

export function InterviewCard({ interview, onClose }: InterviewCardProps) {
  const [phase, setPhase] = useState<"invite" | "qa" | "done" | "exit">("invite");
  const [idx, setIdx] = useState(0);
  const [answer, setAnswer] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [answered, setAnswered] = useState(0);
  const [total, setTotal] = useState(0);
  const timerRef = useRef<ReturnType<typeof setTimeout>>();

  const questions = interview?.questions ?? [];
  const sessionId = interview?.session_id ?? "";
  const q = questions[idx];
  const layer = interview?.layer ?? "cognitive";

  // 新负载到达 → 重置到邀请阶段；邀请气泡 60s 自动收起（上报 dismiss 暂停补送，不打扰，稍后可再触发）
  useEffect(() => {
    if (interview) {
      setPhase("invite");
      setIdx(0);
      setAnswer("");
      setError("");
      setAnswered(0);
      setTotal(interview.total || interview.questions.length || 0);
      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => {
        // 60s 未操作视作「这次不答」→ 上报 dismiss，让后端暂停补送，避免无限重放同一题
        dismissCognitiveInterview(layer).catch(() => {});
        setPhase((p) => (p === "invite" ? "exit" : p));
        setTimeout(() => onClose(), 300);
      }, 60_000);
    }
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [interview]);

  // 邀请 → 问答流（取消自动收起计时）
  const handleStart = useCallback(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    setPhase("qa");
  }, []);

  const handleCloseNow = useCallback(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    setPhase("exit");
    setTimeout(onClose, 200);
  }, [onClose]);

  // 跳过单题：该题关联 gap 标记「暂不追问」，整轮答/跳完即结束（不再补送）
  const handleSkip = useCallback(async () => {
    if (!sessionId || !q) return;
    try {
      const r = await skipCognitiveInterviewQuestion(sessionId, q.id, layer);
      if (r.session_done) {
        handleCloseNow();
      } else {
        setIdx((i) => i + 1);
        setAnswer("");
      }
    } catch (e) {
      console.error("[Interview] skip failed", e);
      // 跳过失败也允许用户继续：直接跳到下一题
      setIdx((i) => i + 1);
      setAnswer("");
    }
  }, [sessionId, q, layer, handleCloseNow]);

  // 拒答整轮：关联 gap 标记「暂不追问」并结束会话（不再反复问同一批问题）
  const handleDecline = useCallback(() => {
    if (sessionId) declineCognitiveInterview(sessionId, layer).catch(() => {});
    handleCloseNow();
  }, [sessionId, layer, handleCloseNow]);

  // 显式忽略（点 ✕ 或「稍后再说」）：fire-and-forget 回传后端，让补送暂停 24h，再关窗。
  // 与「用户不在窗口前、邀请无声消失」的补送是两回事——既然他看见了却 defer，就不再骚扰。
  const handleDismissNow = useCallback(() => {
    dismissCognitiveInterview(layer).catch(() => {});
    handleCloseNow();
  }, [layer, handleCloseNow]);

  const handleSubmit = useCallback(async () => {
    const text = answer.trim();
    if (!text || !sessionId || !q) return;
    setSubmitting(true);
    setError("");
    try {
      const r = await answerCognitiveInterview(sessionId, q.id, text);
      if (r.session_done) {
        setAnswered((n) => n + 1);
        setPhase("done");
      } else {
        setAnswered((n) => n + 1);
        setIdx((i) => i + 1);
        setAnswer("");
      }
    } catch (e) {
      setError("提交失败，请再试一次");
      console.error("[Interview] answer failed", e);
    } finally {
      setSubmitting(false);
    }
  }, [answer, sessionId, q]);

  if (!interview || phase === "exit") return null;

  /* ---------------- 邀请气泡 ---------------- */
  if (phase === "invite") {
    return (
      <div
        className="fixed top-3 right-3"
        style={{
          width: 330,
          zIndex: 60,
          opacity: 1,
          transform: "translateY(0)",
          transition: "opacity .25s ease, transform .25s ease",
        }}
        role="dialog"
        aria-live="polite"
      >
        <div
          className="rounded-xl border overflow-hidden"
          style={{
            background: "var(--panel)",
            borderColor: "var(--line)",
            boxShadow: "0 12px 36px rgba(0,0,0,0.14)",
          }}
        >
          <div className="flex items-start gap-2.5 px-3.5 pt-3 pb-0">
            <div
              className="flex-shrink-0 w-8 h-8 rounded-full flex items-center justify-center text-heading"
              style={{ background: "var(--accent-soft)", color: "var(--accent)" }}
            >
              🌸
            </div>
            <div className="flex-1 min-w-0">
              <div className="text-[12.5px] font-semibold" style={{ color: "var(--ink)" }}>
                小桔想更懂你
              </div>
              <div className="text-label" style={{ color: "var(--faint)" }}>
                深度访谈 · {total} 个问题
              </div>
            </div>
            <button
              className="flex-shrink-0 w-6 h-6 rounded flex items-center justify-center"
              style={{ color: "var(--faint)" }}
              onClick={handleDismissNow}
              aria-label="关闭"
            >
              ✕
            </button>
          </div>
          <div
            className="px-3.5 pt-1.5 pb-1 text-ui leading-relaxed"
            style={{ color: "var(--muted)" }}
          >
            {inviteText(questions)}
          </div>
          <div className="flex items-center gap-2 px-3.5 py-2.5">
            <button
              className="text-[12.5px] font-semibold px-3 py-1.5 rounded-lg border transition-colors"
              style={{
                background: "var(--accent)",
                borderColor: "var(--accent)",
                color: "#fff",
              }}
              onClick={handleStart}
            >
              开始回答
            </button>
            <button
              className="text-[12.5px] px-3 py-1.5 rounded-lg border transition-colors"
              style={{
                background: "transparent",
                borderColor: "var(--line)",
                color: "var(--muted)",
              }}
              onClick={handleDismissNow}
            >
              稍后再说
            </button>
          </div>
        </div>
      </div>
    );
  }

  /* ---------------- 问答流（居中弹窗） ---------------- */
  return (
    <div
      className="fixed inset-0 flex items-center justify-center"
      style={{ zIndex: 70, background: "rgba(0,0,0,0.32)" }}
      onClick={(e) => {
        // 点遮罩不关闭（避免误丢答案），仅「退出」可退
        e.stopPropagation();
      }}
      role="dialog"
      aria-modal="true"
    >
      <div
        className="rounded-2xl border overflow-hidden"
        style={{
          width: "min(480px, calc(100vw - 32px))",
          background: "var(--panel)",
          borderColor: "var(--line)",
          boxShadow: "0 18px 48px rgba(0,0,0,0.22)",
        }}
      >
        {/* 头部 */}
        <div className="flex items-center gap-2.5 px-4 pt-3.5 pb-2">
          <div
            className="flex-shrink-0 w-8 h-8 rounded-full flex items-center justify-center text-heading"
            style={{ background: "var(--accent-soft)", color: "var(--accent)" }}
          >
            🌸
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-ui font-semibold" style={{ color: "var(--ink)" }}>
              {phase === "done" ? "谢谢你的坦诚" : "小桔想更懂你"}
            </div>
            <div className="text-label" style={{ color: "var(--faint)" }}>
              {phase === "done"
                ? "这些会被记进我对你的理解里"
                : `第 ${idx + 1} / ${total} 题`}
            </div>
          </div>
          {phase !== "done" && (
            <button
              className="flex-shrink-0 w-7 h-7 rounded flex items-center justify-center text-meta"
              style={{ color: "var(--faint)" }}
              onClick={handleDecline}
              aria-label="退出访谈"
            >
              退出
            </button>
          )}
        </div>

        {phase === "done" ? (
          /* ---------- 完成页 ---------- */
          <div className="px-4 pb-5">
            <div
              className="rounded-xl px-3.5 py-3 text-ui leading-relaxed"
              style={{ background: "var(--accent-soft)", color: "var(--ink)" }}
            >
              谢谢你认真回答。这些表达会沉淀进认知画像，让我以后更贴近你的想法。
              下次发现还有不懂的地方，我还会再来问你。
            </div>
            <div className="flex justify-end mt-3">
              <button
                className="text-[12.5px] font-semibold px-4 py-1.5 rounded-lg border transition-colors"
                style={{
                  background: "var(--accent)",
                  borderColor: "var(--accent)",
                  color: "#fff",
                }}
                onClick={handleCloseNow}
              >
                好的
              </button>
            </div>
          </div>
        ) : q ? (
          /* ---------- 单题问答 ---------- */
          <div className="px-4 pb-4">
            <div className="flex items-center gap-2 mb-2">
              <span
                className="text-[10.5px] font-semibold px-2 py-0.5 rounded-full"
                style={{ background: "var(--accent-soft)", color: "var(--accent)" }}
              >
                {modeMeta(q.mode).label}
              </span>
              <span className="text-label" style={{ color: "var(--faint)" }}>
                {modeMeta(q.mode).hint}
              </span>
            </div>
            <div
              className="text-body font-medium leading-relaxed mb-2"
              style={{ color: "var(--ink)" }}
            >
              {q.question}
            </div>
            {q.context ? (
              <div
                className="text-label leading-relaxed mb-2 px-2 py-1.5 rounded-lg"
                style={{ background: "var(--paper)", color: "var(--faint)" }}
              >
                背景：{q.context}
              </div>
            ) : null}
            <textarea
              value={answer}
              onChange={(e) => setAnswer(e.target.value)}
              placeholder="说点什么，越具体越好……"
              rows={4}
              style={{
                width: "100%",
                boxSizing: "border-box",
                border: "1px solid var(--line)",
                borderRadius: 10,
                padding: "10px 12px",
                fontSize: 13,
                lineHeight: 1.6,
                color: "var(--ink)",
                background: "var(--paper)",
                outline: "none",
                resize: "vertical",
              }}
            />
            {error && (
              <div className="text-[11.5px] mt-1" style={{ color: "#EA6668" }}>
                {error}
              </div>
            )}
            <div className="flex items-center justify-between mt-2.5">
              <span className="text-label" style={{ color: "var(--faint)" }}>
                {answered} / {total} 已答
              </span>
              <div className="flex items-center gap-2">
                <button
                  className="text-[12.5px] px-3 py-1.5 rounded-lg border transition-colors"
                  style={{
                    background: "transparent",
                    borderColor: "var(--line)",
                    color: "var(--muted)",
                  }}
                  onClick={handleSkip}
                >
                  跳过此题
                </button>
                <button
                  className="text-[12.5px] font-semibold px-4 py-1.5 rounded-lg border transition-colors disabled:opacity-50"
                  style={{
                    background: "var(--accent)",
                    borderColor: "var(--accent)",
                    color: "#fff",
                  }}
                  disabled={!answer.trim() || submitting}
                  onClick={handleSubmit}
                >
                  {submitting ? "提交中…" : idx + 1 >= total ? "完成" : "下一题"}
                </button>
              </div>
            </div>
          </div>
        ) : (
          <div className="px-4 pb-5 text-ui" style={{ color: "var(--muted)" }}>
            暂时没有可问的问题。
          </div>
        )}
      </div>
    </div>
  );
}
