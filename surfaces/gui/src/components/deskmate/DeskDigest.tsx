/**
 * DeskDigest — 桌搭模式首页资讯卡片。
 *
 * 在 DeskHome 底部、Composer 上方展示文化 / 网文 / AI / 开源 四分类资讯胶囊。
 * 数据来自 /v1/deskmate/digest，由后端定时抓取并用 LLM 提炼；点击卡片不是跳外链，
 * 而是向当前会话发送一条消息触发 AI 搜索并总结该话题。
 */

import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Icon } from "../Icon";
import { authedFetch, httpBase } from "../../api";

export interface DigestItem {
  id: string;
  category: string;
  title: string;
  summary: string;
  query: string;
  source_count: number;
  freshness: string;
}

const CATEGORY_META: Record<string, { label: string; icon: Parameters<typeof Icon>[0]["name"] }> = {
  culture: { label: "文化", icon: "bookOpen" },
  webnovel: { label: "网文", icon: "penTool" },
  ai: { label: "AI", icon: "cpu" },
  opensource: { label: "开源", icon: "code" },
};

function dispatchSendMessage(text: string) {
  window.dispatchEvent(new CustomEvent("deskmate:send-message", { detail: { text } }));
}

export function DeskDigest() {
  const { t } = useTranslation();
  const [items, setItems] = useState<DigestItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const load = useCallback(async (force = false) => {
    setLoading(true);
    setError(null);
    try {
      const res = await authedFetch(`${httpBase()}/v1/deskmate/digest${force ? "?force=true" : ""}`);
      if (!res.ok) throw new Error(`digest ${res.status}`);
      const data = await res.json();
      const list = Array.isArray(data.items) ? data.items : [];
      setItems(list);
    } catch (e) {
      setError(t("deskmate.digestLoadError", "资讯加载失败"));
      // 静默降级：失败时保留旧数据
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    load();
    // 每 30 分钟在后台刷新一次，让用户始终看到较新的资讯
    const timer = window.setInterval(() => load(), 30 * 60 * 1000);
    return () => window.clearInterval(timer);
  }, [load]);

  const visibleItems = items.slice(0, 6);
  const extraItems = items.slice(6);

  return (
    <div className="dm-digest">
      <div className="dm-digest-header">
        <span className="dm-digest-title">
          <Icon name="rss" size={14} />
          {t("deskmate.digestTitle", "今日资讯")}
        </span>
        <div className="dm-digest-actions">
          {loading && <span className="dm-digest-spin" />}
          <button
            className="dm-digest-refresh"
            type="button"
            onClick={() => load(true)}
            title={t("deskmate.digestRefresh", "刷新资讯")}
            disabled={loading}
          >
            <Icon name="refresh" size={13} />
          </button>
        </div>
      </div>

      {error && <div className="dm-digest-error">{error}</div>}

      <div className="dm-digest-grid">
        {visibleItems.map((item) => {
          const meta = CATEGORY_META[item.category] ?? { label: item.category, icon: "file-text" as const };
          return (
            <button
              key={item.id}
              className="dm-digest-card"
              type="button"
              onClick={() => dispatchSendMessage(item.query)}
              title={item.summary}
            >
              <span className="dm-digest-tag">
                <Icon name={meta.icon} size={11} />
                {meta.label}
              </span>
              <span className="dm-digest-card-title">{item.title}</span>
              <span className="dm-digest-card-summary">{item.summary}</span>
              <span className="dm-digest-card-meta">{item.freshness}</span>
            </button>
          );
        })}
      </div>

      {extraItems.length > 0 && (
        <div className="dm-digest-extra">
          {expanded.extra ? (
            <div className="dm-digest-grid">
              {extraItems.map((item) => {
                const meta = CATEGORY_META[item.category] ?? { label: item.category, icon: "file-text" as const };
                return (
                  <button
                    key={item.id}
                    className="dm-digest-card"
                    type="button"
                    onClick={() => dispatchSendMessage(item.query)}
                    title={item.summary}
                  >
                    <span className="dm-digest-tag">
                      <Icon name={meta.icon} size={11} />
                      {meta.label}
                    </span>
                    <span className="dm-digest-card-title">{item.title}</span>
                    <span className="dm-digest-card-summary">{item.summary}</span>
                    <span className="dm-digest-card-meta">{item.freshness}</span>
                  </button>
                );
              })}
            </div>
          ) : (
            <button
              className="dm-digest-more"
              type="button"
              onClick={() => setExpanded((p) => ({ ...p, extra: true }))}
            >
              {t("deskmate.digestMore", "展开更多")}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
