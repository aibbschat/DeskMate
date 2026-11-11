import type { IconName } from "../Icon";
import { shortPersonaName } from "../../personaScope";
import brainstormerAvatar from "../../assets/avatars/brainstormer.png";
// 2026-11-10 补齐编辑 / 大纲 / 人物三位的正式头像：沿用既有 256×256 米色纸纹 +
// 圆形浅晕的扁平静物插画风格（与 brainstormer / adaptation 等成套）。
import editorAvatar from "../../assets/avatars/editor.png";
import outlineAvatar from "../../assets/avatars/outline.png";
import characterAvatar from "../../assets/avatars/character.png";

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
    // 2026-11-08 决策 A：开脑洞固定在独立专家（不进 Bot 群、不挂共享记忆）。
    // 卡片就是入口——想开脑洞点这里，不要回群聊里找。
    id: "brainstormer",
    displayName: "创意脑洞专家",
    icon: "sparkle",
    avatar: brainstormerAvatar,
    description:
      "一对一开脑洞：先给几条真正不同的方向，打开你的写作思路；你挑中一条，它再往下盘成能落笔的方案。",
    status: "ready",
    capabilities: ["开脑洞", "发散方向", "反转与钩子", "挑中再往下盘"],
    personaId: "brainstormer",
  },
  {
    id: "editor",
    displayName: "编辑专家",
    icon: "pencil",
    avatar: editorAvatar,
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
    avatar: outlineAvatar,
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
    personaId: "outline",
    systemPrompt:
      "你是一位历史小说的结构大纲专家，尤其擅长给萧盛的历史小说搭建骨架。你会严格遵循大纲 SOP（史料锚定 → 立主脑 → 结构骨架 → 人物弧光 → 冲突节奏），用人物圣经（CHARACTERS/OUTLINE/TIMELINE/NOTES）做 grounding，产出强制结构化的卷纲表与章纲（含时间定位、史实锚点、人物、核心事件、戏剧功能、存疑项）。你只搭骨架不代写正文；所有艺术加工与史料异说必须进存疑项段并标正史版本与出处，绝不混淆虚实；与圣经冲突的设定显式标注，不静默覆盖。",
    // 关联 SkillStore 技能 outline：召唤本专家进入会话后，提到"写大纲/卷纲/章纲"时
    // 由技能自动匹配注入；此处显式登记，便于将来强制 pin 与 UI 展示「关联技能」。
    metadata: { skillId: "outline" },
  },
  {
    // 2026-11-09 萧哥定：人物专家真做出来（persona + 私有笔记本 + 可被小桔调用）。
    // 之前占位卡撤掉是因为点进去是空的；现在先有 persona 再上卡。
    id: "character",
    displayName: "人物专家",
    icon: "users",
    avatar: characterAvatar,
    description:
      "盯人物弧光、动机、关系与口吻：谁的行为立不住、谁跑偏了、关系变化是否可信、重要人物有没有失踪或降智。",
    status: "ready",
    capabilities: ["人物一致性", "弧光与动机", "关系网", "口吻/OOC", "人物小传"],
    personaId: "character",
    systemPrompt:
      "你是一位历史小说的人物专家，尤其擅长长篇群像与历史人物的可信度。你盯人物一致性、弧光、动机、关系与口吻，判断谁的行为立不住、谁跑偏了、关系变化是否可信，并给出可执行的修改建议；你不代写正文。",
  },
];

export function getExpert(id: string): Expert | undefined {
  return EXPERTS.find((e) => e.id === id);
}

/** 按 persona id 反查本地专家。id 与 personaId 不总一致（short-drama → adaptation），
 *  而会话的 agent 字段装的是 personaId，所以不能拿 getExpert(id) 直接查。 */
export function getExpertByPersonaId(personaId: string): Expert | undefined {
  return EXPERTS.find((e) => e.personaId === personaId);
}

/** 聊天气泡的发言者身份；null = 普通会话，仍显示小桔。 */
export interface SessionSpeaker {
  name: string;
  /** persona 头像图；无图时交给 ExpertAvatar 用渐变首字占位 */
  avatar?: string;
  /** 本地 EXPERTS 项，供 ExpertAvatar 取头像 / 配色 */
  expert?: Expert;
}

/**
 * 按「会话 agent（= persona id）」派生专家发言者（2026-11-10 萧哥定：只改专家、
 * 头像一起换、桌搭三张卡片保持原样）。事实源是会话 agent，不是 currentExpert：
 * 后者只在专家中心召唤时写入，从右栏 / 新建会话下拉起的 1V1 会漏、刷新会丢、
 * 切会话还会残留上一个专家 —— 正是回复者名字错配的根因。
 * builtinAgentIds（cowork / chat / code / deskmate / myhelper）一律返回 null，
 * 全局 chat.assistant 兜底「小桔」保持不动。
 */
export function expertSpeakerFor(
  agent: string,
  opts: {
    builtinAgentIds: ReadonlySet<string>;
    persona?: { id: string; name: string } | null;
    personaAvatar?: string;
  },
): SessionSpeaker | null {
  if (opts.builtinAgentIds.has(agent)) return null;
  const local = getExpertByPersonaId(agent);
  if (local) {
    return {
      name: local.displayName,
      expert: { ...local, avatar: local.avatar || opts.personaAvatar },
    };
  }
  if (opts.persona) {
    return { name: shortPersonaName(opts.persona.name, opts.persona.id), avatar: opts.personaAvatar };
  }
  return null;
}

/** 会话空态名片的展示数据（名字 / 头像 / 人设介绍 / 开场提示）。 */
export interface ExpertIntro {
  name: string;
  /** 有本地专家卡时用它渲染 ExpertAvatar（PNG 或渐变首字占位） */
  expert?: Expert;
  /** 无本地卡时回落静物图（圆桌老专家等） */
  avatar?: string;
  description: string;
  hint: string;
}

/**
 * 会话空态名片的唯一事实源（2026-11-11 萧哥定：同一个专家，不管从专家中心、
 * 右栏伙伴还是任何入口进来，头像与人设介绍必须一样）。
 *
 * 事实源是会话 agent（= persona id），不是 `currentExpert` —— 后者只在专家中心
 * 召唤时写入，右栏 / 下拉起的 1V1 是 null、刷新会丢、切会话会残留上一个专家，
 * 正是「两边同一专家长得不一样」的根因。与 expertSpeakerFor 同源。
 */
export function expertIntroFor(
  agent: string,
  opts: {
    builtinAgentIds: ReadonlySet<string>;
    persona?: { id: string; name: string; tagline?: string } | null;
    personaAvatar?: string;
  },
): ExpertIntro | null {
  const speaker = expertSpeakerFor(agent, {
    builtinAgentIds: opts.builtinAgentIds,
    persona: opts.persona,
    personaAvatar: opts.personaAvatar,
  });
  if (!speaker) return null;
  if (speaker.expert) {
    return {
      name: speaker.name,
      expert: speaker.expert,
      description: speaker.expert.description,
      hint: speaker.expert.manuscriptReview
        ? "点右上角「提交书稿」即可开始深度审阅；选中章节后，审阅会在对话中分批完成。"
        : "在下方输入框描述你的需求，开始与专家协作。",
    };
  }
  return {
    name: speaker.name,
    avatar: speaker.avatar,
    description: opts.persona?.tagline || "",
    hint: "在下方输入框直接开聊；需要其他视角时 @名字 可临时请专家补位。",
  };
}
