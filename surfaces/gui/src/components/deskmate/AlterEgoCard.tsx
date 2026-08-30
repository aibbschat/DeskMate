/**
 * AlterEgoCard — 个人 AI 分身模式（P2-6）。
 *
 * 分身模式（可选开启，默认关）：开启后墨灵从对话中持续学习你的决策偏好，
 * 并在对话时注入「另一个我」画像，以你的写作风格与思维方式输出，而非通用 AI 风格。
 * 纯本地规则、零 LLM 成本、数据由你掌控。数据来自 GET/POST /v1/deskmate/alter-ego。
 *
 * 设计原则（8.0 红线「懂你 = 可感可看」）：
 * - 暖米白 + 暖橙配色，与 SyncCard 一致的温度感
 * - 诚实：API 失败静默降级，显示兜底状态；开关操作有明确反馈
 */

import { useEffect, useState } from "react";
import { authedFetch, httpBase } from "../../api";
import { Icon } from "../Icon";

interface AlterEgoState {
  enabled: boolean;
  has_data: boolean;
  total_signals: number;
  mode: string;
}

const FALLBACK: AlterEgoState = {
  enabled: false,
  has_data: false,
  total_signals: 0,
  mode: "full",
};

interface Props {
  onOpenPage?: () => void;
}

export function AlterEgoCard({ onOpenPage }: Props) {
  const [data, setData] = useState<AlterEgoState | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => {
    authedFetch(`${httpBase()}/v1/deskmate/alter-ego`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) =>
        setData(d && typeof d.enabled === "boolean" ? (d as AlterEgoState) : FALLBACK),
      )
      .catch(() => setData(FALLBACK));
  }, []);

  const toggle = async (next: boolean) => {
    setBusy(true);
    setMsg(null);
    try {
      const r = await authedFetch(`${httpBase()}/v1/deskmate/alter-ego`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ enabled: next }),
      });
      const d = await r.json();
      if (d?.ok) {
        setData((prev) => ({ ...(prev ?? FALLBACK), enabled: next }));
        setMsg(
          next
            ? "已开启分身模式：对话中将学习你的决策偏好并注入「另一个我」画像。"
            : "已关闭分身模式。",
        );
      } else {
        setMsg("操作失败，请稍后再试。");
      }
    } catch {
      setMsg("操作失败，请稍后再试。");
    } finally {
      setBusy(false);
    }
  };

  const s = data ?? FALLBACK;

  return (
    <div className="dm-alter-ego-card">
      <div className="dm-alter-ego-head">
        <Icon name="sparkle" size={18} />
        <div>
          <div className="dm-alter-ego-title">个人 AI 分身</div>
          <div className="dm-alter-ego-sub">另一个我 · 可选开启</div>
        </div>
        <span className={`dm-alter-ego-dot ${s.enabled ? "on" : "off"}`} />
      </div>

      <p className="dm-alter-ego-desc">
        开启后，墨灵会从你的对话中持续学习决策偏好（风险取舍、先后顺序、改动倾向等），
        并在对话时注入「另一个我」画像——以你的写作风格与思维方式输出，而非通用 AI 风格。
        纯本地规则、零 LLM 成本、数据由你掌控。
      </p>

      <div className="dm-alter-ego-stats">
        <div>
          <span>已学信号</span>
          <b>{s.total_signals}</b>
        </div>
        <div>
          <span>画像就绪</span>
          <b>{s.has_data ? "是" : "否"}</b>
        </div>
      </div>

      <div className="dm-alter-ego-actions">
        {s.enabled ? (
          <button
            className="dm-alter-ego-btn"
            disabled={busy}
            onClick={() => toggle(false)}
            type="button"
          >
            关闭分身模式
          </button>
        ) : (
          <button
            className="dm-alter-ego-btn primary"
            disabled={busy}
            onClick={() => toggle(true)}
            type="button"
          >
            开启分身模式
          </button>
        )}
        {onOpenPage && (
          <button
            className="dm-alter-ego-btn"
            onClick={onOpenPage}
            type="button"
            style={{ background: "rgba(194,102,47,0.08)", color: "#c2662f", border: "1px solid rgba(194,102,47,0.2)" }}
          >
            查看它眼中的我 →
          </button>
        )}
      </div>

      {msg && <p className="dm-alter-ego-result">{msg}</p>}
    </div>
  );
}
