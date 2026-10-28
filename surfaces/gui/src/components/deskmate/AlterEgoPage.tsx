/**
 * AlterEgoPage — 「它眼中的我」全屏独立页（懂我中心）。
 *
 * 视觉与交互对齐 2026-09-04 通过的原型 understand-me-center.html：
 * - 左区：小桔形象放大为主体 + 光环浮动 + 三个环绕立体按键（认知/情感/侧写）
 *   → 点按键弹「维度数字」（立场31/论证5/价值观6/思维框架7…）→ 点数字弹具体条目详情
 *   → 左下/右下两个 3D 桔黄「去回答」按钮（待澄清 gap，接真访谈）
 * - 右区：透明状态流（无卡片）：情景问答🌼 / 画像回灌🌱 / 这里看到变化❤️
 * - 暖米黄渐变 + 桔橙主色，呼吸/浮动/心跳动画
 *
 * 数据：/v1/deskmate/alter-ego（portrait.sections 带 dimension 分组 + identity + gaps）
 * fail-open：API 失败静默降级，显示兜底状态。
 */

import { useEffect, useMemo, useState } from "react";
import { authedFetch, httpBase } from "../../api";
import { XiaojuMascot } from "./XiaojuMascot";

interface Props {
  open: boolean;
  onClose: () => void;
}

interface Article {
  title: string;
  content: string;
  evidence?: string;
  tags?: string[];
  dimension?: string;
}

interface Section {
  id: string;
  title: string;
  icon: string;
  status: "ready" | "learning" | "empty";
  headline: string;
  articles: Article[];
  updated_at?: string;
}

interface Gap {
  layer: string;
  topic: string;
  reason: string;
  gap_id: string;
}

interface Identity {
  likeness_score: number;
  likeness_label: string;
  understanding_progress: number;
  understanding_detail: string;
  core_ready: number;
  core_total: number;
  core_avg_confidence: number;
  gaps: Gap[];
  note: string;
}

interface Portrait {
  sections: Section[];
  summary: {
    ready_count: number;
    learning_count: number;
    empty_count: number;
    total_articles: number;
  };
  identity?: Identity;
}

interface AlterEgoData {
  enabled: boolean;
  has_data: boolean;
  total_signals: number;
  portrait: Portrait | null;
}

// 维度元数据（认知四维 + 情感三维）
const DIM_META: Record<string, { label: string; icon: string }> = {
  stance: { label: "立场", icon: "🎯" },
  argument: { label: "论证模式", icon: "🔗" },
  value: { label: "价值观", icon: "⚖️" },
  thinking: { label: "思维框架", icon: "🧩" },
  emotional_trigger: { label: "情绪触发", icon: "🌦" },
  recovery_pattern: { label: "回血方式", icon: "🌿" },
  care_preference: { label: "关怀偏好", icon: "💐" },
};

// 三个环绕按键（硬编码布局，计数来自 panelCounts）

interface Chip {
  label: string;
  icon: string;
  count: number;
  items: Article[];
}

function groupByDim(articles: Article[]): Chip[] {
  const map = new Map<string, Article[]>();
  articles.forEach((a) => {
    const d = a.dimension || "other";
    if (!map.has(d)) map.set(d, []);
    map.get(d)!.push(a);
  });
  return Array.from(map.entries()).map(([d, items]) => ({
    label: DIM_META[d]?.label || d,
    icon: DIM_META[d]?.icon || "📌",
    count: items.length,
    items,
  }));
}

// 暖色系动画 keyframes
const ANIM_CSS = `
  @keyframes uglowPulse { 0%,100%{opacity:.5;transform:scale(1);} 50%{opacity:1;transform:scale(1.08);} }
  @keyframes uhaloPulse { 0%,100%{opacity:.5;transform:scale(1);} 50%{opacity:1;transform:scale(1.1);} }
  @keyframes ujuFloat { 0%,100%{transform:translateY(0);} 50%{transform:translateY(-9px);} }
  @keyframes unumFloat { 0%,100%{transform:translateY(0);} 50%{transform:translateY(-3px);} }
  @keyframes ukeyFloat { 0%,100%{transform:translateY(0);} 50%{transform:translateY(-4px);} }
  @keyframes ubloom { 0%,100%{transform:scale(1) rotate(0deg);opacity:.7;} 50%{transform:scale(1.12) rotate(6deg);opacity:1;} }
  @keyframes useed { 0%,100%{transform:translateY(0) rotate(0deg);opacity:.7;} 50%{transform:translateY(-5px) rotate(-4deg);opacity:1;} }
  @keyframes uheart { 0%,100%{transform:scale(1);} 30%{transform:scale(1.06);} 40%{transform:scale(1);} }
`;

export function AlterEgoPage({ open, onClose }: Props) {
  const [data, setData] = useState<AlterEgoData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [enabling, setEnabling] = useState(false);

  // 两阶弹层 + 去回答提示
  const [dimPanel, setDimPanel] = useState<{ title: string; chips: Chip[] } | null>(null);
  const [detail, setDetail] = useState<{ title: string; items: Article[] } | null>(null);
  const [gapTip, setGapTip] = useState<Gap | null>(null);
  const [startingGap, setStartingGap] = useState(false);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setDimPanel(null);
    setDetail(null);
    authedFetch(`${httpBase()}/v1/deskmate/alter-ego`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((d) => {
        if (cancelled) return;
        setData(d as AlterEgoData);
      })
      .catch((e) => {
        if (!cancelled) setError(String(e?.message ?? e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  // 分身默认关 → 首次进入给一次性引导 + 一键开启
  const needsGuide = !loading && !error && data != null && !data.enabled;

  async function handleEnable() {
    if (enabling) return;
    setEnabling(true);
    try {
      const r = await authedFetch(`${httpBase()}/v1/deskmate/alter-ego`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: true }),
      });
      if (r.ok) {
        const d2 = await r.clone().json().catch(() => null);
        setData((prev) => (prev ? { ...prev, enabled: true } : prev));
        if (d2) setData((prev) => ({ ...(prev as AlterEgoData), ...d2 }));
      } else {
        setError("开启失败，请稍后再试");
      }
    } catch (e) {
      setError(String((e as Error)?.message ?? e));
    } finally {
      setEnabling(false);
    }
  }

  const portrait = data?.portrait;
  const sections = portrait?.sections ?? [];
  const identity = portrait?.identity;
  const likeness = identity?.likeness_score ?? 0;
  const likenessPct = Math.round(likeness * 100);
  const gaps = identity?.gaps ?? [];

  const cognitiveSec = sections.find((s) => s.id === "cognitive");
  const emotionalSec = sections.find((s) => s.id === "emotional");
  const behaviorSecs = sections.filter((s) => s.id !== "cognitive" && s.id !== "emotional");

  // 三按键计数
  const panelCounts: Record<string, number> = useMemo(() => {
    const cog = cognitiveSec?.articles.length ?? 0;
    const emo = emotionalSec?.articles.length ?? 0;
    const beh = behaviorSecs.reduce((n, s) => n + s.articles.length, 0);
    return { cognitive: cog, emotional: emo, behavior: beh };
  }, [cognitiveSec, emotionalSec, behaviorSecs]);

  // 条目总数（画像回灌展示用）
  const totalArticles = useMemo(
    () => sections.reduce((n, s) => n + s.articles.length, 0),
    [sections],
  );

  function openPanel(key: string) {
    if (key === "cognitive" && cognitiveSec) {
      setDimPanel({ title: "🧠 它理解的我 · 认知", chips: groupByDim(cognitiveSec.articles) });
    } else if (key === "emotional" && emotionalSec) {
      setDimPanel({ title: "💛 它在乎的 · 情感", chips: groupByDim(emotionalSec.articles) });
    } else if (key === "behavior") {
      const chips: Chip[] = behaviorSecs.map((s) => ({
        label: s.title,
        icon: s.icon,
        count: s.articles.length,
        items: s.articles,
      }));
      setDimPanel({ title: "✍️ 它在学什么 · 侧写", chips });
    }
  }

  // 去回答 → 启动对应 layer 访谈，交给 InterviewCard
  async function handleStartGap() {
    if (!gapTip || startingGap) return;
    setStartingGap(true);
    try {
      const layer = gapTip.layer || "emotional";
      const r = await authedFetch(`${httpBase()}/v1/deskmate/cognitive/interview/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ layer, limit: 3 }),
      });
      if (r.ok) {
        const d = (await r.json()) as { session_id: string | null; questions: unknown[] };
        if (d?.session_id && d?.questions?.length) {
          window.dispatchEvent(
            new CustomEvent("deskmate:interview", {
              detail: { session_id: d.session_id, questions: d.questions, layer },
            }),
          );
          setGapTip(null);
          onClose(); // 让访谈卡在 overlay 聚焦呈现
        } else {
          setGapTip(null);
          setDimPanel({
            title: "想更懂你",
            chips: [{ label: gapTip.topic, icon: "🌼", count: 0, items: [] }],
          });
        }
      } else {
        setGapTip(null);
      }
    } catch (e) {
      setGapTip(null);
    } finally {
      setStartingGap(false);
    }
  }

  if (!open) return null;

  const gapLabel = (g: Gap) => g.topic.replace(/^(情感|认知)[·:：]/, "");

  return (
    <div
      className="fixed inset-0 z-[60] overflow-y-auto"
      style={{
        background:
          "radial-gradient(120% 90% at 22% 12%, #fff8ef 0%, #ffe9d4 38%, #f6dcc6 72%, #efcdb4 100%)",
        color: "#5c3a1e",
        fontFamily:
          '"PingFang SC","Hiragino Sans GB","Microsoft YaHei",-apple-system,system-ui,sans-serif',
      }}
    >
      <style>{ANIM_CSS}</style>

      {/* 顶部暖光斑 */}
      <div
        style={{
          position: "absolute",
          top: -90,
          left: "8%",
          width: 360,
          height: 360,
          borderRadius: "50%",
          background: "radial-gradient(circle,rgba(255,184,77,0.42),transparent 68%)",
          animation: "uglowPulse 7s ease-in-out infinite",
          pointerEvents: "none",
        }}
      />
      <div
        style={{
          position: "absolute",
          top: "10%",
          right: -80,
          width: 300,
          height: 300,
          borderRadius: "50%",
          background: "radial-gradient(circle,rgba(245,169,127,0.30),transparent 66%)",
          animation: "uglowPulse 8s ease-in-out infinite",
          animationDelay: "2s",
          pointerEvents: "none",
        }}
      />

      {/* 顶部栏 */}
      <div
        className="sticky top-0 z-10 flex items-center justify-between px-6 py-4"
        style={{
          background: "rgba(255,248,239,0.85)",
          backdropFilter: "blur(12px)",
          borderBottom: "1px solid rgba(214,188,160,0.3)",
        }}
      >
        <div>
          <h1 style={{ margin: 0, fontSize: 24, fontWeight: 800, letterSpacing: 2, color: "#6b4a32" }}>
            它眼中的我
          </h1>
          <div style={{ marginTop: 4, fontSize: 12.5, color: "#9a7b5f", maxWidth: 480, lineHeight: 1.6 }}>
            这是小桔一点一点，从你身上学到的——你的想法、你的在乎、你的习惯，被它慢慢串起。
          </div>
        </div>
        <button
          onClick={onClose}
          className="px-4 py-2 rounded-lg text-ui font-semibold transition-colors"
          style={{
            background: "#c2662f",
            color: "#fff",
            border: "none",
            boxShadow: "0 4px 14px rgba(180,110,60,0.3)",
          }}
        >
          关闭
        </button>
      </div>

      {/* 分身未开启 → 引导 */}
      {needsGuide && (
        <div style={{ maxWidth: 1080, margin: "0 auto", padding: "18px 20px 0" }}>
          <div
            style={{
              background: "rgba(255,255,255,0.72)",
              borderRadius: 16,
              padding: "18px 20px",
              border: "1px solid rgba(194,102,47,0.35)",
              boxShadow: "0 4px 16px rgba(180,130,90,0.1)",
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 16,
              flexWrap: "wrap",
            }}
          >
            <div style={{ fontSize: 13.5, color: "#5b4636", lineHeight: 1.7, flex: "1 1 320px" }}>
              <div style={{ fontWeight: 700, fontSize: 15, color: "#6b4a32", marginBottom: 4 }}>
                🌟 开启「个人分身」，让它更懂你的选择
              </div>
              开启后，小桔会从你的对话中悄悄学习你的决策偏好，慢慢拼出「它眼中的你」。
            </div>
            <button
              onClick={handleEnable}
              disabled={enabling}
              className="px-4 py-2 rounded-lg text-ui font-semibold transition-colors"
              style={{
                background: enabling ? "#cbb39c" : "#c2662f",
                color: "#fff",
                border: "none",
                boxShadow: "0 4px 14px rgba(180,110,60,0.3)",
                opacity: enabling ? 0.7 : 1,
                cursor: enabling ? "default" : "pointer",
                whiteSpace: "nowrap",
              }}
            >
              {enabling ? "开启中…" : "一键开启"}
            </button>
          </div>
        </div>
      )}

      {/* 主体 */}
      {loading && (
        <div style={{ textAlign: "center", padding: "80px 0", color: "#a38568" }}>
          正在唤起它眼中的你…
        </div>
      )}
      {!loading && error && (
        <div style={{ textAlign: "center", padding: "60px 0", color: "#c0392b" }}>
          读取失败：{error}
        </div>
      )}

      {!loading && !error && (
        <div
          style={{
            position: "relative",
            zIndex: 3,
            maxWidth: 1080,
            margin: "0 auto",
            padding: "18px 20px 60px",
            display: "flex",
            flexDirection: "row",
            alignItems: "stretch",
            gap: 12,
            boxSizing: "border-box",
          }}
        >
          {/* ============ 左区：小桔为主体 + 环绕按键 ============ */}
          <div
            style={{
              flex: "1 1 auto",
              minWidth: 0,
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              position: "relative",
            }}
          >
            {/* 像我度（小桔头顶） */}
            <div style={{ textAlign: "center", marginBottom: 4 }}>
              <div style={{ fontSize: 12.5, color: "#B0762A", fontWeight: 600, letterSpacing: 1.5 }}>
                小桔有多懂我
              </div>
              <div
                style={{
                  fontSize: 46,
                  fontWeight: 800,
                  color: "#E8630A",
                  lineHeight: 1.15,
                  textShadow: "0 2px 0 rgba(255,255,255,0.7)",
                  animation: "unumFloat 4s ease-in-out infinite",
                }}
              >
                {likenessPct}%
                {identity?.likeness_label ? (
                  <span
                    style={{
                      fontSize: 12.5,
                      fontWeight: 700,
                      color: "#b45309",
                      background: "rgba(255,159,28,0.14)",
                      borderRadius: 10,
                      padding: "2px 8px",
                      marginLeft: 8,
                      verticalAlign: "middle",
                    }}
                  >
                    {identity.likeness_label}
                  </span>
                ) : null}
              </div>
              <div style={{ fontSize: 11.5, color: "#9A6B33", marginTop: 1 }}>
                已了解 <b style={{ color: "#E8630A" }}>{identity?.understanding_detail ?? "—"}</b>
                {identity?.core_avg_confidence ? (
                  <span> · 置信度 {Math.round(identity.core_avg_confidence * 100)}%</span>
                ) : null}
              </div>
            </div>

            {/* 小桔 + 环绕按键（限制在左栏内） */}
            <div
              style={{
                position: "relative",
                width: "100%",
                maxWidth: 640,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                margin: "8px 0 10px",
              }}
            >
              {/* 光环 */}
              <div
                style={{
                  position: "absolute",
                  width: 380,
                  height: 380,
                  borderRadius: "50%",
                  background:
                    "radial-gradient(circle,rgba(255,159,28,0.28),rgba(255,138,61,0.10) 55%,transparent 74%)",
                  animation: "uhaloPulse 5s ease-in-out infinite",
                  pointerEvents: "none",
                }}
              />

              {/* 小桔（放大主体） */}
              <div
                style={{
                  position: "relative",
                  width: 330,
                  height: 330,
                  animation: "ujuFloat 4s ease-in-out infinite",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                }}
              >
                <XiaojuMascot scale={1.45} emotion="idle" mood={likenessPct} />
              </div>

              {/* 按键：认知（左中） */}
              <div
                style={{
                  position: "absolute",
                  left: "2%",
                  top: "30%",
                  animation: "ukeyFloat 5.5s ease-in-out infinite",
                  animationDelay: "0s",
                  cursor: "pointer",
                }}
                onClick={() => openPanel("cognitive")}
              >
                <div
                  style={{
                    padding: "10px 14px",
                    borderRadius: 16,
                    background:
                      "linear-gradient(145deg,rgba(255,253,247,0.92),rgba(255,227,184,0.92))",
                    border: "1px solid rgba(255,154,61,0.45)",
                    boxShadow:
                      "0 5px 14px rgba(224,122,40,0.20), inset 0 -2px 0 rgba(255,154,61,0.22)",
                    fontSize: 13.5,
                    fontWeight: 700,
                    color: "#8A4A12",
                    textAlign: "center",
                    letterSpacing: 0.5,
                  }}
                >
                  🧠 它理解的我
                  <br />
                  <span style={{ fontSize: 10.5, fontWeight: 600, color: "#B0762A" }}>
                    认知 · {panelCounts.cognitive}
                  </span>
                </div>
              </div>

              {/* 按键：情感（右中） */}
              <div
                style={{
                  position: "absolute",
                  right: "2%",
                  top: "30%",
                  animation: "ukeyFloat 5.5s ease-in-out infinite",
                  animationDelay: "1.2s",
                  cursor: "pointer",
                }}
                onClick={() => openPanel("emotional")}
              >
                <div
                  style={{
                    padding: "10px 14px",
                    borderRadius: 16,
                    background:
                      "linear-gradient(145deg,rgba(255,253,247,0.92),rgba(255,233,198,0.92))",
                    border: "1px solid rgba(255,154,61,0.45)",
                    boxShadow:
                      "0 5px 14px rgba(224,122,40,0.20), inset 0 -2px 0 rgba(255,154,61,0.22)",
                    fontSize: 13.5,
                    fontWeight: 700,
                    color: "#8A4A12",
                    textAlign: "center",
                    letterSpacing: 0.5,
                  }}
                >
                  💛 它在乎的
                  <br />
                  <span style={{ fontSize: 10.5, fontWeight: 600, color: "#B0762A" }}>
                    情感 · {panelCounts.emotional}
                  </span>
                </div>
              </div>

              {/* 按键：侧写（正下方） */}
              <div
                style={{
                  position: "absolute",
                  left: "50%",
                  transform: "translateX(-50%)",
                  bottom: -14,
                  animation: "ukeyFloat 5.5s ease-in-out infinite",
                  animationDelay: "2.2s",
                  cursor: "pointer",
                }}
                onClick={() => openPanel("behavior")}
              >
                <div
                  style={{
                    padding: "10px 14px",
                    borderRadius: 16,
                    background:
                      "linear-gradient(145deg,rgba(255,253,247,0.92),rgba(253,232,194,0.92))",
                    border: "1px solid rgba(255,154,61,0.45)",
                    boxShadow:
                      "0 5px 14px rgba(224,122,40,0.20), inset 0 -2px 0 rgba(255,154,61,0.22)",
                    fontSize: 13.5,
                    fontWeight: 700,
                    color: "#8A4A12",
                    textAlign: "center",
                    letterSpacing: 0.5,
                  }}
                >
                  ✍️ 它在学什么
                  <br />
                  <span style={{ fontSize: 10.5, fontWeight: 600, color: "#B0762A" }}>
                    侧写 · {panelCounts.behavior}
                  </span>
                </div>
              </div>

              {/* 去回答 · 待澄清（左下/右下 3D 按钮） */}
              {gaps[0] && (
                <div
                  style={{
                    position: "absolute",
                    left: "3%",
                    bottom: "16%",
                    cursor: "pointer",
                    animation: "ukeyFloat 5s ease-in-out infinite",
                    animationDelay: "0.8s",
                  }}
                  onClick={() => setGapTip(gaps[0])}
                >
                  <div
                    style={{
                      padding: "9px 13px",
                      borderRadius: 15,
                      background: "linear-gradient(180deg,#FFC95C,#FF9F1C 60%,#F08A16)",
                      border: "1px solid rgba(224,122,40,0.6)",
                      boxShadow:
                        "0 5px 14px rgba(224,122,40,0.4), inset 0 2px 0 rgba(255,255,255,0.4), inset 0 -3px 0 rgba(180,90,10,0.35)",
                      fontSize: 12,
                      fontWeight: 700,
                      color: "#fff",
                      textShadow: "0 1px 2px rgba(150,70,0,0.4)",
                      textAlign: "center",
                    }}
                  >
                    💬 去回答·{gapLabel(gaps[0])}
                  </div>
                </div>
              )}
              {gaps[1] && (
                <div
                  style={{
                    position: "absolute",
                    right: "3%",
                    bottom: "16%",
                    cursor: "pointer",
                    animation: "ukeyFloat 5s ease-in-out infinite",
                    animationDelay: "1.6s",
                  }}
                  onClick={() => setGapTip(gaps[1])}
                >
                  <div
                    style={{
                      padding: "9px 13px",
                      borderRadius: 15,
                      background: "linear-gradient(180deg,#FFC95C,#FF9F1C 60%,#F08A16)",
                      border: "1px solid rgba(224,122,40,0.6)",
                      boxShadow:
                        "0 5px 14px rgba(224,122,40,0.4), inset 0 2px 0 rgba(255,255,255,0.4), inset 0 -3px 0 rgba(180,90,10,0.35)",
                      fontSize: 12,
                      fontWeight: 700,
                      color: "#fff",
                      textShadow: "0 1px 2px rgba(150,70,0,0.4)",
                      textAlign: "center",
                    }}
                  >
                    💬 去回答·{gapLabel(gaps[1])}
                  </div>
                </div>
              )}
            </div>
          </div>

          {/* ============ 右区：透明状态流（无卡片） ============ */}
          <div
            style={{
              flex: "0 0 240px",
              minWidth: 0,
              display: "flex",
              flexDirection: "column",
              justifyContent: "center",
              gap: 30,
              position: "relative",
              padding: "10px 0",
            }}
          >
            {/* 情景问答：一朵小花 + 计数 */}
            <div style={{ textAlign: "center", position: "relative" }}>
              <div style={{ fontSize: 13, fontWeight: 700, color: "#8A4A12", letterSpacing: 0.5 }}>
                情景问答
              </div>
              <div
                style={{
                  position: "relative",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  marginTop: 6,
                }}
              >
                <span
                  style={{
                    fontSize: 30,
                    display: "inline-block",
                    animation: "ubloom 4s ease-in-out infinite",
                    opacity: 0.9,
                    textShadow: "0 0 14px rgba(255,159,28,0.4)",
                  }}
                >
                  🌸
                </span>
                <span
                  style={{
                    fontSize: 15,
                    fontWeight: 800,
                    color: "#E8630A",
                    marginLeft: 4,
                    animation: "unumFloat 4s ease-in-out infinite",
                    animationDelay: "0.5s",
                  }}
                >
                  ×{gaps.length}
                </span>
              </div>
              <div style={{ fontSize: 10.5, color: "#B98A4A", marginTop: 4, opacity: 0.75 }}>
                每次访谈/验证，开一朵
              </div>
            </div>

            {/* 画像回灌：种子 + 会涨的数字 */}
            <div style={{ textAlign: "center", position: "relative" }}>
              <div style={{ fontSize: 13, fontWeight: 700, color: "#8A4A12", letterSpacing: 0.5 }}>
                画像回灌
              </div>
              <div
                style={{
                  position: "relative",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  marginTop: 6,
                }}
              >
                <span
                  style={{
                    fontSize: 26,
                    display: "inline-block",
                    animation: "useed 4.5s ease-in-out infinite",
                    opacity: 0.85,
                    textShadow: "0 0 12px rgba(245,169,127,0.5)",
                  }}
                >
                  🌱
                </span>
                <span
                  style={{
                    fontSize: 15,
                    fontWeight: 800,
                    color: "#E8630A",
                    marginLeft: 4,
                    animation: "unumFloat 4s ease-in-out infinite",
                    animationDelay: "1s",
                  }}
                >
                  {totalArticles} → {totalArticles + 1}
                </span>
              </div>
              <div style={{ fontSize: 10.5, color: "#B98A4A", marginTop: 4, opacity: 0.75 }}>
                回答变成新画像，条目会涨
              </div>
            </div>

            {/* 这里看到变化：像心跳的数字 */}
            <div style={{ textAlign: "center", position: "relative" }}>
              <div style={{ fontSize: 13, fontWeight: 700, color: "#8A4A12", letterSpacing: 0.5 }}>
                这里看到变化
              </div>
              <div
                style={{
                  position: "relative",
                  display: "flex",
                  alignItems: "baseline",
                  justifyContent: "center",
                  marginTop: 4,
                }}
              >
                <span
                  style={{
                    fontSize: 34,
                    fontWeight: 800,
                    color: "#E8630A",
                    animation: "uheart 3s ease-in-out infinite",
                    textShadow: "0 0 18px rgba(255,138,61,0.4)",
                  }}
                >
                  {likenessPct}%
                </span>
                <span
                  style={{
                    fontSize: 14,
                    fontWeight: 700,
                    color: "#F08A16",
                    marginLeft: 3,
                    animation: "unumFloat 3s ease-in-out infinite",
                    animationDelay: "0.6s",
                  }}
                >
                  ↑
                </span>
              </div>
              <div style={{ fontSize: 10.5, color: "#B98A4A", marginTop: 4, opacity: 0.75 }}>
                它懂了，数字就跳一下
              </div>
            </div>
          </div>
        </div>
      )}

      {/* ============ 弹层 1：维度数字（第 2 级） ============ */}
      {dimPanel && (
        <div
          onClick={(e) => {
            if (e.target === e.currentTarget) setDimPanel(null);
          }}
          style={{
            position: "fixed",
            inset: 0,
            zIndex: 70,
            background: "rgba(80,45,10,0.35)",
            backdropFilter: "blur(2px)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            padding: 20,
            boxSizing: "border-box",
          }}
        >
          <div
            style={{
              background: "linear-gradient(170deg,#FFF7E8,#FFEBCB)",
              borderRadius: 22,
              border: "1px solid rgba(255,154,61,0.4)",
              boxShadow: "0 18px 50px rgba(140,70,10,0.35)",
              padding: 20,
              maxWidth: 420,
              width: "100%",
              boxSizing: "border-box",
              maxHeight: "80vh",
              overflow: "auto",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 14 }}>
              <span style={{ fontSize: 16, fontWeight: 800, color: "#6B3A10" }}>
                {dimPanel.title}
              </span>
              <span
                onClick={() => setDimPanel(null)}
                style={{
                  marginLeft: "auto",
                  fontSize: 12,
                  color: "#B0762A",
                  cursor: "pointer",
                  background: "rgba(255,159,28,0.15)",
                  borderRadius: 10,
                  padding: "3px 10px",
                }}
              >
                ✕ 收起
              </span>
            </div>
            {dimPanel.chips.length === 0 ? (
              <div style={{ fontSize: 13, color: "#A07A45", padding: "8px 0" }}>
                这里还在学习中，多和它聊聊会慢慢长出来。
              </div>
            ) : (
              <div style={{ display: "flex", flexWrap: "wrap", gap: 9 }}>
                {dimPanel.chips.map((c) => (
                  <div
                    key={c.label}
                    onClick={() => setDetail({ title: `${c.label} · ${c.count} 条`, items: c.items })}
                    style={{
                      cursor: "pointer",
                      padding: "10px 14px",
                      borderRadius: 14,
                      background: "linear-gradient(145deg,#FFFDF7,#FFE3B8)",
                      border: "1px solid rgba(255,154,61,0.4)",
                      boxShadow: "0 3px 8px rgba(224,122,40,0.15)",
                      textAlign: "center",
                      flex: "1 1 130px",
                      minWidth: 0,
                      boxSizing: "border-box",
                    }}
                  >
                    <div style={{ fontSize: 20 }}>{c.icon}</div>
                    <div style={{ fontSize: 13, fontWeight: 700, color: "#8A4A12", marginTop: 3 }}>
                      {c.label} <span style={{ color: "#E8630A" }}>{c.count}</span>
                    </div>
                    <div style={{ fontSize: 10, color: "#B0762A", marginTop: 2 }}>
                      点这里看详情 →
                    </div>
                  </div>
                ))}
              </div>
            )}
            <div style={{ fontSize: 11, color: "#A07A45", marginTop: 12, lineHeight: 1.6 }}>
              点每个数字块，展开她理解的对应条目内容。
            </div>
          </div>
        </div>
      )}

      {/* ============ 弹层 2：具体条目详情（第 3 级） ============ */}
      {detail && (
        <div
          onClick={(e) => {
            if (e.target === e.currentTarget) setDetail(null);
          }}
          style={{
            position: "fixed",
            inset: 0,
            zIndex: 80,
            background: "rgba(80,45,10,0.4)",
            backdropFilter: "blur(2px)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            padding: 20,
            boxSizing: "border-box",
          }}
        >
          <div
            style={{
              background: "linear-gradient(170deg,#FFF7E8,#FFE7C2)",
              borderRadius: 22,
              border: "1px solid rgba(255,154,61,0.4)",
              boxShadow: "0 18px 50px rgba(140,70,10,0.35)",
              padding: 20,
              maxWidth: 440,
              width: "100%",
              boxSizing: "border-box",
              maxHeight: "82vh",
              overflow: "auto",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
              <span style={{ fontSize: 15, fontWeight: 800, color: "#6B3A10" }}>
                {detail.title}
              </span>
              <span
                onClick={() => setDetail(null)}
                style={{
                  marginLeft: "auto",
                  fontSize: 12,
                  color: "#B0762A",
                  cursor: "pointer",
                  background: "rgba(255,159,28,0.15)",
                  borderRadius: 10,
                  padding: "3px 10px",
                }}
              >
                ✕ 收起
              </span>
            </div>
            {detail.items.map((it, i) => (
              <div
                key={i}
                style={{
                  marginBottom: 10,
                  padding: "10px 12px",
                  borderRadius: 12,
                  background: "rgba(255,255,255,0.7)",
                  border: "1px solid rgba(255,154,61,0.18)",
                  boxSizing: "border-box",
                }}
              >
                <div style={{ fontSize: 12, fontWeight: 700, color: "#8A4A12" }}>
                  {i + 1}. {it.title}
                </div>
                <div
                  style={{
                    fontSize: 12,
                    color: "#5C3A1E",
                    lineHeight: 1.7,
                    marginTop: 4,
                    whiteSpace: "pre-line",
                  }}
                >
                  {it.content}
                </div>
                {it.evidence ? (
                  <div style={{ fontSize: 10.5, color: "#B0762A", marginTop: 5 }}>
                    {it.evidence}
                  </div>
                ) : null}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ============ 去回答 · 待澄清提示（接真访谈） ============ */}
      {gapTip && (
        <div
          onClick={(e) => {
            if (e.target === e.currentTarget) setGapTip(null);
          }}
          style={{
            position: "fixed",
            inset: 0,
            zIndex: 90,
            background: "rgba(80,45,10,0.4)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            padding: 20,
            boxSizing: "border-box",
          }}
        >
          <div
            style={{
              background: "linear-gradient(170deg,#FFF7E8,#FFE7C2)",
              borderRadius: 22,
              border: "1px solid rgba(255,154,61,0.4)",
              boxShadow: "0 18px 50px rgba(140,70,10,0.35)",
              padding: 22,
              maxWidth: 380,
              width: "100%",
              boxSizing: "border-box",
              textAlign: "center",
            }}
          >
            <div style={{ fontSize: 30 }}>🌼</div>
            <div style={{ fontSize: 15, fontWeight: 800, color: "#6B3A10", marginTop: 8 }}>
              💬 想更懂你：{gapTip.reason || gapTip.topic}
            </div>
            <div style={{ fontSize: 12, color: "#A07A45", marginTop: 10, lineHeight: 1.7 }}>
              这里会弹出 2-3 个问题（和深度/情感访谈同一套），回答后自动回灌到{gapTip.layer === "emotional" ? "情感" : "认知"}画像。
            </div>
            <div style={{ marginTop: 14, display: "flex", gap: 10, justifyContent: "center" }}>
              <button
                onClick={handleStartGap}
                disabled={startingGap}
                style={{
                  cursor: "pointer",
                  fontSize: 12.5,
                  fontWeight: 700,
                  background: "linear-gradient(180deg,#FFC95C,#FF9F1C 60%,#F08A16)",
                  color: "#fff",
                  border: "none",
                  borderRadius: 14,
                  padding: "8px 20px",
                  boxShadow: "0 4px 10px rgba(224,122,40,0.35)",
                  opacity: startingGap ? 0.7 : 1,
                }}
              >
                {startingGap ? "准备问题…" : "开始回答"}
              </button>
              <button
                onClick={() => setGapTip(null)}
                style={{
                  cursor: "pointer",
                  fontSize: 12.5,
                  color: "#B0762A",
                  background: "rgba(255,159,28,0.12)",
                  border: "none",
                  borderRadius: 14,
                  padding: "8px 16px",
                }}
              >
                稍后
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
