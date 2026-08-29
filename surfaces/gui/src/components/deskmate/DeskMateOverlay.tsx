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
import { authedFetch, httpBase, getMemorySettings, setMemorySettings } from "../../api";
import { CicadaMascot } from "./CicadaMascot";
import { useMascotWalk } from "./useMascotWalk";
import { useProactiveTick } from "./useProactiveTick";
import { StatusPanelV2 } from "./StatusPanelV2";
import type { GrowthSnapshot, LearnedHistory } from "./StatusPanel";
import { SuggestionToast, type Suggestion } from "./SuggestionToast";
import { CareToast, type CareMessage } from "./CareToast";
import { MemoryViewer, type MemoryFile } from "./MemoryViewer";
import { MemoryGraphPage } from "./MemoryGraphPage";
import { BadgeMedal } from "./BadgeMedal";
import type { BadgeRarity } from "./BadgeWall";
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

/** 新解锁勋章的报喜数据（来自后端 growth/badges 快照的 newly_unlocked）。 */
export interface CelebrationItem {
  id: string;
  name: string;
  glyph: string;
  icon: string;
  category: string;
  rarity: string;
  celebration: string;
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

export function DeskMateOverlay({ agent, sessionId, activeExpert }: Props) {
  const [panelOpen, setPanelOpen] = useState(false);
  const [memoryOpen, setMemoryOpen] = useState(false);
  const [memoryGraphOpen, setMemoryGraphOpen] = useState(false);
  const [proofreadOpen, setProofreadOpen] = useState(false);
  // 首页 chip 触发：了解度轻量卡 / 批改台进入焦点
  const [understandingOpen, setUnderstandingOpen] = useState(false);
  const [proofreadFocus, setProofreadFocus] = useState<"edit" | "manuscripts" | "style" | null>(null);
  // 主动模式开关（后台建议），需在 useProactiveTick 之前声明
  const [proactiveEnabled, setProactiveEnabled] = useState(true);
  // 记忆自动注入开关（P0-1：默认开启）
  const [injectEnabled, setInjectEnabled] = useState(true);

  // 桌宠固定右下角（不游走、不遮挡文字/操作）。首页与对话页统一停靠角落。
  const mascotMode: "free" | "dock" | "corner" = "corner";

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
    mode: mascotMode,
    idleDuration: [5000, 12000],
    walkDuration: [4000, 8000],
  });

  // 状态面板数据（V2 精简版不再展示，保留供事件系统使用）
  const [_activity, setActivity] = useState("待机中");
  const [_confidence, setConfidence] = useState<number | undefined>();
  const [_memories, setMemories] = useState<string[]>([]);

  // 建议弹窗
  const [suggestion, setSuggestion] = useState<Suggestion | null>(null);
  // P2-5 连续情感关怀：温暖问候（与创作建议分开呈现）
  const [careMessage, setCareMessage] = useState<CareMessage | null>(null);

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

  // 成长系统快照（等级/亲密度/信任感/默契度/技能熟练度），面板展开时拉取
  const [growth, setGrowth] = useState<GrowthSnapshot | null>(null);

  // 勋章解锁报喜：队列 + 当前正在展示的一枚
  const [celebrationQueue, setCelebrationQueue] = useState<CelebrationItem[]>([]);
  const [activeCelebration, setActiveCelebration] = useState<CelebrationItem | null>(null);
  // 已报喜过的勋章 id（组件生命周期内去重，防止后端若重复返回时反复弹出）
  const celebratedIdsRef = useRef<Set<string>>(new Set());

  // 统一拉取 growth：既刷新状态面板数据，也读取 newly_unlocked 触发报喜。
  // check_badges 在服务端幂等（落盘后同一次解锁只报一次），前端无需去重。
  const refreshGrowth = useCallback(async () => {
    try {
      const r = await authedFetch(`${httpBase()}/v1/deskmate/growth`);
      if (!r.ok) return;
      const d = await r.json();
      if (d && typeof d.level === "number") {
        setGrowth(d);
        const raw = Array.isArray(d.newly_unlocked) ? d.newly_unlocked : [];
        const fresh = (raw as CelebrationItem[]).filter(
          (b) => b?.id && !celebratedIdsRef.current.has(b.id),
        );
        if (fresh.length) {
          fresh.forEach((b) => celebratedIdsRef.current.add(b.id));
          setCelebrationQueue((q) => [...q, ...fresh]);
        }
      }
    } catch { /* 静默降级 */ }
  }, []);

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
    // 拉取成长系统快照（等级/亲密度/信任感/默契度/技能熟练度）+ 检测新解锁勋章
    void refreshGrowth();
  }, [panelOpen]);

  // 挂载时拉取一次 growth 快照（同时检测是否有新解锁勋章可报喜）
  useEffect(() => {
    void refreshGrowth();
  }, [refreshGrowth]);

  // 勋章解锁报喜：定时自检（覆盖聊天计数 / 圆桌等不经由面板的路径）+ 事件触发
  useEffect(() => {
    const onRefresh = () => { void refreshGrowth(); };
    window.addEventListener("deskmate:badges-refresh", onRefresh);
    const timer = window.setInterval(() => {
      if (!document.hidden) void refreshGrowth();
    }, 20_000);
    return () => {
      window.removeEventListener("deskmate:badges-refresh", onRefresh);
      window.clearInterval(timer);
    };
  }, [refreshGrowth]);

  // 报喜队列推进：一次只展示一枚（activeCelebration 为空且队列非空时取下一枚）
  useEffect(() => {
    if (activeCelebration || celebrationQueue.length === 0) return;
    const [next, ...rest] = celebrationQueue;
    setActiveCelebration(next);
    setCelebrationQueue(rest);
    // 让桌宠切换到开心情绪，配合报喜
    window.dispatchEvent(new CustomEvent("deskmate:emotion", { detail: { emotion: "happy" } }));
  }, [activeCelebration, celebrationQueue]);

  // 自动消失：当前这枚展示 6 秒后关闭。单独成 effect，避免被「队列推进」的
  // setState 触发 cleanup 把定时器清掉（原实现因此导致弹窗永久不消失）。
  useEffect(() => {
    if (!activeCelebration) return;
    const t = window.setTimeout(() => setActiveCelebration(null), 6000);
    return () => window.clearTimeout(t);
  }, [activeCelebration]);

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

  // 挂载时读取记忆注入状态（P0-1）
  useEffect(() => {
    getMemorySettings()
      .then((s) => setInjectEnabled(s.auto_inject !== false))
      .catch(() => setInjectEnabled(true));
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

  const handleToggleInject = useCallback((enabled: boolean) => {
    setInjectEnabled(enabled);
    setMemorySettings({ auto_inject: enabled }).catch((err: unknown) => {
      console.error("memory auto_inject toggle failed", err);
      setInjectEnabled(!enabled); // 回滚 UI
    });
  }, []);

  // --- 事件监听 ---

  useEffect(() => {
    const onSuggestion = (e: Event) => {
      const d = (e as CustomEvent<SuggestionEvent>).detail;
      if (!d?.suggestion_content) return;
      // 用 suggestion_type 做粗去重（LLM 每次生成的文本不同，精确 key 匹配无效）
      const sugType = d.suggestion_type || "generic";

      // P2-5 情感关怀：走 CareToast 温暖呈现，7 天内不重复
      if (sugType === "emotional_care") {
        try {
          const lastCare = localStorage.getItem("deskmate_last_care_ts");
          if (lastCare && Date.now() - parseInt(lastCare, 10) < 7 * 86400 * 1000) return;
          localStorage.setItem("deskmate_last_care_ts", String(Date.now()));
        } catch { /* localStorage 不可用时跳过本地去重 */ }
        setCareMessage({
          id: d.id,
          text: d.suggestion_content,
          type: sugType,
          confidence: d.confidence,
        });
        return;
      }

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
    // 首页 chip 入口：文稿编校 / 书稿库 / 风格库 → 打开批改台（带焦点）
    const onOpenProofread = (e: Event) => {
      const d = (e as CustomEvent<{ focus?: string }>).detail;
      const focus = d?.focus;
      setProofreadFocus(focus === "manuscripts" || focus === "style" ? focus : "edit");
      setProofreadOpen(true);
    };
    // 首页 chip 入口：我的记忆 → 打开记忆图谱（全屏独立页）
    const onOpenMemory = () => setMemoryGraphOpen(true);
    // 首页 chip 入口：了解度 → 打开轻量卡
    const onOpenUnderstanding = () => setUnderstandingOpen(true);
    // 首页 chip 入口：主动建议 → 切换开关
    const onToggleProactive = () => handleToggleProactive(!proactiveEnabled);

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

  // 手动关闭勋章报喜气泡：同时清空队列，避免立刻又弹下一枚
  const handleCloseCelebration = useCallback(() => {
    setActiveCelebration(null);
    setCelebrationQueue([]);
  }, []);

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
          {/* 知了桌宠 - 分层 SVG 蝉 + 翅膀扇动 + 呼吸浮动 */}
          <CicadaMascot
            emotion={currentEmotion}
            walkDirection={direction}
            isWalking={mascotState === "walking"}
            scale={petScale}
            mood={growth?.mood ?? 80}
            vitality={growth?.vitality ?? 100}
            level={growth?.level ?? 1}
            onClick={() => {
              try { setPanelOpen((v) => !v); } catch (e) { console.error("click error", e); }
            }}
            onDoubleClick={() => {
              try { setMemoryGraphOpen(true); } catch (e) { console.error("dblclick error", e); }
            }}
          />
          {activeCelebration && (
            <div className="mascot-speech mascot-celebrate" data-testid="badge-celebration">
              <button
                type="button"
                className="mascot-speech-close"
                aria-label="关闭"
                onClick={handleCloseCelebration}
              >
                ×
              </button>
              <BadgeMedal
                rarity={activeCelebration.rarity as BadgeRarity}
                glyph={activeCelebration.glyph}
                category={activeCelebration.category}
                size={34}
              />
              <div className="mascot-speech-body">
                <div className="mascot-speech-kicker">获得新勋章</div>
                <div className="mascot-speech-name">{activeCelebration.name}</div>
                <div className="mascot-speech-text">{activeCelebration.celebration}</div>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* 状态面板 V2 — 精致重设计版，4层信息架构 */}
      <StatusPanelV2
        emotion={currentEmotion}
        open={panelOpen}
        onClose={() => setPanelOpen(false)}
        onOpenMemory={() => setMemoryGraphOpen(true)}
        onOpenProofread={() => setProofreadOpen(true)}
        proactiveEnabled={proactiveEnabled}
        onToggleProactive={handleToggleProactive}
        injectEnabled={injectEnabled}
        onToggleInject={handleToggleInject}
        learned={learned}
        growth={growth}
        achievements={growth?.achievements ?? []}
        personality={growth?.personality ?? { energy: 0, logic: 0, creativity: 0, empathy: 0 }}
        streakDays={growth?.streak_days ?? 0}
        totalChats={growth?.total_chats ?? 0}
      />

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

      {/* P2-5 情感关怀 - 右上角温暖呈现（与创作建议分开） */}
      <CareToast
        message={careMessage}
        onClose={() => setCareMessage(null)}
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

      {/* 记忆查看/编辑器 — 全屏模态（图谱页「列表视图」可达） */}
      {memoryOpen && (
        <MemoryViewer
          files={memoryFiles}
          onSave={handleSaveMemory}
          onClose={() => setMemoryOpen(false)}
        />
      )}

      {/* 记忆图谱 — 全屏独立页（「查看记忆 / 我们的记忆」入口） */}
      {memoryGraphOpen && (
        <MemoryGraphPage
          open={memoryGraphOpen}
          onClose={() => setMemoryGraphOpen(false)}
          onOpenList={() => {
            setMemoryGraphOpen(false);
            setMemoryOpen(true);
          }}
        />
      )}

      {/* 写作批改（文稿编校 / 书稿库 / 风格库）— 全屏模态 */}
      {proofreadOpen && (
        <ProofreadPanel
          open={proofreadOpen}
          sessionId={sessionId}
          initialFocus={proofreadFocus ?? undefined}
          onClose={() => {
            setProofreadOpen(false);
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
