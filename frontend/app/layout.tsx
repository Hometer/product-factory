import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = { title: "LaunchFlow｜AI Coding 产品工厂与 Harness 工具链", description: "针对多个 AI 产品开发中需求材料重复准备、实现偏离范围及验收标准不统一的问题，建立从需求定义、任务拆解到辅助开发、评审修复与交付验收的工作流程。" };
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return <html lang="zh-CN"><body>{children}</body></html>;
}
