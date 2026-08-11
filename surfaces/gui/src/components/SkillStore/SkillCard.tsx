import { useState } from "react";
import type { SkillEntry } from "../../api";
import { installSkill, uninstallSkill } from "../../api";

const CATEGORY_LABELS: Record<string, string> = {
  search: "搜索",
  document: "文档",
  database: "数据库",
  development: "开发",
  productivity: "效率",
  other: "其他",
};

export function SkillCard({
  skill,
  onInstalled,
}: {
  skill: SkillEntry;
  onInstalled: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isInstalled = skill.status === "installed" || skill.status === "configured";

  const handleInstall = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await installSkill(skill.id);
      if (res.ok) {
        onInstalled();
      } else {
        setError(res.error || "安装失败");
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "安装失败");
    } finally {
      setBusy(false);
    }
  };

  const handleUninstall = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await uninstallSkill(skill.id);
      if (res.ok) {
        onInstalled();
      } else {
        setError(res.error || "卸载失败");
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "卸载失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-xl border border-line bg-panel p-4 flex flex-col gap-2 hover:shadow-md transition-shadow">
      <div className="flex items-start gap-3">
        <div className="text-2xl shrink-0">{skill.icon || "🔧"}</div>
        <div className="flex-1 min-w-0">
          <div className="text-[14px] font-medium truncate">{skill.name}</div>
          <div className="text-[12px] text-muted line-clamp-2">{skill.description}</div>
        </div>
      </div>

      <div className="flex items-center gap-2 text-[11px] text-faint">
        <span className="px-1.5 py-0.5 rounded bg-faint/30">
          {CATEGORY_LABELS[skill.category] || skill.category}
        </span>
        {skill.version && <span>v{skill.version}</span>}
      </div>

      {error && <div className="text-[12px] text-danger">{error}</div>}

      <div className="flex items-center gap-2 mt-auto pt-2">
        {isInstalled ? (
          <>
            <span className="text-[12px] text-ok">已安装</span>
            <button
              className="text-[12px] text-danger hover:underline ml-auto"
              onClick={handleUninstall}
              disabled={busy}
            >
              {busy ? "..." : "卸载"}
            </button>
          </>
        ) : (
          <button
            className="px-3 py-1.5 rounded-lg bg-accent text-white text-[12px] font-medium hover:opacity-90 disabled:opacity-50"
            onClick={handleInstall}
            disabled={busy}
          >
            {busy ? "安装中…" : "安装"}
          </button>
        )}
      </div>
    </div>
  );
}
