import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "감사렌즈 — 코스피 감사보고서 AI 분석",
  description: "코스피 전 종목 감사보고서·재무제표 주석을 근거와 함께 검색하는 회계사용 AI 도구",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="ko" className="h-full antialiased">
      <body className="min-h-full flex flex-col">{children}</body>
    </html>
  );
}
