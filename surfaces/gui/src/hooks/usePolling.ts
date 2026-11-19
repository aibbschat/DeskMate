import { useEffect, useRef } from "react";

/**
 * 统一轮询 hook（2026-11-19 PR9）。
 *
 * 目的：把散落各处的 setInterval 收敛到一个实现，统一提供
 *   - 页面隐藏时暂停（document.hidden），回到前台立刻补一次；
 *   - 连续失败指数退避（避免后端抖动时轮询风暴）；
 *   - 卸载时清理定时器 / 取消在途请求；
 *   - 不并发：上一次还没结束就不再发下一次。
 *
 * 刻意**不改变**各调用点原有的间隔（会话列表 15s 等），那些值是被前几轮
 * 性能事故调过的，见 App.tsx 的历史注释。这里只加护栏，不调频率。
 */
export function usePolling(
  fn: () => void | Promise<void>,
  intervalMs: number,
  options: { enabled?: boolean; immediate?: boolean } = {},
): void {
  const { enabled = true, immediate = false } = options;
  const fnRef = useRef(fn);
  fnRef.current = fn;

  useEffect(() => {
    if (!enabled) return;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let inFlight = false;
    let failures = 0;
    let stopped = false;

    const schedule = (delay: number) => {
      if (stopped) return;
      timer = setTimeout(run, delay);
    };

    const run = async () => {
      if (stopped || inFlight) return;
      if (typeof document !== "undefined" && document.hidden) {
        // 后台标签：只等待，不请求；visibilitychange 时会立刻补一次。
        return;
      }
      inFlight = true;
      try {
        await fnRef.current();
        failures = 0;
      } catch {
        failures += 1;
      } finally {
        inFlight = false;
        if (!stopped) {
          const backoff = failures > 0 ? Math.min(intervalMs * 2 ** failures, 60_000) : 0;
          schedule(intervalMs + backoff);
        }
      }
    };

    const onVisible = () => {
      if (!document.hidden && !stopped && !inFlight) {
        if (timer) clearTimeout(timer);
        void run();
      }
    };
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", onVisible);
    }

    if (immediate) void run();
    else schedule(intervalMs);

    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      if (typeof document !== "undefined") {
        document.removeEventListener("visibilitychange", onVisible);
      }
    };
  }, [enabled, intervalMs, immediate]);
}
