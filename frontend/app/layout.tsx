import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = { title: "产品工厂 · 本机工作台", description: "管理产品项目与只读接入资料" };
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return <html lang="zh-CN"><body>{children}</body></html>;
}
