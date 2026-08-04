import './globals.css';
import type { Metadata } from 'next';
export const metadata: Metadata = { title: 'veya', description: 'AI 编码工作台' };
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return <html lang="zh" data-theme="terminal-dark"><body>{children}</body></html>;
}
