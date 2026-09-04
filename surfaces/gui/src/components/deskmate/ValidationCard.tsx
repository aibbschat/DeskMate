import { useCallback, useEffect, useRef, useState } from "react";
import { submitValidationEvaluation, type ValidationScenario } from "../../api";

export interface ValidationPayload {
  scenario: ValidationScenario;
  note?: string;
}

export interface ValidationCardProps {
  /** 验证负载；null 时组件不渲染。 */
  validation: ValidationPayload | null;
  /** 关闭（忽略 / 稍后 / 完成）。 */
  onClose: () => void;
}

/** 四维度中文标签。 */
const DIM_LABELS: Record<string, string> = {
  stance: "立场一致",
  argument: "论证模式",
  value: "价值观排序",
  expression: "表达习惯",
};

export function ValidationCard({ validation, onClose }: ValidationCardProps) {
  const [phase, setPhase] = useState<"invite" | "qa" | "done" | "exit">("invite");
  const [userAnswer, setUserAnswer] = useState("");
  const [aiAnswer, setAiAnswer] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<{
    overall_score: number;
    dimension_scores: Record<string, number>;
    feedback: string;
    divergence_points: string[];
    engine?: string;
  } | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout>>();

  const scenario = validation?.scenario;

  // 新负载到达 → 重置到邀请阶段；邀请气泡 60s 自动收起
  useEffect(() => {
    if (validation) {
      setPhase("invite");
      setUserAnswer("");
      setAiAnswer("");
      setError("");
      setResult(null);
      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => {
        setPhase((p) => (p === "invite" ? "exit" : p));
        setTimeout(() => onClose(), 300);
      }, 60_000);
    }
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [validation]);

  // 邀请 → 问答流（取消自动收起计时）
  const handleStart = useCallback(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    // 进入问答前先让 AI 以用户身份代答，生成一段"小桔替你想的"预览
    setPhase("qa");
    const draft = aiDraftAnswer(scenario?.scenario ?? "", scenario?.domain ?? "");
    setAiAnswer(draft);
  }, [scenario]);

  const handleCloseNow = useCallback(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    setPhase("exit");
    setTimeout(() => onClose(), 200);
  }, [onClose]);

  // 提交：AI 代答 + 用户回答一起评估
  const handleSubmit = useCallback(async () => {
    if (!aiAnswer.trim() || !userAnswer.trim()) {
      setError("两边都填一下，我才能判断像不像你。");
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      const r = await submitValidationEvaluation(aiAnswer.trim(), userAnswer.trim());
      if (r.evaluation) {
        setResult({
          overall_score: r.evaluation.overall_score,
          dimension_scores: r.evaluation.dimension_scores || {},
          feedback: r.evaluation.feedback || "",
          divergence_points: r.evaluation.divergence_points || [],
          engine: r.evaluation._engine,
        });
        setPhase("done");
      } else {
        setError(r.note || "评估失败，请稍后再试。");
      }
    } catch (e) {
      setError("提交失败，请检查连接后重试。");
    } finally {
      setSubmitting(false);
    }
  }, [aiAnswer, userAnswer]);

  // 重新生成 AI 代答
  const handleRegen = useCallback(() => {
    setAiAnswer(aiDraftAnswer(scenario?.scenario ?? "", scenario?.domain ?? ""));
  }, [scenario]);

  if (!validation || phase === "exit") return null;

  // 邀请气泡（带自动收起）
  if (phase === "invite") {
    return (
      <div
        className="fixed bottom-5 right-5"
        style={{ zIndex: 68 }}
        role="dialog"
        aria-modal="false"
      >
        <div
          className="rounded-2xl border w-[300px] overflow-hidden"
          style={{
            background: "var(--panel)",
            borderColor: "var(--line)",
            boxShadow: "0 14px 40px rgba(0,0,0,0.18)",
          }}
        >
          <div className="flex items-center gap-2.5 px-3.5 pt-3">
            <div
              className="flex-shrink-0 w-8 h-8 rounded-full flex items-center justify-center text-[15px]"
              style={{ background: "var(--accent-soft)", color: "var(--accent)" }}
            >
              🌸
            </div>
            <div className="flex-1 min-w-0">
              <div className="text-[12.5px] font-semibold" style={{ color: "var(--ink)" }}>
                小桔想考考自己
              </div>
              <div className="text-[11px]" style={{ color: "var(--faint)" }}>
                认知验证 · 一个情境
              </div>
            </div>
            <button
              className="flex-shrink-0 w-6 h-6 rounded flex items-center justify-center"
              style={{ color: "var(--faint)" }}
              onClick={handleCloseNow}
              aria-label="关闭"
            >
              ✕
            </button>
          </div>
          <div
            className="px-3.5 pt-1.5 pb-1 text-[13px] leading-relaxed"
            style={{ color: "var(--muted)" }}
          >
            我想验证一下自己有多懂你：给你一个没遇到过的新情境，我先替你想个答案，你再告诉我你真实的做法。
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
              开始验证
            </button>
            <button
              className="text-[12.5px] px-3 py-1.5 rounded-lg border transition-colors"
              style={{
                background: "transparent",
                borderColor: "var(--line)",
                color: "var(--muted)",
              }}
              onClick={handleCloseNow}
            >
              稍后再说
            </button>
          </div>
        </div>
      </div>
    );
  }

  /* ---------------- 问答/结果流（居中弹窗） ---------------- */
  return (
    <div
      className="fixed inset-0 flex items-center justify-center"
      style={{ zIndex: 70, background: "rgba(0,0,0,0.32)" }}
      onClick={(e) => e.stopPropagation()}
      role="dialog"
      aria-modal="true"
    >
      <div
        className="rounded-2xl border overflow-hidden"
        style={{
          width: "min(520px, calc(100vw - 32px))",
          background: "var(--panel)",
          borderColor: "var(--line)",
          boxShadow: "0 18px 48px rgba(0,0,0,0.22)",
        }}
      >
        {/* 头部 */}
        <div className="flex items-center gap-2.5 px-4 pt-3.5 pb-2">
          <div
            className="flex-shrink-0 w-8 h-8 rounded-full flex items-center justify-center text-[15px]"
            style={{ background: "var(--accent-soft)", color: "var(--accent)" }}
          >
            🌸
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-[13px] font-semibold" style={{ color: "var(--ink)" }}>
              {phase === "done" ? "验证结果" : "小桔想考考自己"}
            </div>
            <div className="text-[11px]" style={{ color: "var(--faint)" }}>
              {phase === "done" ? "看看它有没有真的懂你" : "认知验证 · 一个情境"}
            </div>
          </div>
          {phase !== "done" && (
            <button
              className="flex-shrink-0 w-7 h-7 rounded flex items-center justify-center text-[12px]"
              style={{ color: "var(--faint)" }}
              onClick={handleCloseNow}
              aria-label="退出验证"
            >
              退出
            </button>
          )}
        </div>

        {phase === "done" && result ? (
          /* ---------- 结果页 ---------- */
          <div className="px-4 pb-5">
            <div
              className="rounded-xl px-3.5 py-3 text-[13px] leading-relaxed"
              style={{ background: "var(--accent-soft)", color: "var(--ink)" }}
            >
              {result.feedback}
            </div>
            {result.engine && (
              <div className="mt-1.5 text-center">
                <span
                  className="inline-block rounded-full px-2.5 py-0.5 text-[11px]"
                  style={{
                    background:
                      result.engine === "llm"
                        ? "rgba(255, 167, 38, 0.14)"
                        : "rgba(108, 117, 125, 0.12)",
                    color:
                      result.engine === "llm"
                        ? "var(--accent)"
                        : "var(--muted)",
                  }}
                >
                  {result.engine === "llm"
                    ? "AI 语义评估"
                    : "本地规则评估"}
                </span>
              </div>
            )}

            {/* 像我度环 */}
            <div className="flex items-center justify-center my-4">
              <div
                className="flex items-center justify-center rounded-full"
                style={{
                  width: 88,
                  height: 88,
                  background: `conic-gradient(var(--accent) ${Math.round(
                    result.overall_score * 100,
                  )}%, var(--line) 0)`,
                }}
              >
                <div
                  className="rounded-full flex flex-col items-center justify-center"
                  style={{
                    width: 70,
                    height: 70,
                    background: "var(--panel)",
                  }}
                >
                  <div className="text-[19px] font-bold" style={{ color: "var(--ink)" }}>
                    {Math.round(result.overall_score * 100)}%
                  </div>
                  <div className="text-[10px]" style={{ color: "var(--faint)" }}>
                    像我度
                  </div>
                </div>
              </div>
            </div>

            {/* 四维条 */}
            <div className="space-y-2">
              {Object.entries(DIM_LABELS).map(([key, label]) => {
                const v = result.dimension_scores[key] ?? 0;
                return (
                  <div key={key} className="flex items-center gap-2">
                    <span
                      className="w-[70px] text-[11px] text-right flex-shrink-0"
                      style={{ color: "var(--muted)" }}
                    >
                      {label}
                    </span>
                    <div
                      className="flex-1 h-2 rounded-full overflow-hidden"
                      style={{ background: "var(--line)" }}
                    >
                      <div
                        className="h-full rounded-full transition-all"
                        style={{
                          width: `${Math.round(v * 100)}%`,
                          background: v >= 0.5 ? "var(--accent)" : "var(--danger, #EA6668)",
                        }}
                      />
                    </div>
                    <span
                      className="w-[34px] text-[11px] font-semibold"
                      style={{ color: v >= 0.5 ? "var(--accent)" : "var(--danger, #EA6668)" }}
                    >
                      {Math.round(v * 100)}%
                    </span>
                  </div>
                );
              })}
            </div>

            {result.divergence_points.length > 0 && (
              <div
                className="mt-3 rounded-xl px-3.5 py-2.5 text-[12px] leading-relaxed"
                style={{ background: "var(--paper)", color: "var(--muted)" }}
              >
                {result.divergence_points.map((p, i) => (
                  <div key={i} className="mb-1 last:mb-0">
                    • {p}
                  </div>
                ))}
              </div>
            )}

            <div className="flex justify-end mt-3 gap-2">
              <button
                className="text-[12.5px] px-3 py-1.5 rounded-lg border transition-colors"
                style={{
                  background: "transparent",
                  borderColor: "var(--line)",
                  color: "var(--muted)",
                }}
                onClick={handleCloseNow}
              >
                好的
              </button>
            </div>
          </div>
        ) : (
          /* ---------- 问答：情景 + AI 代答 + 用户回答 ---------- */
          <div className="px-4 pb-4">
            {/* 情景 */}
            <div
              className="rounded-xl px-3.5 py-3 mb-3 text-[13.5px] leading-relaxed"
              style={{ background: "var(--paper)", color: "var(--ink)" }}
            >
              <div className="text-[10.5px] font-semibold mb-1" style={{ color: "var(--faint)" }}>
                一个你没遇到过的新情境
              </div>
              {scenario?.scenario}
            </div>

            {/* AI 代答 */}
            <div className="mb-2">
              <div className="flex items-center justify-between mb-1">
                <span className="text-[11px] font-semibold" style={{ color: "var(--muted)" }}>
                  小桔替你答的
                </span>
                <button
                  className="text-[11px] px-2 py-0.5 rounded border"
                  style={{ borderColor: "var(--line)", color: "var(--faint)" }}
                  onClick={handleRegen}
                >
                  换一个
                </button>
              </div>
              <div
                className="rounded-xl px-3 py-2.5 text-[13px] leading-relaxed"
                style={{ background: "var(--accent-soft)", color: "var(--ink)" }}
              >
                {aiAnswer || "小桔正在替你思考……"}
              </div>
            </div>

            {/* 用户回答 */}
            <div className="mb-1">
              <div className="text-[11px] font-semibold mb-1" style={{ color: "var(--muted)" }}>
                你真实的做法
              </div>
              <textarea
                value={userAnswer}
                onChange={(e) => setUserAnswer(e.target.value)}
                placeholder="如果是你，会怎么做、为什么？"
                rows={3}
                style={{
                  width: "100%",
                  boxSizing: "border-box",
                  border: "1px solid var(--line)",
                  borderRadius: 10,
                  padding: "10px 12px",
                  fontSize: 13,
                  lineHeight: 1.6,
                  color: "var(--ink)",
                  background: "var(--panel)",
                  resize: "vertical",
                }}
              />
            </div>

            {error && (
              <div className="text-[12px] mt-1" style={{ color: "var(--danger, #EA6668)" }}>
                {error}
              </div>
            )}

            <div className="flex justify-end mt-2.5">
              <button
                className="text-[12.5px] font-semibold px-4 py-1.5 rounded-lg border transition-colors"
                style={{
                  background: "var(--accent)",
                  borderColor: "var(--accent)",
                  color: "#fff",
                  opacity: submitting ? 0.6 : 1,
                }}
                onClick={handleSubmit}
                disabled={submitting}
              >
                {submitting ? "对比中……" : "提交验证"}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/** 规则化生成"小桔替你答的"草稿（纯本地，无 LLM 成本）。 */
function aiDraftAnswer(scenario: string, domain: string): string {
  const t = domain || "这件事";
  // 基于画像倾向的保守代答：尊重原则、不轻易妥协（画像高置信度条目多为原则性）。
  // 若情境提到"约稿/平台/机会"，代答里呼应一下，显得是"顺着情境想"。
  const isCommerial = /约稿|平台|稿费|机会|收益/.test(scenario);
  const head = isCommerial
    ? "如果是我面对这种约稿"
    : "如果换成我在这个情境里";
  return `${head}，我不会轻易让步。${t}是我一直放在心上的原则，遇到这样的情况，我宁可慢一点、宁可少拿，也要守住自己真正认同的那条线——就像我在作品里对待史实那样，底线一旦妥协，以后就守不住了。`;
}
