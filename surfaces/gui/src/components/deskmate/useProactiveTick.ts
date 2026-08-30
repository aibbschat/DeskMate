import { useEffect, useRef } from "react";
import { authedFetch, httpBase } from "../../api";
import { type EmotionName } from "./EmotionState";

/** 桌宠是否正处于一轮对话中（由 App.tsx 在 turn_start/turn_done 时维护）。
 * 对话进行中主动心跳不抢状态、也不浪费 LLM 调用。 */
declare global {
  interface Window {
    __deskmateTurnActive?: boolean;
  }
}

/**
 * 主动感知 TICK —— 改为「桌面活动触发」而非盲心跳。
 *
 * - 在 deskmate 模式且主动模式开启时运行；对话进行中跳过。
 * - 用户回到窗口 / 与界面交互（focus / 可见 / mousemove / keydown）时去抖触发（≥45s 一次），
 *   对应「感知到桌面活动变化才跑推理，平时不打扰」。
 * - 保留一个 5 分钟慢心跳兜底，保证长时间无交互时仍有生命感。
 */
export function useProactiveTick({
  agent,
  enabled = true,
}: {
  agent: string;
  enabled?: boolean;
}) {
  const heartbeatRef = useRef<ReturnType<typeof setTimeout>>();

  useEffect(() => {
    if (agent !== "deskmate" || !enabled) return;

    let lastTick = 0;
    const MIN_GAP = 15_000; // 15s（原 45s 太长，用户离开再回来要等很久才感知到状态变化）
    // 慢心跳兜底间隔：默认 5 分钟；后端 tick 返回 heartbeat_interval_ms 时用活力调制值覆盖
    // （活力低 → 间隔拉长 → 累了不打扰）。
    const heartbeatMsRef = { current: 5 * 60_000 };

    const tick = async () => {
      // 对话进行中：不动状态、不烧 LLM，等本轮结束再说
      if (window.__deskmateTurnActive) return;
      try {
        const res = await authedFetch(`${httpBase()}/v1/deskmate/proactive/tick`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        });
        if (!res.ok) return;
        const data = await res.json();
        if (!data) return;

        // 活力调制的心跳间隔：后端已按活力折算好，前端据此重新调度慢心跳
        if (data.heartbeat_interval_ms && data.heartbeat_interval_ms >= 5_000) {
          heartbeatMsRef.current = data.heartbeat_interval_ms;
        }

        // 对话中再次校验（tick 请求发出后可能已进入一轮）
        if (window.__deskmateTurnActive) return;

        // 推送状态更新（仅空闲时，避免覆盖真实对话状态）
        // 复用后端已有的系统活跃度（user_activity），无需申请辅助功能权限：
        //   在桌前活跃 → focused（专注陪伴中）
        //   离开电脑   → idle（待机）
        //   忙碌/会议  → concerned
        let emotion: EmotionName = "thinking";
        let activityText = data.suggestion_content || data.reason || "待机中";
        if (data.action === "no_action") {
          if (data.user_activity === "active") {
            emotion = "focused";
            activityText = "专注陪伴中";
          } else if (data.user_activity === "busy") {
            emotion = "concerned";
            activityText = data.reason || "忙碌中（会议 / 通话）";
          } else {
            emotion = "idle";
            activityText = "离开电脑";
          }
        } else if (data.action === "suggest") {
          emotion = "concerned";
        }

        window.dispatchEvent(
          new CustomEvent("deskmate:status", {
            detail: {
              activity: activityText,
              emotion,
              confidence: data.confidence,
            },
          }),
        );

        // 如果有建议，推送 suggestion 事件
        if (data.action === "suggest" && data.suggestion_content) {
          window.dispatchEvent(
            new CustomEvent("deskmate:suggestion", {
              detail: {
                id: `tick-${Date.now()}`,
                suggestion_content: data.suggestion_content,
                suggestion_type: data.suggestion_type || "generic",
                confidence: data.confidence,
                reasoning: data.reasoning,
              },
            }),
          );
        }

        // P2-4：长时间专注陪伴语 → 走 CareToast 温暖呈现
        if (data.companion_message) {
          window.dispatchEvent(
            new CustomEvent("deskmate:companion", {
              detail: {
                id: `companion-${Date.now()}`,
                text: data.companion_message,
                type: "companion",
              },
            }),
          );
        }

        // P2-4：「看见你在做什么」活动描述 → 状态面板展示
        if (data.what_user_doing) {
          window.dispatchEvent(
            new CustomEvent("deskmate:activity-desc", {
              detail: { text: data.what_user_doing },
            }),
          );
        }
      } catch (err) {
        console.error("[DeskMate] tick error:", err);
      }
    };

    // 桌面活动触发（去抖）
    const maybeTick = () => {
      const now = Date.now();
      if (now - lastTick < MIN_GAP) return;
      if (window.__deskmateTurnActive) return;
      lastTick = now;
      void tick();
    };

    window.addEventListener("focus", maybeTick);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) maybeTick();
    });
    window.addEventListener("mousemove", maybeTick);
    window.addEventListener("keydown", maybeTick);

    // 慢心跳兜底（仅页面可见时）：自调度 setTimeout，间隔跟随活力调制动态变化
    const scheduleHeartbeat = () => {
      if (heartbeatRef.current) clearTimeout(heartbeatRef.current);
      heartbeatRef.current = setTimeout(() => {
        if (!document.hidden) maybeTick();
        scheduleHeartbeat();
      }, heartbeatMsRef.current);
    };
    scheduleHeartbeat();

    // 首屏延迟 8 秒先跑一次
    const firstTimer = setTimeout(maybeTick, 8_000);

    return () => {
      clearTimeout(firstTimer);
      if (heartbeatRef.current) clearTimeout(heartbeatRef.current);
      window.removeEventListener("focus", maybeTick);
      document.removeEventListener("visibilitychange", maybeTick as EventListener);
      window.removeEventListener("mousemove", maybeTick);
      window.removeEventListener("keydown", maybeTick);
    };
  }, [agent, enabled]);
}
