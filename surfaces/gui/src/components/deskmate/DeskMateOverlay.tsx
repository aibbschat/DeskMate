/**
 * DeskMateOverlay — 将桌宠角色、状态面板、建议弹窗集成为一个浮动覆盖层。
 *
 * 在 App.tsx 中按条件渲染：仅当 deskmate persona 激活时显示。
 *
 * 事件接口（通过 window CustomEvent 通信）：
 * - "deskmate:suggestion" → 显示建议弹窗
 * - "deskmate:emotion"   → 切换情感状态
 * - "deskmate:status"    → 更新状态面板数据
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { authedFetch, httpBase } from "../../api";
import { Mascot } from "./Mascot";
import { useMascotWalk } from "./useMascotWalk";
import { useProactiveTick } from "./useProactiveTick";
import { StatusPanel, type LearnedHistory } from "./StatusPanel";
import { SuggestionToast, type Suggestion } from "./SuggestionToast";
import { MemoryViewer, type MemoryFile } from "./MemoryViewer";
import { MemoryLearnedToast } from "./MemoryLearnedToast";
import { ProofreadPanel } from "./ProofreadPanel";
import { UnderstandingCard } from "./UnderstandingCard";
import {
  type EmotionName,
  type EmotionParams,
  emotionToMascotState,
  mascotToEmotionName,
} from "./EmotionState";
import { type Expert } from "./experts";

/* ------------------------------------------------------------------ */
/*  外部事件 payload 类型                                               */
/* ------------------------------------------------------------------ */

export interface SuggestionEvent {
  id: string;
  suggestion_type?: string;
  suggestion_content: string;
  confidence: number;
  reasoning?: string;
}

export interface StatusUpdate {
  activity?: string;
  confidence?: number;
  memories?: string[];
  emotion?: EmotionName;
}

/* ------------------------------------------------------------------ */
/*  组件                                                               */
/* ------------------------------------------------------------------ */

interface Props {
  /** 当前 agent id，仅 "deskmate" 时显示覆盖层 */
  agent: string;
  /** 当前会话 id（记忆保存时回传后端广播用） */
  sessionId?: string;
  /** 是否处于空会话首页（桌搭模式 + 无消息）。用于把桌宠切到 dock 横走模式。 */
  idle?: boolean;
  /** 当前激活的专家（专家模式）；存在时强制隐藏首页，进入对话界面。 */
  activeExpert?: Expert | null;
}

export function DeskMateOverlay({ agent, sessionId, idle, activeExpert }: Props) {
  const [panelOpen, setPanelOpen] = useState(false);
  const [memoryOpen, setMemoryOpen] = useState(false);
  const [proofreadOpen, setProofreadOpen] = useState(false);
  // 聊天「审阅第X章」触发时，携带预载数据打开批改台
  const [proofreadSeed, setProofreadSeed] = useState<import("./ProofreadPanel").ProofreadSeed | null>(null);
  // 首页 chip 触发：了解度轻量卡 / 批改台进入焦点
  const [understandingOpen, setUnderstandingOpen] = useState(false);
  const [proofreadFocus, setProofreadFocus] = useState<"edit" | "manuscripts" | "style" | null>(null);
  // 主动模式开关（后台建议），需在 useProactiveTick 之前声明
  const [proactiveEnabled, setProactiveEnabled] = useState(true);

  // 空会话首页（桌搭 + 无消息）：桌宠切到 dock 横走模式；其余保持自由游走。
  const isHome = agent === "deskmate" && !!idle && !activeExpert;

  // 主动感知 TICK（受主动模式开关控制）
  useProactiveTick({ agent, enabled: proactiveEnabled });

  // 桌宠尺寸（设置中可调，localStorage 持久化）
  const [petScale, setPetScale] = useState<number>(() => {
    const v = parseFloat(localStorage.getItem("deskmate_pet_scale") || "");
    return Number.isFinite(v) && v >= 0.5 && v <= 2 ? v : 1.0;
  });
  useEffect(() => {
    const onScale = (e: Event) => {
      const v = (e as CustomEvent<number>).detail;
      if (typeof v === "number" && v >= 0.5 && v <= 2) setPetScale(v);
    };
    window.addEventListener("deskmate:pet-scale", onScale as EventListener);
    return () => window.removeEventListener("deskmate:pet-scale", onScale as EventListener);
  }, []);

  // 桌宠行走逻辑
  const {
    position,
    state: mascotState,
    direction,
    setState: setMascotState,
  } = useMascotWalk({
    size: Math.round(180 * petScale),
    enabled: (agent === "deskmate" || !!activeExpert) && !panelOpen,
    mode: isHome ? "dock" : "free",
    idleDuration: [5000, 12000],
    walkDuration: [4000, 8000],
  });

  // 状态面板数据
  const [activity, setActivity] = useState("待机中");
  const [confidence, setConfidence] = useState<number | undefined>();
  const [memories, setMemories] = useState<string[]>([]);

  // 建议弹窗
  const [suggestion, setSuggestion] = useState<Suggestion | null>(null);

  // 建议去重：已处理（接受/忽略）的 type 在冷却期内永不再弹；
  // 无操作最多展示 3 次。用 localStorage 持久化，防止组件重挂载后丢失。
  const actedTypesRef = useRef<Set<string>>(new Set(
    (() => {
      try {
        const stored = localStorage.getItem("deskmate_acted_types");
        if (!stored) return [];
        const entries = JSON.parse(stored) as { type: string; ts: number }[];
        const now = Date.now();
        return entries.filter((e) => now - e.ts < 30 * 60_000).map((e) => e.type);
      } catch { return []; }
    })(),
  ));
  const shownCountRef = useRef<Map<string, number>>(new Map());

  // 持久化 actedTypes 到 localStorage
  const persistActedTypes = useCallback(() => {
    try {
      const now = Date.now();
      const entries = Array.from(actedTypesRef.current).map((type) => ({ type, ts: now }));
      localStorage.setItem("deskmate_acted_types", JSON.stringify(entries));
    } catch { /* 静默降级 */ }
  }, []);

  // 「我刚记住了」提示
  const [learnedFacts, setLearnedFacts] = useState<string[] | null>(null);

  // 学习历史（了解度 + 时间线），面板展开时拉取
  const [learned, setLearned] = useState<LearnedHistory | null>(null);

  // 记忆注入低调提示（非模态、节流）
  const [contextHint, setContextHint] = useState<string | null>(null);
  const lastCtxRef = useRef(0);

  // 记忆文件（供 MemoryViewer 使用，实际从 MCP 加载）
  const [memoryFiles, setMemoryFiles] = useState<MemoryFile[]>([
    { id: "soul", name: "SOUL.md", path: "~/.deskmate/SOUL.md", content: "", lastModified: null },
    { id: "user", name: "USER.md", path: "~/.deskmate/USER.md", content: "", lastModified: null },
    { id: "memory", name: "MEMORY.md", path: "~/.deskmate/MEMORY.md", content: "", lastModified: null },
  ]);

  // 打开记忆查看器时即时拉取真实记忆内容（避免首轮前显示空白）
  useEffect(() => {
    if (!memoryOpen) return;
    authedFetch(`${httpBase()}/v1/deskmate/memory`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d?.files?.length) setMemoryFiles(d.files);
      })
      .catch(() => {/* 静默降级，保留当前内容 */});
  }, [memoryOpen]);

  // 展开感知面板时拉取学习历史（了解度 + 时间线）
  useEffect(() => {
    if (!panelOpen) return;
    authedFetch(`${httpBase()}/v1/deskmate/learned-history`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (d && d.items) setLearned(d);
      })
      .catch(() => {/* 静默降级 */});
  }, [panelOpen]);

  // 挂载时读取主动模式状态
  useEffect(() => {
    authedFetch(`${httpBase()}/v1/deskmate/proactive`)
      .then((r) => r.json())
      .then((d) => {
        if (d && typeof d.silent === "boolean") {
          setProactiveEnabled(!d.silent);
        }
      })
      .catch(() => {/* 静默降级 */});
  }, []);

  const handleToggleProactive = useCallback((enabled: boolean) => {
    setProactiveEnabled(enabled);
    authedFetch(`${httpBase()}/v1/deskmate/proactive`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    }).catch((err: unknown) => {
      console.error("deskmate proactive toggle failed", err);
      // 回滚 UI
      setProactiveEnabled(!enabled);
    });
  }, []);

  // --- 事件监听 ---

  useEffect(() => {
    const onSuggestion = (e: Event) => {
      const d = (e as CustomEvent<SuggestionEvent>).detail;
      if (!d?.suggestion_content) return;
      // 用 suggestion_type 做粗去重（LLM 每次生成的文本不同，精确 key 匹配无效）
      const sugType = d.suggestion_type || "generic";
      // 用户已接受/忽略过此类型 -> 30 分钟内不再弹
      if (actedTypesRef.current.has(sugType)) return;
      // 无操作已展示 3 次 -> 不再自动弹（等用户有动作或新内容）
      const shown = shownCountRef.current.get(sugType) ?? 0;
      if (shown >= 3) return;
      shownCountRef.current.set(sugType, shown + 1);
      setSuggestion({
        id: d.id,
        type: sugType,
        text: d.suggestion_content,
        confidence: d.confidence,
        reason: d.reasoning,
        timestamp: Date.now(),
      });
    };
    const onEmotion = (e: Event) => {
      const d = (e as CustomEvent<{ emotion: EmotionName; params?: EmotionParams }>).detail;
      if (d?.emotion) {
        // 同步到桌宠状态
        setMascotState(emotionToMascotState(d.emotion));
      }
    };
    const onStatus = (e: Event) => {
      const d = (e as CustomEvent<StatusUpdate>).detail;
      if (!d) return;
      if (d.activity) setActivity(d.activity);
      if (d.confidence !== undefined) setConfidence(d.confidence);
      if (d.memories) setMemories(d.memories);
      if (d.emotion) {
        setMascotState(emotionToMascotState(d.emotion));
      }
    };
    const onMemoryUpdate = (e: Event) => {
      const d = (e as CustomEvent<{ files: MemoryFile[] }>).detail;
      if (d?.files) setMemoryFiles(d.files);
    };
    const onMemoryLearned = (e: Event) => {
      const d = (e as CustomEvent<{ facts?: string[] }>).detail;
      if (d?.facts?.length) setLearnedFacts(d.facts);
    };
    const onContextInjected = (e: Event) => {
      const d = (e as CustomEvent<{ count?: number }>).detail;
      const now = Date.now();
      if (now - lastCtxRef.current < 60_000) return; // 60s 节流，避免刷屏
      const n = d?.count ?? 0;
      if (n <= 0) return;
      lastCtxRef.current = now;
      setContextHint(`我从记忆中找到了 ${n} 条相关上下文`);
      window.setTimeout(() => setContextHint(null), 4000);
    };

    window.addEventListener("deskmate:suggestion", onSuggestion);
    window.addEventListener("deskmate:emotion", onEmotion);
    window.addEventListener("deskmate:status", onStatus);
    window.addEventListener("deskmate:memory-update", onMemoryUpdate);
    window.addEventListener("deskmate:memory-learned", onMemoryLearned);
    window.addEventListener("deskmate:context-injected", onContextInjected);
    // 聊天「审阅第X章」：打开批改台并预载到对应章节
    const onProofreadEvent = (e: Event) => {
      const d = (e as CustomEvent).detail;
      if (d && d.chapters && d.result) {
        setProofreadSeed(d as import("./ProofreadPanel").ProofreadSeed);
        setProofreadOpen(true);
      }
    };
    // 首页 chip 入口：文稿编校 / 书稿库 / 风格库 → 打开批改台（带焦点）
    const onOpenProofread = (e: Event) => {
      const d = (e as CustomEvent<{ focus?: string }>).detail;
      const focus = d?.focus;
      setProofreadFocus(focus === "manuscripts" || focus === "style" ? focus : "edit");
      setProofreadOpen(true);
    };
    // 首页 chip 入口：我的记忆 → 打开记忆查看器
    const onOpenMemory = () => setMemoryOpen(true);
    // 首页 chip 入口：了解度 → 打开轻量卡
    const onOpenUnderstanding = () => setUnderstandingOpen(true);
    // 首页 chip 入口：主动建议 → 切换开关
    const onToggleProactive = () => handleToggleProactive(!proactiveEnabled);

    window.addEventListener("deskmate:proofread", onProofreadEvent);
    window.addEventListener("deskmate:open-proofread", onOpenProofread);
    window.addEventListener("deskmate:open-memory", onOpenMemory);
    window.addEventListener("deskmate:open-understanding", onOpenUnderstanding);
    window.addEventListener("deskmate:toggle-proactive", onToggleProactive);
    return () => {
      window.removeEventListener("deskmate:suggestion", onSuggestion);
      window.removeEventListener("deskmate:emotion", onEmotion);
      window.removeEventListener("deskmate:status", onStatus);
      window.removeEventListener("deskmate:memory-update", onMemoryUpdate);
      window.removeEventListener("deskmate:memory-learned", onMemoryLearned);
      window.removeEventListener("deskmate:context-injected", onContextInjected);
      window.removeEventListener("deskmate:proofread", onProofreadEvent);
      window.removeEventListener("deskmate:open-proofread", onOpenProofread);
      window.removeEventListener("deskmate:open-memory", onOpenMemory);
      window.removeEventListener("deskmate:open-understanding", onOpenUnderstanding);
      window.removeEventListener("deskmate:toggle-proactive", onToggleProactive);
    };
  }, [setMascotState, handleToggleProactive, proactiveEnabled]);

  // NOTE: emotion is driven explicitly by the `deskmate:status` / `deskmate:emotion`
  // events (dispatched from App.tsx from real session-agent state), not auto-derived
  // from the activity string — so the particle always reflects the true live state.

  const handleAccept = useCallback((s: Suggestion) => {
    const sugType = s.type || "generic";
    actedTypesRef.current.add(sugType);
    persistActedTypes();
    window.dispatchEvent(
      new CustomEvent("deskmate:feedback", {
        detail: { key: s.text.slice(0, 100), accepted: true },
      }),
    );
    // 反馈回传后端，让防打扰机制生效（连续忽略 3 次降级、30 分钟冷却）
    authedFetch(`${httpBase()}/v1/deskmate/feedback`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ suggestion_key: s.text.slice(0, 100), accepted: true }),
    }).catch((err: unknown) => console.error("deskmate feedback failed", err));
    setSuggestion(null);
  }, [persistActedTypes]);

  const handleDismiss = useCallback((s: Suggestion) => {
    const sugType = s.type || "generic";
    actedTypesRef.current.add(sugType);
    persistActedTypes();
    window.dispatchEvent(
      new CustomEvent("deskmate:feedback", {
        detail: { key: s.text.slice(0, 100), accepted: false },
      }),
    );
    authedFetch(`${httpBase()}/v1/deskmate/feedback`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ suggestion_key: s.text.slice(0, 100), accepted: false }),
    }).catch((err: unknown) => console.error("deskmate feedback failed", err));
    setSuggestion(null);
  }, [persistActedTypes]);

  const handleSaveMemory = useCallback((fileId: string, content: string) => {
    // 乐观更新：先让界面立即反映编辑结果
    setMemoryFiles((prev) =>
      prev.map((f) => (f.id === fileId ? { ...f, content, lastModified: Date.now() } : f)),
    );
    // 持久化到 ~/.deskmate 并通过后端广播刷新浮层
    authedFetch(`${httpBase()}/v1/deskmate/memory`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ section: fileId, content, session_id: sessionId }),
    }).catch((err: unknown) => {
      // 保存失败不阻塞 UI；控制台可见
      console.error("save deskmate memory failed", err);
    });
  }, [sessionId]);

  // deskmate persona 或专家模式时渲染
  if (agent !== "deskmate" && !activeExpert) return null;

  // 安全检查：确保位置有效
  if (!position || isNaN(position.x) || isNaN(position.y)) return null;

  // 将 mascotState 转换回 emotion 供 StatusPanel 使用
  const currentEmotion = mascotToEmotionName(mascotState);

  return (
    <>
      {/* 桌宠角色 — 可在聊天输入框上方区域行走 */}
      <div
        className="fixed z-40"
        style={{
          left: position.x,
          top: position.y,
          transition: "left 0.1s linear, top 0.1s linear",
        }}
        data-testid="deskmate-mascot"
      >
        <div className="relative">
          {/* 墨灵桌宠 - 水墨形象 + 水墨动效，状态驱动情绪/朝向 */}
          <Mascot
            emotion={currentEmotion}
            walkDirection={direction}
            isWalking={mascotState === "walking"}
            scale={petScale}
            onClick={() => {
              try { setPanelOpen((v) => !v); } catch (e) { console.error("click error", e); }
            }}
            onDoubleClick={() => {
              try { setMemoryOpen(true); } catch (e) { console.error("dblclick error", e); }
            }}
          />
          {/* 状态面板 — 点击桌宠后显示 */}
          <StatusPanel
            emotion={currentEmotion}
            currentActivity={activity}
            recentMemories={memories}
            confidence={confidence}
            open={panelOpen}
            onClose={() => setPanelOpen(false)}
            onOpenMemory={() => setMemoryOpen(true)}
            onOpenProofread={() => setProofreadOpen(true)}
            proactiveEnabled={proactiveEnabled}
            onToggleProactive={handleToggleProactive}
            learned={learned}
            petPos={position}
            petWidth={Math.round(180 * petScale)}
          />
        </div>
      </div>

      {/* 建议弹窗 - 右上角 */}
      <SuggestionToast
        suggestion={suggestion}
        onAccept={handleAccept}
        onDismiss={handleDismiss}
        onAutoDismiss={() => {
          // 自动消失：只清本地状态，不触发后端 record_feedback，
          // 不加入 actedTypes（用户没看到不代表忽略了）
          setSuggestion(null);
        }}
      />

      {/* 「我刚记住了」提示 — 左下角，数秒后自动消失 */}
      {learnedFacts && (
        <MemoryLearnedToast facts={learnedFacts} onDone={() => setLearnedFacts(null)} />
      )}

      {/* 记忆注入低调提示 — 左下角，数秒后自动消失，非模态、不抢操作 */}
      {contextHint && (
        <div
          className="fixed bottom-4 left-4 z-50 rounded-lg border px-3 py-2 text-[12px] shadow-lg"
          style={{
            background: "var(--panel)",
            borderColor: "var(--line)",
            color: "var(--muted)",
          }}
        >
          {contextHint}
        </div>
      )}

      {/* 记忆查看/编辑器 — 全屏模态 */}
      {memoryOpen && (
        <MemoryViewer
          files={memoryFiles}
          onSave={handleSaveMemory}
          onClose={() => setMemoryOpen(false)}
        />
      )}

      {/* 写作批改（文稿编校 / 书稿库 / 风格库）— 全屏模态 */}
      {proofreadOpen && (
        <ProofreadPanel
          open={proofreadOpen}
          seed={proofreadSeed}
          initialFocus={proofreadFocus ?? undefined}
          onClose={() => {
            setProofreadOpen(false);
            setProofreadSeed(null);
            setProofreadFocus(null);
          }}
        />
      )}

      {/* 了解度轻量卡 — 首页 chip 触发 */}
      {understandingOpen && (
        <UnderstandingCard onClose={() => setUnderstandingOpen(false)} />
      )}
    </>
  );
}
