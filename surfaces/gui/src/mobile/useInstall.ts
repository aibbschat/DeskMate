/**
 * useInstall —— 把 install.ts 的纯判断接到 React 上（只手机端用）。
 *
 * `beforeinstallprompt` 事件本体由 `mobile.html` 内联脚本抢先存进 window.__COWORKER_BIP__
 * （事件只发一次，等 React 挂监听就晚了）。这里只负责状态同步、点按钮、以及 manifest 自检。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { installCopyOf, installEnv, installStateOf, probeManifest, type InstallState, type ManifestProbe } from "./install";

type BipEvent = Event & { prompt: () => Promise<void>; userChoice?: Promise<{ outcome: string }> };

const bip = (): BipEvent | null => ((globalThis as any).__COWORKER_BIP__ as BipEvent) || null;

const snapshot = (): InstallState => installStateOf(installEnv());

export function useInstall() {
  const [state, setState] = useState<InstallState>(snapshot);
  const [probe, setProbe] = useState<ManifestProbe | null>(null);
  const [busy, setBusy] = useState(false);
  const alive = useRef(true);

  const recheck = useCallback(() => {
    void probeManifest().then((p) => alive.current && setProbe(p));
  }, []);

  useEffect(() => {
    alive.current = true;
    const sync = () => setState(snapshot());
    const installed = () => {
      (globalThis as any).__COWORKER_BIP__ = null;
      setState("installed");
    };
    window.addEventListener("beforeinstallprompt", sync);
    window.addEventListener("appinstalled", installed);
    let mq: MediaQueryList | null = null;
    try {
      mq = window.matchMedia("(display-mode: standalone)");
      mq.addEventListener?.("change", sync);
    } catch {
      mq = null;
    }
    const onVis = () => document.visibilityState === "visible" && sync();
    document.addEventListener("visibilitychange", onVis);
    recheck();
    return () => {
      alive.current = false;
      window.removeEventListener("beforeinstallprompt", sync);
      window.removeEventListener("appinstalled", installed);
      mq?.removeEventListener?.("change", sync);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [recheck]);

  /** 点「装到主屏」：走浏览器原生安装弹窗。返回结果供 UI 说人话。 */
  const install = useCallback(async (): Promise<"accepted" | "dismissed" | "unavailable"> => {
    const ev = bip();
    if (!ev) return "unavailable";
    setBusy(true);
    try {
      await ev.prompt();
      const choice = await (ev.userChoice || Promise.resolve({ outcome: "unknown" }));
      setState(snapshot());
      return choice?.outcome === "accepted" ? "accepted" : "dismissed";
    } catch {
      return "unavailable";
    } finally {
      setBusy(false);
    }
  }, []);

  return { state, copy: installCopyOf(state), probe, recheck, install, busy };
}
