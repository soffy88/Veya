'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';

const navItems = [
  { href: '/', label: 'Chat', icon: '💬' },
  { href: '/models', label: 'Models', icon: '🤖' },
  { href: '/security', label: 'Security', icon: '🔒' },
  { href: '/cicd', label: 'CI/CD', icon: '🚀' },
];

export function LeftPanel() {
  const pathname = usePathname();

  return (
    <aside className="w-64 bg-gray-50 border-r h-screen p-4">
      <div className="mb-6">
        <h2 className="text-xl font-bold">Hicode</h2>
        <p className="text-xs text-gray-600">AI Agent Platform</p>
      </div>

      <nav className="space-y-2">
        {navItems.map((item) => {
          const isActive = pathname === item.href;
          return (
            <Link
              key={item.href}
              href={item.href}
              className={`flex items-center gap-3 px-3 py-2 rounded-lg transition-colors ${
                isActive
                  ? 'bg-blue-100 text-blue-700 font-semibold'
                  : 'hover:bg-gray-200 text-gray-700'
              }`}
            >
              <span className="text-xl">{item.icon}</span>
              <span>{item.label}</span>
            </Link>
          );
        })}
      </nav>

      <div className="mt-auto pt-6 border-t">
        <div className="text-xs text-gray-500">
          <p>Version 0.2.0</p>
          <p className="mt-1">Connected to localhost:8000</p>
        </div>
      </div>
    </aside>
  );
}
