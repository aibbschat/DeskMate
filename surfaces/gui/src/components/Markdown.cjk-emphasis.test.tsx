import { renderToStaticMarkup } from "react-dom/server";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { describe, expect, it } from "vitest";
import { repairCjkEmphasis } from "./Markdown";

// 渲染一遍，确认真实产出里没有裸露的星号（只看函数返回值会漏掉"删了但解析器仍不认"）
function render(md: string): string {
  return renderToStaticMarkup(
    <ReactMarkdown remarkPlugins={[remarkGfm]}>{repairCjkEmphasis(md)}</ReactMarkdown>,
  );
}

describe("repairCjkEmphasis（中文标点收尾的加粗）", () => {
  it("「**小标题。**正文」不再露出星号，且一个字都不少", () => {
    const src =
      "**三、方也行单独泡洗澡，这个细节你可以直接当钩子用。**敌人十有八九以为落单的是块软肉。";
    const out = repairCjkEmphasis(src);
    expect(out).not.toContain("**");
    expect(out).toContain("当钩子用。敌人十有八九以为落单的是块软肉。");
    expect(render(src)).not.toContain("**");
  });

  it("合法加粗不动：闭合后是标点", () => {
    const src = "**一**，第十五章换了名。";
    expect(repairCjkEmphasis(src)).toBe(src);
    expect(render(src)).toContain("<strong>一</strong>");
  });

  it("合法加粗不动：闭合前是汉字（非标点）", () => {
    const src = "**敌人**十有八九以为落单的是块软肉。";
    expect(repairCjkEmphasis(src)).toBe(src);
    expect(render(src)).toContain("<strong>敌人</strong>");
  });

  it("合法加粗不动：英文 intraword", () => {
    const src = "a**bold**text";
    expect(repairCjkEmphasis(src)).toBe(src);
  });

  it("合法加粗不动：闭合后是空格", () => {
    const src = "**小标题。** 后面有空格";
    expect(repairCjkEmphasis(src)).toBe(src);
  });

  it("未闭合的孤星也删掉——同样是裸符号", () => {
    const src = "**一、没有闭合的小标题\n正文继续。";
    const out = repairCjkEmphasis(src);
    expect(out).not.toContain("**");
    expect(out).toContain("一、没有闭合的小标题");
  });

  it("fenced 代码块里的星号原样保留", () => {
    const src = "说明：\n```\nconst a = '**x**';\n```\n结束。";
    expect(repairCjkEmphasis(src)).toBe(src);
  });

  it("多组混排：坏的删、好的留", () => {
    const src =
      "**第一。**正文一。\n**第二**，正文二。\n**第三**正文三。";
    const out = repairCjkEmphasis(src);
    expect(out).not.toContain("**第一");
    expect(out).toContain("**第二**，");
    expect(out).toContain("**第三**正文三。");
  });

  it("没有星号时零成本返回原串", () => {
    const src = "普通一段话，没有任何标记。";
    expect(repairCjkEmphasis(src)).toBe(src);
  });
});
