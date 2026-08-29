import { useEffect, useState } from "react";
import type { SkillEntry, DeskmateCuratorStatus } from "../../api";
import { getSkills, getDeskmateSkillsCurator, restoreDeskmateSkill } from "../../api";
import { SkillCard } from "./SkillCard";
import { ConfigModal } from "./ConfigModal";

const CATEGORIES = [
  { key: "all", label: "全部" },
  { key: "search", label: "搜索" },
  { key: "document", label: "文档" },
  { key: "database", label: "数据库" },
  { key: "development", label: "开发" },
  { key: "productivity", label: "效率" },
  { key: "other", label: "其他" },
];

export function SkillStore() {
  const [skills, setSkills] = useState<SkillEntry[]>([]);
  const [category, setCategory] = useState("all");
  const [configSkill, setConfigSkill] = useState<SkillEntry | null>(null);
  const [loading, setLoading] = useState(true);
  const [curator, setCurator] = useState<DeskmateCuratorStatus | null>(null);
  const [restoring, setRestoring] = useState<string | null>(null);

  const load = async () => {
    try {
      const res = await getSkills();
      setSkills(res.skills || []);
    } catch {
      setSkills([]);
    } finally {
      setLoading(false);
    }
  };

  const loadCurator = async () => {
    try {
      const res = await getDeskmateSkillsCurator();
      setCurator(res);
    } catch {
      setCurator(null);
    }
  };

  useEffect(() => {
    load();
    loadCurator();
  }, []);

  const filtered = skills.filter(
    (s) => category === "all" || s.category === category,
  );

  const installedCount = skills.filter(
    (s) => s.status === "installed" || s.status === "configured",
  ).length;

  return (
    <div className="flex-1 min-w-0 overflow-y-auto hairline-scroll">
      <div className="max-w-5xl mx-auto px-7 py-6">
        {/* Header */}
        <div className="mb-6">
          <h1 className="text-[20px] font-semibold tracking-tight">技能广场</h1>
          <p className="text-[13px] text-muted mt-1">
            安装 MCP 技能，扩展 AI 的能力。安装后按需配置 API Key。
          </p>
        </div>

        {/* Stats + category filter */}
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            {CATEGORIES.map((c) => (
              <button
                key={c.key}
                className={
                  "px-3 py-1.5 rounded-lg text-[12px] transition-colors " +
                  (category === c.key
                    ? "bg-accent text-white"
                    : "bg-faint/30 text-muted hover:bg-faint/50")
                }
                onClick={() => setCategory(c.key)}
              >
                {c.label}
              </button>
            ))}
          </div>
          <div className="text-[12px] text-faint">
            已安装 {installedCount} / {skills.length}
          </div>
        </div>

        {/* Skill grid */}
        {loading ? (
          <div className="text-center text-[13px] text-muted py-12">加载中…</div>
        ) : filtered.length === 0 ? (
          <div className="text-center text-[13px] text-muted py-12">暂无技能</div>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {filtered.map((skill) => (
              <SkillCard
                key={skill.id}
                skill={skill}
                onInstalled={load}
              />
            ))}
          </div>
        )}

        {/* Installed section */}
        {installedCount > 0 && (
          <div className="mt-8">
            <h2 className="text-[16px] font-semibold mb-3">已安装</h2>
            <div className="space-y-2">
              {skills
                .filter((s) => s.status === "installed" || s.status === "configured")
                .map((skill) => (
                  <div
                    key={skill.id}
                    className="flex items-center gap-3 p-3 rounded-lg border border-line bg-panel"
                  >
                    <span className="text-lg">{skill.icon || "🔧"}</span>
                    <div className="flex-1 min-w-0">
                      <div className="text-[13px] font-medium truncate">{skill.name}</div>
                      <div className="text-[11px] text-faint">{skill.description}</div>
                    </div>
                    {skill.status === "configured" ? (
                      <span className="text-[11px] text-ok">已配置</span>
                    ) : (
                      <button
                        className="text-[12px] text-accent hover:underline"
                        onClick={() => setConfigSkill(skill)}
                      >
                        配置
                      </button>
                    )}
                  </div>
                ))}
            </div>
          </div>
        )}

        {/* Curator / 归档与自整理 (P0-7) */}
        <div className="mt-8">
          <h2 className="text-[16px] font-semibold mb-3">自整理（curator）</h2>
          <div className="text-[12px] text-faint mb-3">
            自动归档：
            <span className={curator?.auto_archive_enabled ? "text-ok" : "text-muted"}>
              {curator?.auto_archive_enabled ? "已开启" : "关闭（默认只报告）"}
            </span>
            {" · "}重叠可归档候选 {curator?.auto_archive_candidates.length ?? 0} 个
          </div>
          {curator && curator.archived_names.length > 0 ? (
            <div className="space-y-2">
              <div className="text-[12px] text-muted">已归档（未删除，可一键还原）：</div>
              {curator.archived_names.map((name) => (
                <div
                  key={name}
                  className="flex items-center gap-3 p-3 rounded-lg border border-line bg-panel"
                >
                  <span className="text-lg">🗄️</span>
                  <div className="flex-1 min-w-0">
                    <div className="text-[13px] font-medium truncate">{name}</div>
                    <div className="text-[11px] text-faint">归档在 archive/，不丢失</div>
                  </div>
                  <button
                    className="text-[12px] text-accent hover:underline disabled:opacity-50"
                    disabled={restoring === name}
                    onClick={async () => {
                      setRestoring(name);
                      try {
                        await restoreDeskmateSkill(name);
                        await loadCurator();
                      } finally {
                        setRestoring(null);
                      }
                    }}
                  >
                    {restoring === name ? "还原中…" : "还原"}
                  </button>
                </div>
              ))}
            </div>
          ) : (
            <div className="text-[12px] text-faint">暂无归档技能。</div>
          )}
        </div>
      </div>

      {/* Config modal */}
      {configSkill && (
        <ConfigModal
          skill={configSkill}
          onClose={() => setConfigSkill(null)}
          onConfigured={load}
        />
      )}
    </div>
  );
}
