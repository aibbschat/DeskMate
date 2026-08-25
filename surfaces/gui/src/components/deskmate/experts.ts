import type { IconName } from "../Icon";

// 桌搭「专家模式」的可扩展抽象。
// 第一个实例是「编辑专家」；未来在此注册大纲专家、人物专家、润色专家等，
// 回复角色（Transcript 的 who）与 Composer 专家条会自动跟随当前激活的专家变化。
// 专家中心（OpenWorker 侧边栏「专家」导航）直接读取 EXPERTS 渲染卡片，
// 新增专家只需在此 push 一项，UI 自动出现。

export type ExpertStatus = "ready" | "soon";

export interface Expert {
  id: string;
  displayName: string;
  /** Icon 组件支持的线性图标名 */
  icon: IconName;
  /** 专家头像 URL；未提供时使用名称首字 + 渐变占位头像 */
  avatar?: string;
  description: string;
  /** 预留：注入对话后端作为系统提示，使专家在对话中真正扮演该角色 */
  systemPrompt?: string;
  /** 卡片上展示的能力标签（短词） */
  capabilities?: string[];
  /** 就绪 / 即将上线。默认 "ready"。 */
  status?: ExpertStatus;
  /** 关联的 persona id — 召唤时切换到该 persona 的 Agent 会话 */
  personaId?: string;
  /**
   * 是否启用「书稿审阅」流程（提交书稿 + 生成深度审阅报告）。
   * 数据驱动开关：任何专家置 true 即拥有该能力，避免按 id 硬编码。
   */
  manuscriptReview?: boolean;
  /**
   * 专家的额外配置数据（各专家自由定义）。
   * 例：大纲专家可绑定 OUTLINE.md 路径，人物专家可绑定 CHARACTERS.md 路径。
   * 前端不直接消费此字段，由后端 Agent 按需读取。
   */
  metadata?: Record<string, any>;
}

export const EXPERTS: Expert[] = [
  {
    id: "editor",
    displayName: "编辑专家",
    icon: "pencil",
    description: "深度审阅你的小说稿件，逐章细抠，并产出完整的审阅报告。",
    status: "ready",
    capabilities: ["逐章深度审阅", "产出审阅报告", "人物/情节/文笔分析", "设定一致性核查"],
    personaId: "editor",
    manuscriptReview: true,
    systemPrompt:
      "你是一位资深文学编辑，尤其擅长历史小说。你会深度阅读用户的小说稿件，从人物一致性、情节与结构、文笔与语言、设定一致性等维度进行审阅，并在用户要求时产出结构化的审阅报告（含总体评价与逐章问题清单）。",
  },
  {
    id: "short-drama",
    displayName: "短剧剧本",
    icon: "penTool",
    description: "把你的历史小说（如《大明快报》）改编为竖屏短剧：定基调 → 分集大纲 → 分镜脚本 → 剧本医生精修。",
    status: "ready",
    capabilities: ["分集大纲", "分镜拍摄脚本", "剧本医生精修", "人物圣经 grounding", "作者风格锁死"],
    personaId: "adaptation",
    systemPrompt:
      "你是一位竖屏短剧剧本改编专家，尤其擅长把萧盛的历史小说（如《大明快报》/《大明提塘官》）改编为分集大纲与分镜拍摄脚本。你会严格遵循短剧改编 SOP（定基调 → 全局大纲 → 分集大纲 → 分镜脚本 → 剧本医生精修），锁定作者 8 条写作风格并用人物圣经做 grounding，产出交外包公司用的前期纯文本稿——不代笔续写、不擅自改原著硬设定（人物生死、核心事件、年代基调）。",
    // 关联 SkillStore 技能：召唤本专家进入会话后，提到"改编短剧/分集大纲/分镜脚本"时
    // 由技能自动匹配注入（skill_match_and_inject）；此处显式登记，便于将来强制 pin 与 UI 展示。
    metadata: { skillId: "short-drama-script" },
  },
  // —— 阶段4 路线图（即将上线，卡片已占位，召唤暂未启用）——
  {
    id: "outline",
    displayName: "大纲专家",
    icon: "file",
    description:
      "为你的历史小说搭建可写、可核验、史料锚定的结构化大纲：卷纲 + 章纲，每步吃人物圣经 grounding，艺术加工全进存疑项。",
    status: "ready",
    capabilities: [
      "卷纲表",
      "章纲(时间/史实/人物/事件/戏剧功能/存疑)",
      "史料锚定",
      "人物圣经 grounding",
      "结构诊断",
    ],
    personaId: "lit-editor",
    systemPrompt:
      "你是一位历史小说的结构大纲专家，尤其擅长给萧盛的历史小说搭建骨架。你会严格遵循大纲 SOP（史料锚定 → 立主脑 → 结构骨架 → 人物弧光 → 冲突节奏），用人物圣经（CHARACTERS/OUTLINE/TIMELINE/NOTES）做 grounding，产出强制结构化的卷纲表与章纲（含时间定位、史实锚点、人物、核心事件、戏剧功能、存疑项）。你只搭骨架不代写正文；所有艺术加工与史料异说必须进存疑项段并标正史版本与出处，绝不混淆虚实；与圣经冲突的设定显式标注，不静默覆盖。",
    // 关联 SkillStore 技能 outline：召唤本专家进入会话后，提到"写大纲/卷纲/章纲"时
    // 由技能自动匹配注入；此处显式登记，便于将来强制 pin 与 UI 展示「关联技能」。
    metadata: { skillId: "outline" },
  },
  {
    id: "character",
    displayName: "人物专家",
    icon: "branch",
    description: "塑造立体角色：动机、弧光、关系网与对话一致性。",
    status: "soon",
    capabilities: ["人物小传", "关系图谱", "弧光设计"],
  },
  {
    id: "polish",
    displayName: "润色专家",
    icon: "sparkle",
    description: "在保留你文风的前提下，精修语句、节奏与语感。",
    status: "soon",
    capabilities: ["语感打磨", "冗余精简", "节奏优化"],
  },
  {
    id: "lore",
    displayName: "设定一致性专家",
    icon: "sliders",
    description: "跨章节追踪时间线、地名、官职等设定，揪出前后矛盾。",
    status: "soon",
    capabilities: ["时间线校验", "设定冲突检测", "名词统一"],
  },
];

export function getExpert(id: string): Expert | undefined {
  return EXPERTS.find((e) => e.id === id);
}
